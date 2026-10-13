# Refactor rounds

A refactor round looks for one design choice in already-merged TauCeti code that
a side-by-side comparison shows is worse than an alternative, and refactors it.
The agent follows the [mathlib-api](https://github.com/homeowmorphism/mathlib-api)
skill. It builds both designs in scratch, runs the same consumer proofs against
each, and opens a PR only when the comparison decides the question.

```bash
tauceti work --only refactor                            # one refactor round
tauceti work --loop --only rebase,fix-ci,fix,refactor   # tend our open PRs first, then refactor
```

Keep `rebase` and `fix-ci` in a refactor loop. `fix` only answers reviews, and a
PR whose build is red gets no review. Without them, a refactor PR that conflicts
with main or fails CI just waits for you.

The phase is opt-in. It runs only when `--only` names it, and the full cascade,
`--skip`, and the dashboard's auto loop all leave it out. Every round that can
opens a PR, so as a cascade fallback it would starve roadmap authoring. When
`--only` names both `refactor` and `roadmap`, refactor runs first until its own
backpressure (below).

## Why it exists alongside the review rubrics

The [review rubrics](https://github.com/TauCetiProject/TauCetiReview/tree/main/rubrics)
already enforce the checklist half of the skill on every new diff. `api-design`
covers the characterising API, `@[simp]` normal forms, hidden bodies, and free
data that defeats `@[ext]`. `generality` covers the weakest assumptions, and
`reuse` covers existing Mathlib lemmas.

What no rubric does is build the alternative. Reviewers judge the diff in front
of them and never compile a rival design. So a choice between, say, a `Set` and
an indexed family, `Nat.find` and `sInf`, or an instance and a hypothesis was
merged without anyone running the comparison. That gap is what these rounds
work on.

## When it opens a PR

The skill sorts every verdict into three kinds:
- **mechanism**: a consumer proof breaks or needs a workaround under one design
  and goes through under the other;
- **count**: Mathlib's shape-mates lean decisively, giving the command and the
  numbers;
- **judgment call**: the evidence does not separate the designs.

The round opens a PR for a mechanism, or for a count that a probe confirms as
friction in a real TauCeti consumer. A judgment call, a regression no conversion
at the use site can undo, or an asymmetry that disappears once the missing line
is written ends the round without a PR. The worker logs that as no progress, and
the loop backs off as usual.

The PR reads like any other TauCeti refactor. Its body explains the change,
the consumer proof it helps, the Mathlib precedent with links, and what the old
design did better, if anything. Neither the body, the commit, nor the code
describes the method: the skill, the comparison and the scratch files stay out
of everything public. The PR carries `Roadmap:` attribution taken from the PR
that introduced the declaration, as TauCeti's `AGENTS.md` asks of refactors.

## Evidence stays local

The comparison files never reach the PR. Each round writes them to a fresh
directory under the worker's state, `refactor-scratch/<timestamp>-<suffix>/`. The
worker keeps the newest 20 for you to inspect or re-run with `lake env lean`.
The directory sits outside the TauCeti checkout, which is cleaned every round.
As a backstop, the checkout's `.git/info/exclude` ignores a project-root
`scratch*.lean`.

The agent also leaves a one-line `verdict.json` in that directory. The worker
appends it to `refactor-verdicts.jsonl` in its state, and the next round's prompt
names the 40 most recent declarations so the agent looks somewhere new.

## Coordination

The agent claims `author/<focus>/<id>` in the worker's claim namespace before
building, the same shape roadmap authoring uses. The `<focus>` is the roadmap
area named on the PR's `Roadmap:` line, or `none`. The `<id>` is the slugged
name of the declaration whose design changes, without its `TauCeti.` prefix.
The PR body carries the matching marker,
`<!--tauceti-target:v1 {"focus":"<focus>","id":"<id>"}-->`, and TauCeti's
housekeeping dedups open PRs on that id.

Branches are named `refactor/<slug>-<worker-id>`. The survey counts our open
`refactor/` PRs against `TAUCETI_MAX_OPEN_PRS` (default 8) and stops offering
refactor rounds at the cutoff. Roadmap authoring has its own count.

## The skill pin

Rounds read the skill at the commit pinned in `constants.py`
(`MATHLIB_API_SKILL_REF`), not a branch: its text instructs an agent that pushes
to your fork. To try other text, set `TAUCETI_MATHLIB_API_SKILL_REPO` and
`TAUCETI_MATHLIB_API_SKILL_REF`. A full SHA is verified exactly. A branch name is
fetched fresh every round.

## Holding a PR for review

To read a round's PR before it enters TauCeti's review queue, set
`TAUCETI_HOLD_PR_DIR` on a host round. The PR wrapper then saves the would-be
PR's arguments (`args`) and body (`body.md`) instead of opening it. Each held PR
gets its own timestamped subdirectory there, so a loop can hold several. The branch is still pushed to your fork, and the round reports no
progress because nothing opened. Open it yourself once you are happy with it.
The switch lives in the shared wrapper, so it holds roadmap PRs too. Bubble
rounds don't see it.

Nothing updates a held branch, and CI checks a PR's whole tree with the current
guards. So if main fixes something before you open the PR, the PR can fail on a
file it never touched. Merge main into the branch before opening it, or open it
and let a loop that includes `fix-ci` repair it.
