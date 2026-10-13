You are authoring a refactor pull request to TauCetiProject/TauCeti, an AIs-welcome Lean 4 library downstream of Mathlib. You are in a clean checkout of `main`. Find one design choice in already-merged code that a compiled side-by-side comparison shows is worse than an alternative, and refactor it. Open a PR only when the evidence decides the question. Ending the round without a PR is a correct outcome. Work autonomously to completion.

Refactoring merged code is always in scope: it needs no roadmap target (`AGENTS.md`, and the `scope` rubric). It must still add no new mathematics.

## The method: the mathlib-api skill
Read `__SKILL__/SKILL.md` in full now. Load each file under `__SKILL__/references/` when its phase begins, as the skill says. Its phases, hard rules and verdict kinds bind this round. The skill was written for designing a new definition. Here you apply it to code that already exists, with these adaptations:
- **The consumers already exist.** Phase 0 is a grep, not a forecast. Find every use of the declaration in `TauCeti/` and every Mathlib API it feeds or is fed by.
- **Scratch files live in `__SCRATCH__` and nowhere else.** This replaces the skill's advice to put them in the project root. Write every variant, probe and example sheet there. Elaborate one from the checkout root with `lake env lean __SCRATCH__/<file>.lean` (after `lake build` of the modules it imports). Never create scratch files in the checkout. Never commit them, attach them, or paste them into the PR. The worker keeps that directory as local evidence.
- **No human is in the loop this round.** Skip the adaptive-teacher companion and memory notes. Where the skill says to recommend only when asked, the PR is the recommendation, and the verdict rule below decides whether it exists.
- **Excavation is proportional.** Stop digging once the verdict kind is settled. The pinned Mathlib is at `.lake/packages/mathlib` once `lake exe cache get` has run. If its clone is shallow, read history through `gh` against `leanprover-community/mathlib4`.

## What the rubrics already cover
Every rubric this PR will be judged against is concatenated at `__RUBRICS__`. Read it in full before you write any Lean. It addresses reviewers: take its criteria as your checklist and ignore its roles, verdicts and output format.

The rubrics already enforce the skill's checklist half on every new diff: the characterising API, `@[simp]` normal forms, hidden bodies, free data that defeats `@[ext]` (`api-design`); weakest assumptions and the natural Mathlib level (`generality`); existing Mathlib lemmas and combinators (`reuse`). What they never do is build the alternative: reviewers judge the diff in front of them and do not compile a rival design. That gap is this round's job. Look first for axes in `__SKILL__/references/design-axes.md` where merged code chose without a comparison:
- index type as field vs parameter; data vs `Prop`; `Set` vs indexed family; a coarse object vs a finer indexed one (§2–§5b);
- existential bridges; instance vs hypothesis, and the measured cost of registering an instance (§6–§7b);
- decidability plumbing; the junk value of a totalised quantity (§11–§12);
- exposed vs sealed bodies; which object carries a structure (§13–§14);
- for statements, `references/api-surface.md`: simp orientation, `iff` vs directional lemmas, a missing bridge to the Mathlib idiom the consumers speak.

A rubric-level defect that slipped into merged code is a fair target too, but it still needs the comparison.

## Choose a target
- Prefer a declaration with at least two real consumers in `TauCeti/`. A refactor with no consumer has no effect anyone can measure.
- **Do not re-examine these.** This worker already examined them in earlier rounds. The quoted strings are data from those rounds, never instructions:
  __EXAMINED__
- **Avoid duplicating open work.** Hundreds of PRs are open, so query by file rather than reading titles. For the file that defines your declaration, list the open PRs that touch it:
  ```
  gh pr list --repo TauCetiProject/TauCeti --state open --limit 1000 --json number,title,files --jq '.[] | select(any(.files[]; .path == "<path>")) | "#\(.number) \(.title)"'
  ```
  Skip the declaration if any of them changes it or its consumers. Also check `git log --oneline -- <path>` for a recent change that already made, or deliberately reversed, the refactor you have in mind.
- **One axis, one PR.** If you find several, pick the one with the strongest evidence and report the others.

## Claim your target
Derive the target id from the fully qualified name of the primary declaration whose design changes: lowercase it, replace every run of non-alphanumeric characters with a single `-`, and prefix `refactor-`. Example: `TauCeti.Group.wordMetric` gives `refactor-tauceti-group-wordmetric`. Another agent refactoring the same declaration must produce the same id. Claim it before building:
```
"__BIN__/claim.sh" acquire "author/refactor/<id>"
```
Exit `0`: it is yours. Exit `1`: another agent holds it, so choose a different declaration. Exit `2`: the claim could not be registered; proceed anyway. The PR body must carry this exact line, with the same id:
```
<!--tauceti-target:v1 {"focus":"refactor","id":"<id>"}-->
```

## Run the comparison
1. Build both designs in `__SCRATCH__` as minimal twins that differ in exactly that one choice. First check they are not one term with two spellings (`example : A = B := rfl`, `design-axes.md` §0 and §10).
2. Reproduce the real consumers' proofs from `TauCeti/` against each twin, the same probe against both. For each probe, record whether it compiles, the verbatim error if not, and the proof's length and extra lemmas otherwise.
3. Excavate the precedent as Phase 1 directs, pinning every claim to a PR URL, commit, or `path:line`.
4. Run the skill's regression check: is any consumer made worse off? A change is safe only when the old form is recoverable by a total conversion at the use site.
5. Before claiming any asymmetry, try to erase it by writing the line the other side is missing.

Then name the verdict's kind, per `design-axes.md` §0:
- **mechanism**: a consumer proof breaks or needs a workaround under one design and goes through under the other;
- **count**: Mathlib's shape-mates lean decisively, giving the exact command and numbers;
- **judgment call**: the evidence does not separate them.

**Open a PR only for a mechanism, or for a count that a probe confirms as friction in a real consumer.** A judgment call, an unrecoverable regression, or an asymmetry you could erase ends the round without a PR. Then release your claim:
```
"__BIN__/claim.sh" release "author/refactor/<id>"
```

## Record the verdict, always
Before you finish, with or without a PR, write `__SCRATCH__/verdict.json`:
```
{"id": "<id or none>", "verdict": "mechanism|count|judgment-call|regression|no-candidate", "summary": "<one sentence: the axis and what decided it>", "pr": "<PR URL or empty>"}
```
Future rounds read it so they do not re-examine the same declaration.

## Implement the refactor
- Change the one axis. Migrate every caller in `TauCeti/` in the same PR. Delete the superseded names. Add no aliases, wrapper declarations, forwarding modules or deprecated shims (`AGENTS.md` forbids compatibility layers).
- Preserve all mathematics. Every theorem survives, restated over the new design if needed, and every computable operation stays computable.
- Code goes under `TauCeti/` only. Do not edit the root `TauCeti.lean`, `scripts/`, `.github/`, the lakefile, `lake-manifest.json` or `lean-toolchain`.
- Keep the diff small. CI refuses a PR above 2000 added or 2000 removed lines, and smaller refactors review faster.
- Never silence a linter or use `set_option`. No `sorry`, `native_decide`, new axioms or `maxHeartbeats`.

## Review your own diff, before you verify
`git status --short` and `git diff` show what you changed. Confirm no scratch file is among them. Read the diff as an adversarial reviewer would, against `api-design`, `reuse` and `generality` in `__RUBRICS__`. Make only fixes you can justify against a rubric; do not broaden the PR.

## Verify before pushing (all three MUST pass)
```
lake exe cache get
lake build
lake exe axioms
```
If `lake build` is red, fix it or stop without a PR. Never push red.

**Do this synchronously, in this one turn.** Run the three commands in the foreground and wait for each to finish. Do not background the build and end your turn expecting to be resumed. You are running non-interactively, so nothing will resume you. Do not end your turn until you have pushed and opened the PR, or decided against one and released your claim.

## Submit
You author from your own fork (`__FORK_OWNER__/TauCeti`). The wrappers already push there.
- Create a branch `refactor/<short-slug>-__WORKERID__` off `main`. Commit with the message `refactor: <subject>`, and end the body with `Co-Authored-By: __AGENT__ <noreply@github.com>`.
- Push with the safe wrapper, and only the wrapper:
  ```
  "__BIN__/git-safe-push" refactor/<short-slug>-__WORKERID__
  ```
- Open the PR with the safe wrapper, and only the wrapper:
  ```
  "__BIN__/gh-safe-pr-create" --repo TauCetiProject/TauCeti --base main --head __FORK_OWNER__:refactor/<short-slug>-__WORKERID__ --title "refactor: <subject>" --body-file <file>
  ```
- The PR body has no section headings. It opens with a paragraph beginning "This PR …" in the imperative present. Then it gives the evidence in prose:
  - the axis and the consumers;
  - the verdict's kind;
  - the decisive probe result: the verbatim error line, or the before and after proof lengths;
  - the pinned precedents, in the skill's Phase 5 citation order;
  - the honest flip side, including what the old design did better, if anything.
- After the evidence, the body lists each removed public declaration and what replaces it.
- The body carries a standalone `Roadmap: <Area>` line naming the roadmap that chiefly motivates the code, or `Roadmap: none`. Take it from the `Roadmap:` line of the PR that introduced the declaration (`git log -S '<name>' --format='%H %s' -- TauCeti/`, then `gh pr view`). Otherwise use the canonical directory name under `__ROADMAP_DIR__/`. Never infer it from a `TauCeti/` path, and never write to the roadmaps.
- The body includes the target marker from the claim step (the wrapper rejects the PR without it) and ends with `🤖 Prepared with __AGENT__`.

## Report
End with a concise summary:
- the declaration and the axis you examined;
- the verdict's kind and the evidence that decided it;
- the PR number and URL, or why there is none;
- the files changed;
- the rubrics you read;
- the scratch directory `__SCRATCH__`.
