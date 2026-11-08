---
kind: goal
slug: content-replay-gate
roadmap: tilth-vs-builtin
created: 2026-11-07
prereqs: []
---
# Make replay the free gate for output-size changes

## Intent

`benchmark/evolve/replay.py` sends the tilth calls of a recorded cell to a candidate binary with no model calls. It reports result characters by tool and new errors.
It cannot yet replay the cells that carry the content gap:

- `_cells` selects only `tilth_forced` rows, so it skips the stock-prompt arms.
- A chain row's `trajectory_path` names only the `.turn1` trajectory, so a replay misses sub-tasks 2–6.

Close these gaps, so that a change to tool output can be measured on stored chain cells at no cost.
The diagnosis page also names a third gap: replay uses one persistent server and hides the seen-line reset between chain sub-tasks. #435 keeps seen lines across tilth processes in one checkout, so confirm whether this gap still matters before you work on it.

## Acceptance

- `replay.py` replays a stored chain row through all of its `.turnN` trajectories.
- `replay.py` accepts rows of every tilth arm, not only `tilth_forced`.
- A test replays a two-turn chain fixture and checks the per-tool character totals.
- The wiki records reference totals: a replay at main `30f64208` of the 12 stored seed cells of `chain:express-edit-6` and of `chain:rg-edit-6` in `ab-write-md-seed.jsonl`, as result characters per tool.
