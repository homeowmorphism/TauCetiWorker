"""Refactor rounds: A/B-test a design choice in merged code with the mathlib-api skill, then open a PR.

The agent follows the pinned mathlib-api skill (https://github.com/homeowmorphism/mathlib-api) on
code that already exists. It builds the current design and an alternative side by side, runs the
same consumer proofs against both, and opens a PR only when the comparison decides the question.

The comparison happens in a scratch directory outside the checkout. Outside, because
prepare_checkout cleans the checkout every round and a file there could be committed. Kept, because
the variants are the evidence behind the PR and the operator may want to re-run them. Each round
also leaves a one-line verdict, which the worker folds into a ledger so later rounds do not
re-examine a declaration this worker has already judged.

Review feedback flows back the same way. Before each round the worker reads the rubric findings on
its own refactor PRs and names the most recent in the prompt's self-review, so a mistake a reviewer
caught once is checked for in every later diff without anyone editing the prompt.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from .agents import fetch_pinned, fetch_ref, fill_prompt, prepare_checkout, run_agent_host, run_in_bubble, wrapper_bin
from .config import Config, Die, log
from .constants import (
    MATHLIB_API_SKILL,
    MATHLIB_API_SKILL_REF,
    REFACTOR_HEAD_PREFIX,
    REFACTOR_SCRATCH_KEEP,
    REVIEW,
    ROADMAP,
    TAUCETI,
)
from .github import GitHubError, claims_repo, ensure_fork, me
from .paths import HERE
from .review_state import META_RE

VERDICT = "verdict.json"
LEDGER = "refactor-verdicts.jsonl"
LEDGER_SHOWN = 40  # most recent verdicts named in the next prompt
_FIELD_CAP = 300  # characters kept per verdict field; the ledger is prompt text, not an archive

FINDINGS = "review-findings.json"
FINDINGS_SHOWN = 12  # most recent review findings named in the next prompt
# The review engine opens one thread per non-green rubric; its root carries this marker. A contest
# answer replies in that thread with a `tauceti-reply:` marker and either clears the finding or not.
_RUBRIC_RE = re.compile(r"<!--tauceti-rubric:([a-z][a-z-]*?)\s*-->")
_HEADER_VERDICT_RE = re.compile(r"^###\s.*?—\s*(block|request_changes)\b")
_BLOCKING = ("block", "request_changes")
_CLEARED = "this clears the finding"


def scratch_dir(cfg: Config) -> Path:
    """A fresh scratch directory for this round, after pruning all but the newest ones."""
    root = cfg.state / "refactor-scratch"
    root.mkdir(parents=True, exist_ok=True)
    rounds = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
    for old in rounds[REFACTOR_SCRATCH_KEEP - 1 :]:
        shutil.rmtree(old, ignore_errors=True)
    # The timestamp prefix keeps names in age order for the pruning above; mkdtemp keeps them unique.
    return Path(tempfile.mkdtemp(prefix=time.strftime("%Y%m%d-%H%M%S-"), dir=root))


def record_verdict(cfg: Config, scratch: Path) -> dict | None:
    """Append the agent's verdict file to the ledger; return the entry, or None if there was none.

    A missing or malformed file records nothing: the round may have died before writing it, and an
    unreadable verdict is no reason to fail a round whose PR may already be open."""
    try:
        raw = json.loads((scratch / VERDICT).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"].strip():
        return None
    entry = {key: str(raw.get(key) or "")[:_FIELD_CAP] for key in ("id", "verdict", "summary", "pr")}
    entry["at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry["scratch"] = str(scratch)
    with open(cfg.state / LEDGER, "a") as ledger:
        ledger.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def examined(cfg: Config) -> str:
    """The most recent verdicts as prompt lines, newest first, or `none`.

    Values are JSON-quoted: they are earlier agents' text, and the prompt presents them as data."""
    try:
        lines = (cfg.state / LEDGER).read_text().splitlines()
    except OSError:
        return "none"
    out: list[str] = []
    seen: set[str] = set()
    for raw in reversed(lines):
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(entry, dict) or not entry.get("id") or entry["id"] in seen:
            continue
        seen.add(entry["id"])
        quoted = [json.dumps(str(entry.get(key) or ""), ensure_ascii=False) for key in ("id", "verdict", "summary")]
        out.append("- {}: {} — {}".format(*quoted))
        if len(out) >= LEDGER_SHOWN:
            break
    return "\n  ".join(out) or "none"


def _thread_verdict(body: str) -> str:
    """The blocking verdict a rubric thread root records, or ``""``.

    The engine's meta block is authoritative; the `### … — verdict` header is the fallback for a
    root whose meta is missing or unreadable."""
    meta = META_RE.search(body)
    if meta:
        try:
            runs = json.loads(meta.group(1)).get("runs") or []
        except (ValueError, AttributeError):
            runs = []
        verdicts = {r.get("verdict") for r in runs if isinstance(r, dict)}
        for verdict in _BLOCKING:
            if verdict in verdicts:
                return verdict
        if verdicts:
            return ""
    for line in body.splitlines():
        header = _HEADER_VERDICT_RE.match(line)
        if header:
            return header.group(1)
    return ""


def _summary_and_fix(body: str) -> tuple[str, str]:
    """A rubric thread's one-paragraph summary and its first finding's suggested fix.

    The summary is the paragraph after the `###` header. It names the kind of mistake, which is what
    carries over to the next PR; the finding bullets below it name this PR's lines."""
    lines = body.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("### ")), None)
    if start is None:
        return "", ""
    summary: list[str] = []
    fix = ""
    for line in lines[start + 1 :]:
        text = line.strip()
        if not summary and not text:
            continue
        if summary and not text:
            break
        summary.append(text)
    for line in lines[start + 1 :]:
        if line.startswith("- ") and "_Fix:_" in line:
            fix = line.split("_Fix:_", 1)[1].strip()
            break
    return " ".join(summary), fix


def review_findings(comments: list[dict]) -> list[dict]:
    """Every blocking rubric finding in a PR's review comments that its reviewer has not withdrawn.

    A finding the author later fixed still counts: it was a real mistake, and the point is not to
    make it again. One that a contest cleared does not: the reviewer conceded it, so it teaches
    nothing. The newest contest answer in the thread decides."""
    roots: dict[int, tuple[str, dict]] = {}
    answers: dict[int, dict] = {}
    for comment in comments:
        body = comment.get("body") or ""
        parent = comment.get("in_reply_to_id")
        if parent is None:
            rubric = _RUBRIC_RE.search(body)
            if rubric:
                roots[comment.get("id") or 0] = (rubric.group(1), comment)
        elif "tauceti-reply:" in body:
            newest = answers.get(parent)
            if newest is None or (comment.get("id") or 0) > (newest.get("id") or 0):
                answers[parent] = comment
    out = []
    for cid, (rubric, comment) in roots.items():
        body = comment.get("body") or ""
        verdict = _thread_verdict(body)
        if not verdict or _CLEARED in (answers.get(cid, {}).get("body") or ""):
            continue
        summary, fix = _summary_and_fix(body)
        if not summary:
            continue
        out.append(
            {
                "rubric": rubric,
                "verdict": verdict,
                "summary": summary[:_FIELD_CAP],
                "fix": fix[:_FIELD_CAP],
                "at": str(comment.get("created_at") or ""),
            }
        )
    return sorted(out, key=lambda f: f["at"])


def _load_findings(cfg: Config) -> dict:
    """The findings ledger, keyed by PR number; empty when missing or unreadable."""
    try:
        raw = json.loads((cfg.state / FINDINGS).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, dict) and isinstance(v.get("findings"), list)}


def harvest_findings(cfg: Config, gh, login: str) -> int:
    """Refresh the findings ledger from this login's refactor PRs; return how many PRs were re-read.

    Only PRs whose `updatedAt` moved since the last harvest are re-read, since a PR's review threads
    cannot change without moving it. A PR whose comments cannot be fetched keeps its old entry and
    its old clock, so the next round retries it. Raises GitHubError if the PR list itself fails."""
    ledger = _load_findings(cfg)
    reread = 0
    for p in gh.pr_list(["number", "headRefName", "updatedAt"], author=login, state="all"):
        if not str(p.get("headRefName") or "").startswith(REFACTOR_HEAD_PREFIX):
            continue
        key, updated = str(p.get("number")), str(p.get("updatedAt") or "")
        if ledger.get(key, {}).get("updated") == updated:
            continue
        comments = gh.review_comments(p["number"])
        if comments is None:
            continue
        ledger[key] = {"updated": updated, "findings": review_findings(comments)}
        reread += 1
    if reread:
        path = cfg.state / FINDINGS
        tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=1) + "\n")
        os.replace(tmp, path)
    return reread


def findings(cfg: Config) -> str:
    """The most recent review findings as prompt lines, newest first, or `none`.

    The summary and fix are reviewers' text, so they are JSON-quoted and the prompt presents them as
    data. The PR number, rubric and verdict were matched against fixed patterns when harvested."""
    rows = [
        (finding, pr)
        for pr, entry in _load_findings(cfg).items()
        for finding in entry["findings"]
        if isinstance(finding, dict) and finding.get("summary")
    ]
    rows.sort(key=lambda row: str(row[0].get("at") or ""), reverse=True)
    out: list[str] = []
    seen: set[tuple[str, str]] = set()
    for finding, pr in rows:
        if len(out) >= FINDINGS_SHOWN:
            break
        key = (str(finding.get("rubric") or ""), str(finding["summary"]))
        if key in seen:  # a later review round restating the same finding
            continue
        seen.add(key)
        line = "- #{} {}, {}: {}".format(
            pr,
            json.dumps(str(finding.get("rubric") or "")),
            json.dumps(str(finding.get("verdict") or "")),
            json.dumps(str(finding["summary"]), ensure_ascii=False),
        )
        if finding.get("fix"):
            line += " Fix: " + json.dumps(str(finding["fix"]), ensure_ascii=False)
        out.append(line)
    return "\n  ".join(out) or "none"


def refresh_findings(cfg: Config, gh) -> None:
    """Harvest review findings before a round, without ever failing the round over it.

    The prompt then names whatever the ledger already holds, which is at worst one harvest old."""
    try:
        n = harvest_findings(cfg, gh, me())
    except (GitHubError, Die, OSError) as error:
        log(f"  refactor: could not refresh review findings ({error}); using the last harvest")
        return
    if n:
        log(f"  refactor: re-read review findings on {n} refactor PR(s)")


def exclude_scratch(checkout: Path) -> None:
    """Keep a stray project-root scratch file out of `git add -A`.

    The prompt sends scratch files elsewhere, but the skill's own habit is a project-root
    `scratch*.lean`, and an agent that falls back on it must not commit the file."""
    exclude = checkout / ".git" / "info" / "exclude"
    pattern = "/scratch*.lean"
    try:
        text = exclude.read_text() if exclude.exists() else ""
        if pattern not in text.splitlines():
            exclude.parent.mkdir(parents=True, exist_ok=True)
            exclude.write_text(text + ("" if text.endswith("\n") or not text else "\n") + pattern + "\n")
    except OSError as error:
        log(f"  refactor: could not add {pattern} to {exclude} ({error}); the prompt alone keeps scratch out")


def do_refactor(w, sv, c, opts, bubble) -> int:
    from .work_units import RUBRIC_BUNDLE, _effective_authoring_profile, stage_rubrics

    refs = w.cfg.state / "refs"
    if not fetch_ref(ROADMAP, refs / "roadmap"):
        raise Die(f"fetch {ROADMAP} failed")
    if not fetch_ref(REVIEW, refs / "review"):
        raise Die(f"fetch {REVIEW} failed")
    skill = fetch_pinned(f"https://github.com/{MATHLIB_API_SKILL}", MATHLIB_API_SKILL_REF, refs / "mathlib-api")
    if skill is None:
        raise Die(f"fetch {MATHLIB_API_SKILL}@{MATHLIB_API_SKILL_REF} failed")
    bundle = stage_rubrics(refs / "review", refs / "rubrics")
    scratch = scratch_dir(w.cfg)
    log(f"  refactor: {MATHLIB_API_SKILL}@{skill[:12]}; scratch {scratch}")
    # The agent picks its own target, so the wrapper can only demand that SOME marker is present.
    os.environ["TAUCETI_REQUIRE_TARGET_MARKER"] = "1"
    # Author claims go to this worker's claim namespace, where they take effect, not to canonical.
    os.environ["CLAIM_REPO"] = claims_repo()
    fork = ensure_fork()
    os.environ["TAUCETI_PUSH_REMOTE"] = f"https://github.com/{fork}"
    os.environ.pop("TAUCETI_PUSH_EXPECT", None)  # a fresh branch ⇒ create-only CAS on the fork
    refresh_findings(w.cfg, w.gh)
    subs = dict(
        AGENT=opts.agent_name,
        FORK_OWNER=fork.split("/", 1)[0],
        WORKERID=w.cfg.wid,
        EXAMINED=examined(w.cfg),
        FINDINGS=findings(w.cfg),
    )
    try:
        if bubble:
            mounts = [
                f"{refs / 'roadmap'}:/opt/roadmap:ro",
                f"{refs / 'review'}:/opt/review:ro",
                f"{refs / 'mathlib-api'}:/opt/mathlib-api:ro",
                f"{scratch}:/opt/scratch:rw",
            ]
            if bundle is not None:
                mounts.append(f"{refs / 'rubrics'}:/opt/rubrics:ro")
            prompt = fill_prompt(
                HERE / "prompts" / "refactor.md",
                **subs,
                SKILL="/opt/mathlib-api",
                SCRATCH="/opt/scratch",
                ROADMAP_DIR="/opt/roadmap/TauCetiRoadmap",
                RUBRICS=(
                    f"/opt/rubrics/{RUBRIC_BUNDLE}"
                    if bundle is not None
                    else "/opt/review/rubrics (read every .md file in it)"
                ),
                BIN=wrapper_bin(bubble=True),
            )
            return run_in_bubble(w, TAUCETI, prompt, opts, mounts=mounts, allow_push=fork)
        if not prepare_checkout(w.cfg):
            raise Die("checkout failed")
        exclude_scratch(w.cfg.checkout)
        prompt = fill_prompt(
            HERE / "prompts" / "refactor.md",
            **subs,
            SKILL=str(refs / "mathlib-api"),
            SCRATCH=str(scratch),
            ROADMAP_DIR=str(refs / "roadmap" / "TauCetiRoadmap"),
            RUBRICS=(
                str(bundle) if bundle is not None else f"{refs / 'review' / 'rubrics'} (read every .md file in it)"
            ),
            BIN=wrapper_bin(),
        )
        return run_agent_host(w.cfg.checkout, prompt, _effective_authoring_profile(opts), w.cfg.logdir)
    finally:
        entry = record_verdict(w.cfg, scratch)
        if entry is None:
            log(f"  refactor: no verdict recorded (scratch {scratch})")
        else:
            log(f"  refactor: verdict {entry['verdict'] or '?'} on {entry['id']}: {entry['summary']}")
