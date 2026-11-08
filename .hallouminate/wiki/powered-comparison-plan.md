# Powered comparison plan: tilth against the built-in tools

The ship gate that replaces evolve rounds (goal `powered-comparison` of the [tilth-vs-builtin roadmap](roadmaps/tilth-vs-builtin/index.md)).
The user approved 24 tasks × 3 reps on 2026-11-08: it detects a 15% cost-per-cell difference with 80% power for about $83 (cap $100).
A 10% target needs about 48 tasks × 5 reps ($276), so the roadmap goal now records the 15% target.

## Design

- Arms: `baseline_stock` (built-in tools) and `tilth_stock` (tilth MCP plus Bash), both on the stock Claude Code prompt with memory off (`prompt_mode: append`). Sonnet, CLI 2.1.310, `--strict-file-tools --bare --max-budget-usd 3`.
- Tasks: the 18 `gepa-v3-long-3` members plus `rg_search_dispatch`, `rg_trait_implementors`, `gin_servehttp_flow`, `sharkdp__bat-2650`, `tokio-rs__axum-1730`, `axios__axios-5316`.
- Metric: the mean of per-task cost-per-cell deltas, (tilth − baseline) / baseline, with the task as the sampling unit. Report that mean and its 95% task bootstrap CI (the statistic `benchmark/paired.py` bootstraps). Do not pair this CI with the aggregate delta that `paired.py` prints as its point estimate: that is a different statistic.
- Accuracy guard: pooled pass counts per arm over all cells, with a Fisher exact test and a Newcombe 95% interval for the difference, plus the list of tasks whose pass count differs. Do not use the task bootstrap for accuracy: when every task has the same delta (for example all cells pass) it returns a zero-width interval.
- Candidate: main after #450 and #451 land.
- Fresh cells only: the baseline arm is named `baseline_stock_1108`, so its run keys differ from the 2026-11-02 stock A/B and no cell is reused. A dry run then plans 144 paid cells.

## Decision rule (fixed before the run)

- **Tilth costs more:** the CI's lower bound is above 0.
- **Tilth costs less:** the CI's upper bound is below 0.
- **Parity within 15%:** the CI lies inside [−15%, +15%] and contains 0.
- **Inconclusive:** any other CI. Report it as inconclusive; do not round it to parity.
- In every case, an accuracy guard with p < 0.05 against tilth blocks a ship verdict, whatever the cost result.

## Power estimate

From the stock A/B rows (18 tasks × 3 reps, `tilth_stock` against `baseline_stock`): the per-task delta spread splits into true task-to-task spread and rep noise from resampling each task's reps. Power for a true mean effect d is Φ(|d|/SE − 1.96), SE = √((σ_task² + σ_rep²·3/R)/T).

| Metric | σ_task | σ_rep (3 reps) | 10% effect | 15% effect |
|---|---|---|---|---|
| Cost per correct | 0.14 | 0.32 | 48 × 8: 0.82 ($442) | 24 × 8: 0.86 ($221) |
| Cost per cell | 0.22 | 0.14 | 48 × 5: 0.80 ($276) | 24 × 3: 0.80 ($83) |

- Cost per correct is noisy because pass flips on cheap tasks swing the ratio; cost per cell is not, so more tasks help it and more reps barely do.
- The rep noise comes from resampling only 3 observed reps per task, so it is a lower bound.

_Source: `power.py` in the session scratchpad over `benchmark/results/stock-ab/rows.jsonl`; `goal6.sh` dry run (2026-11-08) · Updated: 2026-11-08 · Supersedes: none_
