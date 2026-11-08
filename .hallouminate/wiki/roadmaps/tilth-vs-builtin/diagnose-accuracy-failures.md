---
kind: goal
slug: diagnose-accuracy-failures
roadmap: tilth-vs-builtin
created: 2026-11-07
prereqs: []
---
# Find why tilth agents fail ripgrep-2209 and coreutils-6682

## Intent

The stored trajectories hold 19 tilth failures on ripgrep-2209 and 10 on coreutils-6682, with passing tilth and baseline cells beside them.
Compare them at no model cost.
Find whether a tool behavior causes the wrong patch, or whether the agent reasons wrongly with complete information.
Examples of tool causes: an incomplete or stale view, an edit applied to the wrong lines, a search that hides the relevant code, or truncated output.

Sources (local, untracked): `benchmark/results/ab-write-md-seed.jsonl`, `ab-write-md-winner.jsonl`, `bench-prs-pr443.jsonl` and `bench-prs-pr444.jsonl`, with their `_streams/` directories. The baseline cells are the `baseline` rows in `benchmark/results/evolve/tools-sonnet-r1/rows.jsonl`.

## Acceptance

- Each failing tilth cell has one cause class (tool or reasoning) and a citation of its trajectory file and line.
- The finding names the code that a correct patch changes in each task repo, and the step where each failing agent left the correct path.
- Each tool cause has a minimal repro: a tilth call sequence that shows the bad output on main `30f64208`.
- The findings go into the wiki, beside [Stock A/B trajectory diagnosis](../../stock-ab-trajectory-diagnosis.md).
