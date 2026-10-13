"""Refactor rounds: A/B-test a design choice in merged code with the mathlib-api skill, then open a PR.

The agent follows the pinned mathlib-api skill (https://github.com/homeowmorphism/mathlib-api) on
code that already exists. It builds the current design and an alternative side by side, runs the
same consumer proofs against both, and opens a PR only when the comparison decides the question.

The comparison happens in a scratch directory outside the checkout. Outside, because
prepare_checkout cleans the checkout every round and a file there could be committed. Kept, because
the variants are the evidence behind the PR and the operator may want to re-run them. Each round
also leaves a one-line verdict, which the worker folds into a ledger so later rounds do not
re-examine a declaration this worker has already judged.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from .agents import fetch_pinned, fetch_ref, fill_prompt, prepare_checkout, run_agent_host, run_in_bubble, wrapper_bin
from .config import Config, Die, log
from .constants import MATHLIB_API_SKILL, MATHLIB_API_SKILL_REF, REFACTOR_SCRATCH_KEEP, REVIEW, ROADMAP, TAUCETI
from .github import claims_repo, ensure_fork
from .paths import HERE

VERDICT = "verdict.json"
LEDGER = "refactor-verdicts.jsonl"
LEDGER_SHOWN = 40  # most recent verdicts named in the next prompt
_FIELD_CAP = 300  # characters kept per verdict field; the ledger is prompt text, not an archive


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
    subs = dict(
        AGENT=opts.agent_name,
        FORK_OWNER=fork.split("/", 1)[0],
        WORKERID=w.cfg.wid,
        EXAMINED=examined(w.cfg),
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
