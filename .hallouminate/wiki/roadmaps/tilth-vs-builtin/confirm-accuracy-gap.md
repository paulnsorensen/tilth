---
kind: goal
slug: confirm-accuracy-gap
roadmap: tilth-vs-builtin
created: 2026-11-07
prereqs: []
---
# Confirm the accuracy gap on ripgrep-2209 and coreutils-6682

## Intent

The baseline pass rates come from 4 distinct cells per task in one run on 2026-11-05, and the tilth rates come from other runs. With 4 cells, neither difference is significant (Fisher p = 0.12 and 0.56).
Run both arms in one run at main to confirm that tilth agents fail these two tasks more often than agents with the built-in tools.
These tasks are cheap (about $0.04–0.08 per cell), so many reps cost little: 20 reps of both arms on both tasks cost about $5.

## Acceptance

- Both arms (`baseline` and `tilth_forced`) run 20 reps of each task at the same main commit, in one run.
- The run uses the settings of the 2026-11-06 runs: sonnet, CLI 2.1.310 through `TILTH_BENCH_CLAUDE`, `--strict-file-tools --bare --max-budget-usd 3`.
- The user approves the budget before launch, and the spend stays at or below $10.
- The result gives pass counts per arm and task, and a Fisher exact p-value for each task.
- The wiki records the verdict. The gap is confirmed when p < 0.05 on at least one task. When it is not confirmed, `fix-accuracy-failures` closes with no work.
