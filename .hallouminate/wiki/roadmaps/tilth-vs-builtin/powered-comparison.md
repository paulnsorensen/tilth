---
kind: goal
slug: powered-comparison
roadmap: tilth-vs-builtin
created: 2026-11-07
prereqs: [fix-accuracy-failures, trim-search-output]
---
# Compare tilth with the built-in tools once, with enough power

## Intent

After the fixes, measure tilth against the built-in tools once, with enough power to see a 15% difference in cost per cell.
The [noise audit](../../benchmark-gepa-v3-evolve.md#november-2026-audit-noise-causes) found that 8 test tasks with 3 reps detect only about a 20% effect, and that more tasks cost less than more reps.
The user chose the 15% cost-per-cell target on 2026-11-08 ($83 estimate, cap $100) over the 10% cost-per-correct target first planned (about $440); see the [plan](../../powered-comparison-plan.md).
This comparison replaces the 8-task evolve test split as the gate to ship a tool or prompt change.

## Acceptance

- Before launch, a written plan states the metric (the mean of per-task cost-per-cell deltas, the task as the sampling unit, with the task bootstrap CI of that same mean), the accuracy guard, the decision rule, the task set, and the reps.
- The plan gives a power estimate of at least 0.8 for a 15% effect, computed from the stored per-task variance.
- The user approves the budget before launch.
- Both arms run fresh cells in one run at the same main commit; no stored cell is reused.
- The wiki records the verdict under the plan's decision rule, with its 95% CI and the accuracy guard result.
