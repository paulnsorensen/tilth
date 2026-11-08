# Powered comparison plan: tilth against the built-in tools

The ship gate that replaces evolve rounds (goal `powered-comparison` of the [tilth-vs-builtin roadmap](roadmaps/tilth-vs-builtin/index.md)).
The user approved 24 tasks × 3 reps on 2026-11-08: it detects a 15% cost difference with 80% power for about $83 (cap $100).
A 10% target needs about 48 tasks × 5 reps ($276), so the roadmap's 10% acceptance is relaxed to 15% by that decision.

## Design

- Arms: `baseline_stock` (built-in tools) and `tilth_stock` (tilth MCP plus Bash), both on the stock Claude Code prompt with memory off (`prompt_mode: append`). Sonnet, CLI 2.1.310, `--strict-file-tools --bare --max-budget-usd 3`.
- Tasks: the 18 `gepa-v3-long-3` members plus `rg_search_dispatch`, `rg_trait_implementors`, `gin_servehttp_flow`, `sharkdp__bat-2650`, `tokio-rs__axum-1730`, `axios__axios-5316`.
- Metric: paired cost per cell, the task as the sampling unit, a 95% task bootstrap CI (`benchmark/paired.py` method). Accuracy is a separate guard: report the paired accuracy delta and its CI.
- Candidate: main after #450 and the hints change land.
- A dry run at main `30f64208` plans 144 cells: 99 paid, 45 `baseline_stock` cells reused from the 2026-11-02 stock A/B (same key). The reused baseline is from another day; record that as a limit.

## Power estimate

From the stock A/B rows (18 tasks × 3 reps, `tilth_stock` against `baseline_stock`): the per-task delta spread splits into true task-to-task spread and rep noise from resampling each task's reps. Power for a true mean effect d is Φ(|d|/SE − 1.96), SE = √((σ_task² + σ_rep²·3/R)/T).

| Metric | σ_task | σ_rep (3 reps) | 10% effect | 15% effect |
|---|---|---|---|---|
| Cost per correct | 0.14 | 0.32 | 48 × 8: 0.82 ($442) | 24 × 8: 0.86 ($221) |
| Cost per cell | 0.22 | 0.14 | 48 × 5: 0.80 ($276) | 24 × 3: 0.80 ($83) |

- Cost per correct is noisy because pass flips on cheap tasks swing the ratio; cost per cell is not, so more tasks help it and more reps barely do.
- The rep noise comes from resampling only 3 observed reps per task, so it is a lower bound.

_Source: `power.py` in the session scratchpad over `benchmark/results/stock-ab/rows.jsonl`; `goal6.sh` dry run (2026-11-08) · Updated: 2026-11-08 · Supersedes: none_
