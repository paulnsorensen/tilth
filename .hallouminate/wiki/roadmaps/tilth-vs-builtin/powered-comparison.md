---
kind: goal
slug: powered-comparison
roadmap: tilth-vs-builtin
created: 2026-11-07
prereqs: [fix-accuracy-failures, trim-search-output]
---
# Compare tilth with the built-in tools once, with enough power

## Intent

After the fixes, measure tilth against the built-in tools once, with enough power to see a 10% difference in cost per correct answer.
The [noise audit](../../benchmark-gepa-v3-evolve.md#november-2026-audit-noise-causes) found that 8 test tasks with 3 reps detect only about a 20% effect. It also found that more tasks cost less than more reps: about 15 reps at 24 tasks reach 80% power for a 5% effect.
This comparison replaces the 8-task evolve test split as the gate to ship a tool or prompt change.

## Acceptance

- Before launch, a written plan states the metric (paired cost per correct answer, the task as the sampling unit, `benchmark/paired.py`), the task set, and the reps.
- The plan gives a power estimate of at least 0.8 for a 10% effect, computed from the stored per-task variance.
- The user approves the budget before launch.
- Both arms run in one run at the same main commit.
- The wiki records the verdict with its 95% CI.
