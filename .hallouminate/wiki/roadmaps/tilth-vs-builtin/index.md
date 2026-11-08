---
created: 2026-11-07
---
# Close tilth's gap to the built-in tools

On the `gepa-v3-long-3` test split, the `tilth_forced` arm scores 0.875. The `baseline` arm (Claude's built-in tools) scores 1.0 by construction.
This roadmap closes that gap with tool fixes, free offline checks, and one paid comparison with enough power.
It replaces evolve rounds and small paired A/Bs, which cannot detect effects of the size we test.

## Where the gap is (2026-11-07)

Per-task seed scores come from `benchmark/results/ab-write-md-seed.jsonl` (12 reps, seed `e6d47cd4`), scored against the baseline cells of the tools-sonnet-r1 run (4 distinct cells per task; its rows file lists each twice). The score is pass rate × baseline cost / tilth cost.

| Task | Seed score | Share of the shortfall | Cause |
|---|---|---|---|
| burntsushi__ripgrep-2209 | 0.502 | 49.6% | accuracy: 6/12 pass, baseline 4/4 |
| uutils__coreutils-6682 | 0.709 | 29.0% | accuracy: 8/12 pass, baseline 4/4 |
| chain:express-edit-6 | 0.871 | 12.8% | cost: $1.42 per cell against $1.24 (+15%) |
| vuejs__core-11739, caddy-6350, express_edit_example_start | 0.929–0.932 | 20.7% | one miss or a small cost difference each |
| chain:rg-edit-6, rg_edit_flow_migration | 1.022, 1.099 | −12.1% | tilth costs less than baseline |

- **Accuracy on two cheap SWE tasks carries 79% of the shortfall.** Across the four tilth arms run on 2026-11-06 (seed, `write.md` winner, #443, #444), ripgrep-2209 passes 21/40 against baseline 4/4 (Fisher exact p = 0.12). coreutils-6682 passes 30/40 (p = 0.56). With only 4 baseline cells, neither difference is significant; goal `confirm-accuracy-gap` measures it. Every tilth miss fails the same held-out test: `regression::r2208` for ripgrep-2209 and `test_mkdir::test_mkdir_parent_mode_skip_existing` for coreutils-6682.
- **The content gap of the stock A/B is mostly closed.** #433–#436 fixed causes 2–5 and 8 of [Stock A/B trajectory diagnosis](../../stock-ab-trajectory-diagnosis.md). `chain:express-edit-6` still costs about 15% more. Cause 7 (four continuation hints on each resolved search hit) is still in the default search output on main `30f64208`.
- The tilth arm in these runs has no preamble. The preamble showed no detectable effect in the [preamble rounds and the stock-prompt A/B](../../benchmark-gepa-v3-evolve.md#november-2026-stock-prompt-ab).

## Rules

- Do not run an evolve round or a paired A/B on the 8-task test split before goal `powered-comparison`. At 8 tasks and 3 reps such a test detects about a 20% effect; the changes tested so far are worth 1–4%.
- Each fix passes a free offline check before any paid run.
- Each paid run states its budget, and the user approves it before launch.

## Order

1. `confirm-accuracy-gap` and `diagnose-accuracy-failures` can run in parallel.
2. `fix-accuracy-failures` follows both.
3. `content-replay-gate`, then `trim-search-output`.
4. `powered-comparison` closes the roadmap.

<!-- HALLOUMINATE:INDEX-START -->
- [confirm-accuracy-gap](./confirm-accuracy-gap.md) — Confirm the accuracy gap on ripgrep-2209 and coreutils-6682
- [content-replay-gate](./content-replay-gate.md) — Make replay the free gate for output-size changes
- [diagnose-accuracy-failures](./diagnose-accuracy-failures.md) — Find why tilth agents fail ripgrep-2209 and coreutils-6682
- [fix-accuracy-failures](./fix-accuracy-failures.md) — Fix the tool causes of the accuracy failures
- [powered-comparison](./powered-comparison.md) — Compare tilth with the built-in tools once, with enough power
- [trim-search-output](./trim-search-output.md) — Cut tilth search output toward the size of Grep output
<!-- HALLOUMINATE:INDEX-END -->
