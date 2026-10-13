#!/usr/bin/env python3
"""Regression guard for the opt-in `refactor` phase.

Dependency-free; no network. Covers the opt-in gate (want, --skip, the dashboard keys), where the
real run_round dispatches refactor, its backpressure, the verdict ledger that steers later rounds
away from examined declarations, and what do_refactor hands the agent on the host and in Bubble.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tauceti_worker as tc
from tauceti_worker import refactor

fails = 0


def check(message, actual, expected):
    global fails
    ok = actual == expected
    print(f"[{'OK ' if ok else 'XX '}] {message}: {actual!r} (want {expected!r})")
    fails += not ok


def make_cfg():
    home = Path(tempfile.mkdtemp())
    cfg = tc.Config(
        wid="t",
        home=home,
        data_home=home,
        state=home / "state",
        checkout=home / "co",
        store_dir=home / "store",
        sbcache=home / "sb",
        logdir=home / "logs",
        quota_cache=home / "qc",
    )
    cfg.state.mkdir(parents=True)
    return cfg


def pr(number, head_ref):
    return tc.PRInfo.from_json(
        {
            "number": number,
            "body": "",
            "headRefName": head_ref,
            "labels": [],
            "headRepositoryOwner": {"login": "alice"},
            "statusCheckRollup": [],
        }
    )


# --- the opt-in gate --------------------------------------------------------------------------------
check("refactor is a known task", "refactor" in tc.ALLOWED_TASKS, True)
check("refactor is opt-in", "refactor" in tc.OPT_IN_TASKS, True)
check("the unrestricted cascade leaves it out", tc.want([], "refactor"), False)
check("the unrestricted cascade keeps roadmap", tc.want([], "roadmap"), True)
check("--only refactor enables it", tc.want(["refactor"], "refactor"), True)
check("--only review leaves it out", tc.want(["review"], "refactor"), False)
check("--skip does not opt it in", "refactor" in tc.resolve_tasks([], ["review"]), False)
check("--only refactor --skip fix keeps it", tc.resolve_tasks(["refactor,fix"], ["fix"]), ["refactor"])
check("not part of the automatic stage order", "refactor" in tc.AUTO_STAGES, False)
check("no --pr can target it", "refactor" in tc.PR_TASKS, False)
check("the ten digit keys keep their kinds", [tc.KIND_KEYS[k] for k in "1234567890"], tc.ALLOWED_TASKS[:10])
check("refactor has no digit key", "refactor" in tc.KIND_BY_NAME, False)
check(
    "the workers view lists the default phases without it",
    "refactor" in tc.worker_manager._worker_configuration_lines({"spec": {}}, 200)[0],
    False,
)

# --- survey backpressure ----------------------------------------------------------------------------
sv = tc.Survey(worker_id="t")
sv.max_open_prs = 2
tc.survey_refactor(sv, [pr(1, "refactor/a-w1"), pr(2, "roadmap/b-w1"), pr(3, "fix/c")])
check("under the cutoff a round is offered", len(sv.refactor.actionable), 1)
check("only refactor/ branches count", sv.refactor.actionable[0].reason, "1/2 refactor PRs open; opt-in")
sv = tc.Survey(worker_id="t")
sv.max_open_prs = 2
tc.survey_refactor(sv, [pr(1, "refactor/a-w1"), pr(2, "refactor/b-w2")])
check("at the cutoff nothing is offered", sv.refactor.actionable, [])
check("and the reason says why", sv.refactor.suppressed[0].reason, "backpressure: 2/2 refactor PRs open")
idle = tc.Survey(worker_id="t")
idle.roadmap_backpressure = True
idle.refactor.actionable.append(tc.Candidate(0, "", "offered"))
check("an offered refactor never becomes the predicted auto stage", tc._next_auto_stage(idle), None)

# --- run_round: where refactor is dispatched ---------------------------------------------------------
cfg = make_cfg()
worker = tc.Worker(cfg, None, None, tc.Counters(cfg), None, None)
tc.work_units.mirror_creds = lambda _cfg: None
tc.work_units.spread_candidates = lambda cs: list(cs)
tc.work_units.log = lambda msg: None
dispatched = []


def fake_dispatch(stage, w, sv, c, opts):
    dispatched.append(stage)
    return 0


tc.work_units.dispatch = fake_dispatch


def survey_with(*, fix=False, red=False, refactor_ok=True):
    sv = tc.Survey(worker_id="t")
    if fix:
        sv.needs_fix.actionable.append(tc.Candidate(7, "abc"))
    if red:
        sv.red_ci.actionable.append(tc.Candidate(8, "def", "build failed at head"))
    target = sv.refactor.actionable if refactor_ok else sv.refactor.suppressed
    target.append(tc.Candidate(0, "", "offered" if refactor_ok else "backpressure: 8/8 refactor PRs open"))
    return sv


def round_over(make_survey, only=(), prs=()):
    dispatched.clear()
    tc.work_units.survey = lambda *a, **k: make_survey()
    tc.work_units.survey_refactor_only = lambda *a, **k: make_survey()
    opts = tc.RoundOpts(
        only=list(only), agent="codex", work_model="codex", sandbox_host=True, prs=tuple(prs), dry_run=False
    )
    try:
        tc.run_round(worker, opts)
    except tc.NoProgress as e:
        return dispatched[:], str(e)
    return dispatched[:], None


check("an unrestricted round authors roadmap, not refactor", round_over(survey_with)[0], ["roadmap"])
check("--only refactor dispatches it", round_over(survey_with, ["refactor"])[0], ["refactor"])
check(
    "--only fix,refactor tends our PRs first",
    round_over(lambda: survey_with(fix=True), ["fix", "refactor"])[0],
    ["fix"],
)
check(
    "the documented refactor loop repairs a red PR before opening another",
    round_over(lambda: survey_with(red=True), ["rebase", "fix-ci", "fix", "refactor"])[0],
    ["fix-ci"],
)
check(
    "refactor runs ahead of roadmap when both are named",
    round_over(survey_with, ["refactor", "roadmap"])[0],
    ["refactor"],
)
check(
    "at backpressure, a worker naming both falls through to roadmap",
    round_over(lambda: survey_with(refactor_ok=False), ["refactor", "roadmap"])[0],
    ["roadmap"],
)
check(
    "a focused refactor worker at backpressure says why",
    round_over(lambda: survey_with(refactor_ok=False), ["refactor"]),
    ([], "refactor: backpressure: 8/8 refactor PRs open"),
)
check("guarded: a round that opens nothing is no progress", "refactor" in tc.PROGRESS_GUARDED, True)

# A pure refactor round reads only the open-PR list: no per-PR review state.
survey_module = sys.modules["tauceti_worker.survey"]
survey_module.me = lambda: "alice"


class OpenPRs:
    def open_prs(self):
        def raw(number, head_ref, author, draft=False):
            return {
                "number": number,
                "headRefName": head_ref,
                "isDraft": draft,
                "author": {"login": author},
                "labels": [],
                "headRepositoryOwner": {"login": author},
                "statusCheckRollup": [],
            }

        return [
            raw(1, "refactor/a-w1", "alice"),
            raw(2, "refactor/b-w1", "alice", draft=True),
            raw(3, "refactor/c-w1", "bob"),
        ]


narrow = tc.survey_refactor_only(cfg, OpenPRs())
check(
    "the narrow survey counts only our non-draft refactor PRs",
    narrow.refactor.actionable[0].reason,
    "1/8 refactor PRs open; opt-in",
)
check("the narrow survey reads no review state", (narrow.reviewable.actionable, narrow.needs_fix.actionable), ([], []))

# --- the verdict ledger -------------------------------------------------------------------------------
cfg = make_cfg()
check("no ledger yet reads as none", refactor.examined(cfg), "none")
scratch = cfg.state / "s1"
scratch.mkdir()
check("no verdict file records nothing", refactor.record_verdict(cfg, scratch), None)
(scratch / "verdict.json").write_text("{not json")
check("a malformed verdict records nothing", refactor.record_verdict(cfg, scratch), None)
(scratch / "verdict.json").write_text(json.dumps({"id": "", "verdict": "judgment-call"}))
check("a verdict without an id records nothing", refactor.record_verdict(cfg, scratch), None)
for i, (target, verdict) in enumerate(
    [("refactor-a", "judgment-call"), ("refactor-b", "mechanism"), ("refactor-a", "regression")]
):
    (scratch / "verdict.json").write_text(
        json.dumps({"id": target, "verdict": verdict, "summary": f"s{i} __BIN__ " + "x" * 400, "pr": ""})
    )
    entry = refactor.record_verdict(cfg, scratch)
check("a long summary is capped", len(entry["summary"]), 300)
lines = refactor.examined(cfg).split("\n  ")
check(
    "newest first, one line per declaration", [ln.split(":")[0] for ln in lines], ['- "refactor-a"', '- "refactor-b"']
)
check("the newest verdict on a declaration wins", '"regression"' in lines[0], True)
check("values are JSON-quoted data", lines[1].startswith('- "refactor-b": "mechanism" — "s1 __BIN__'), True)
with open(cfg.state / refactor.LEDGER, "a") as ledger:
    ledger.write("garbage\n")
check("a corrupt ledger line is skipped", len(refactor.examined(cfg).split("\n  ")), 2)

# --- scratch directories and the exclude backstop -------------------------------------------------------
cfg = make_cfg()
root = cfg.state / "refactor-scratch"
root.mkdir()
for i in range(tc.REFACTOR_SCRATCH_KEEP + 5):
    (root / f"20260101-0000{i:02d}-1").mkdir()
fresh = refactor.scratch_dir(cfg)
kept = sorted(p.name for p in root.iterdir())
check("pruned to the keep limit, including the fresh one", len(kept), tc.REFACTOR_SCRATCH_KEEP)
check("the oldest go first", "20260101-000000-1" in kept, False)
check("the fresh one is kept", fresh.name in kept, True)
co = cfg.checkout / ".git" / "info"
co.mkdir(parents=True)
(co / "exclude").write_text("# git ls-files --others --exclude-from=.git/info/exclude")
refactor.exclude_scratch(cfg.checkout)
refactor.exclude_scratch(cfg.checkout)
text = (co / "exclude").read_text()
check("the exclude backstop is added once", text.count("/scratch*.lean"), 1)
check(
    "an unterminated last line is preserved", text.startswith("# git ls-files") and "\n/scratch*.lean\n" in text, True
)

# --- do_refactor: what the agent is handed ---------------------------------------------------------------
cfg = make_cfg()
worker = tc.Worker(cfg, None, None, tc.Counters(cfg), None, None)
calls = {}
refactor.fetch_ref = lambda repo, d: d.mkdir(parents=True, exist_ok=True) or True
refactor.fetch_pinned = lambda url, ref, d: calls.setdefault("pinned", (url, ref)) and ref
refactor.claims_repo = lambda: "TauCetiProject/tauceti-claims"
refactor.ensure_fork = lambda: "alice/TauCeti"
refactor.prepare_checkout = lambda c: (c.checkout / ".git" / "info").mkdir(parents=True, exist_ok=True) or True
refactor.log = lambda msg: calls.setdefault("log", []).append(msg)


def host_agent(cwd, prompt, profile, logdir):
    calls["prompt"] = prompt
    scratch = Path(prompt.split("Scratch files live in `")[1].split("`")[0])
    (scratch / "verdict.json").write_text(json.dumps({"id": "refactor-x", "verdict": "judgment-call", "summary": "s"}))
    return 0


refactor.run_agent_host = host_agent


class Opts:
    agent_name = "Codex"
    work_model = "codex"


tc.work_units._effective_authoring_profile = lambda opts: None
for key in ("TAUCETI_REQUIRE_TARGET_MARKER", "CLAIM_REPO", "TAUCETI_PUSH_EXPECT"):
    os.environ.pop(key, None)
os.environ["TAUCETI_PUSH_EXPECT"] = "stale"
check("host round returns the agent's rc", refactor.do_refactor(worker, None, None, Opts(), False), 0)
check("the skill is fetched at the pin", calls["pinned"][1], tc.MATHLIB_API_SKILL_REF)
prompt = calls["prompt"]
check("the prompt points at the fetched skill", f"{cfg.state / 'refs' / 'mathlib-api'}/SKILL.md" in prompt, True)
check(
    "the scratch dir is outside the checkout",
    str(cfg.checkout) in prompt.split("Scratch files live in `")[1].split("`")[0],
    False,
)
check("the marker is required", os.environ.get("TAUCETI_REQUIRE_TARGET_MARKER"), "1")
check("claims go to the worker's namespace", os.environ.get("CLAIM_REPO"), "TauCetiProject/tauceti-claims")
check("a fresh branch push is create-only", "TAUCETI_PUSH_EXPECT" in os.environ, False)
check("pushes go to the fork", os.environ.get("TAUCETI_PUSH_REMOTE"), "https://github.com/alice/TauCeti")
check("the verdict is folded into the ledger", refactor.examined(cfg).startswith('- "refactor-x"'), True)
check("the next prompt would name it", "refactor-x" in refactor.examined(cfg), True)
check(
    "the exclude backstop is in the checkout",
    "/scratch*.lean" in (cfg.checkout / ".git/info/exclude").read_text(),
    True,
)


def bubble(w, target, prompt, opts, *, mounts, allow_push):
    calls["bubble"] = (prompt, mounts, allow_push)
    return 3


refactor.run_in_bubble = bubble
check("bubble round returns the agent's rc", refactor.do_refactor(worker, None, None, Opts(), True), 3)
prompt, mounts, allow_push = calls["bubble"]
check("bubble reads the skill read-only", any(m.endswith(":/opt/mathlib-api:ro") for m in mounts), True)
check("bubble writes scratch to a kept host dir", any(m.endswith(":/opt/scratch:rw") for m in mounts), True)
check(
    "bubble prompt uses the mount paths",
    "/opt/mathlib-api/SKILL.md" in prompt and "/opt/scratch/verdict.json" in prompt,
    True,
)
check("bubble may push to the fork", allow_push, "alice/TauCeti")
check("a round without a verdict says so", any("no verdict recorded" in m for m in calls["log"]), True)

# --- the PR hold switch ------------------------------------------------------------------------------
# Two held PRs in one loop must both survive for the operator to read; neither may reach `gh`.
wrapper = Path(__file__).resolve().parent.parent / "scripts" / "gh-safe-pr-create"
hold_root = Path(tempfile.mkdtemp()) / "held"
hold_env = {k: v for k, v in os.environ.items() if not k.startswith("TAUCETI_")}
hold_env.update(TAUCETI_HOLD_PR_DIR=str(hold_root), PATH="/nonexistent:/usr/bin:/bin")
for n in (1, 2):
    done = subprocess.run(
        [str(wrapper), "--title", f"refactor: {n}", "--body", f"body {n}", "--head", f"alice:refactor/x{n}-w1"],
        env=hold_env,
        capture_output=True,
        text=True,
    )
    check(f"held PR {n} exits 0 without opening", (done.returncode, "NOT opened" in done.stderr), (0, True))
held = sorted(hold_root.iterdir())
check("each held PR gets its own directory", len(held), 2)
check("and keeps its own body", sorted((d / "body.md").read_text() for d in held), ["body 1", "body 2"])

print("FAIL" if fails else "ALL OK")
sys.exit(1 if fails else 0)
