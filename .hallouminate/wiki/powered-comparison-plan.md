# Powered comparison plan: tilth against the built-in tools

The ship gate that replaces evolve rounds (goal `powered-comparison` of the [tilth-vs-builtin roadmap](roadmaps/tilth-vs-builtin/index.md)).
The user approved 24 tasks × 3 reps on 2026-11-08: it detects a 15% cost-per-cell difference with 80% power for about $83 (cap $100).
A 10% target needs about 48 tasks × 5 reps ($276), so the roadmap goal now records the 15% target.
**Verdict (2026-11-09): parity within 15%.** Tilth's mean per-task cost delta is +5.1% (95% CI −3.3% to +14.9%) and the accuracy guard does not block (66/72 against 70/72, p = 0.275).

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



## Verdict (2026-11-09): parity within 15%

The run at main `69581207` (after #448, #450, #451) planned 144 cells, ran all of them fresh (0 reused), and spent $61.24 (budget `goal6-powered`). Rows: `benchmark/results/goal6-powered.jsonl` (local, untracked).

| Measure | Result | Rule outcome |
|---|---|---|
| Mean per-task cost-per-cell delta | +5.1%, 95% CI [−3.3%, +14.9%] | parity within 15% |
| Accuracy (pooled) | tilth 66/72 (91.7%), baseline 70/72 (97.2%); difference −5.6%, Newcombe 95% [−14.5%, +2.6%], Fisher p = 0.275 | does not block |
| Mean cost per cell | tilth $0.430, baseline $0.421 | — |

- The CI's upper bound sits at +14.9%, just inside the 15% band. The run does not show that tilth costs less; it bounds the extra cost at about 15%.
- Against the 2026-11-02 stock A/B on the same statistic (18 tasks): +10.7%, CI [−1.0%, +23.1%], inconclusive. The point estimate fell and the interval narrowed, but the two runs differ in tasks and day.
- Largest tilth costs: `vuejs__core-11739` +75%, `burntsushi__ripgrep-2209` +59%, metaflow +32%, `chain:express-edit-6` +15%. Largest savings: `fastapi_edit_security_raise_on_missing` −32%, `caddy-6350` −22%.
- Tilth misses: `sharkdp__bat-2650` 0/3, all three at the cell timeout. Both arms spend 12–23 minutes building the crate; the tilth agents then ran the full `cargo test` or the integration tests, while the baseline agents mostly ran `cargo test --lib` or a filtered test. The other misses (`tokio-4867`, `axum-1730` one each in both arms, `coreutils-6682` one for tilth) are wrong patches.
- Limits: 3 reps per task; a 15% band, not 10%; the strict hook blocked `sleep`/`until` wait loops in both arms, which costs turns but is equal across arms.
- Next levers by evidence: the per-task outliers above (vuejs, ripgrep-2209) and the fixed prefix (about 27% of the chain context gap, see [Stock A/B trajectory diagnosis](stock-ab-trajectory-diagnosis.md#one-whole-file-per-search-2026-11-08)). Measure each with replay first, then a paid run on the affected tasks.

### Continuation: hill-climb loop (2026-11-15)

The user chose a hill-climb loop after this verdict: one lever per iteration, a free deterministic gate, a paid check only on affected tasks (up to $10 per lever, baseline cells reused), and a fresh 24 × 3 comparison after about four levers with explicit approval.

- First thread `mcp-prefix`: metric `mcp_prefix.bytes` from `scripts/mcp_prefix_bytes.py` (instructions + served tool descriptions + compact input schemas), 10704 bytes at `9f9ba1f3`, ratcheted in `perf/ratchet/mcp-prefix.json` and checked by `scripts/ratchet_gate.py` in CI.
- Why the prefix: in this run the per-task first-call context delta is 1951–1958 tokens, the tilth prefix as tokenized. Over 16.1 API calls per cell at the cache-read price of the cost model below ($0.20/Mtok) it costs about $0.0064 per cell, 1.5% of tilth cost.
- Cost model: price ratios to the input price (cache write 1.25, cache read 0.1, output 5) with the input price fitted to `model_usage.costUSD` give $2.02/Mtok input for `claude-sonnet-5-5`. The model reproduces the arm means ($0.416 vs $0.421 baseline, $0.430 vs $0.430 tilth). Tilth's cost per cell is cache reads 36%, output 35%, cache writes 30%. Against the baseline, tilth pays +$0.024 for cache reads and +$0.005 for cache writes and saves $0.015 on output.
- Bound: removing tilth's whole cache-read excess makes it about 2.4% cheaper than the baseline. Tool-output levers each move about 1–2%: the prefix is about 26% of the cache-read gap, search JSON overhead (status, completeness, diagnostics, offsets, escapes) is about 9% of search bytes, and comment lines are 21% of search `core` bodies. A marked gain needs fewer output tokens or API calls, not smaller tool output.
- Ceiling of the prefix alone: deleting it saves about 1.5% of tilth cost. Most prefix bytes are model-facing text or validation keywords, so a material cut changes wording or removes a check. Lever 2 (#456, 10704 → 10524 bytes) removed stale hint text and repeated `cwd` text.
- Not levers: `maximum: 4294967295` on integer op fields is the #116 client-side check; the six goal 6 `InputValidationError`s are raw control characters in model-written JSON, which the host rejects before tilth sees the call.
- vuejs (+75%): the tilth agents wrote and ran throwaway repro specs (rep 0: 15 `tilth_write` and 14 `Bash` calls, 25.9k output tokens against 9.0k). Two of those writes failed: `create_file` on a file the agent had just created, then an untagged `replace` of it. The agent then switched to delete and recreate under a new name.
- ripgrep-2209 (+58%): consistent across reps ($0.125 against $0.079). The tilth agents ran extra verification (full `cargo test --all`, `git stash` reruns, a scratch fixture through `cargo run`), with 11–13 API calls against 7–9. File creation per cell is equal across arms (0.44 tilth, 0.47 baseline), so the instruction that tells agents to write scripts with `tilth_write` does not explain it.
- Replay as a lab metric: `replay_cell` on a Rust external task admits it through `external.resolve_task`, which runs its grading build in `/tmp/tilth-external-grade-*` (several GB each). Use local fixture tasks for free replay metrics.

## Power estimate

From the stock A/B rows (18 tasks × 3 reps, `tilth_stock` against `baseline_stock`): the per-task delta spread splits into true task-to-task spread and rep noise from resampling each task's reps. Power for a true mean effect d is Φ(|d|/SE − 1.96), SE = √((σ_task² + σ_rep²·3/R)/T).

| Metric | σ_task | σ_rep (3 reps) | 10% effect | 15% effect |
|---|---|---|---|---|
| Cost per correct | 0.14 | 0.32 | 48 × 8: 0.82 ($442) | 24 × 8: 0.86 ($221) |
| Cost per cell | 0.22 | 0.14 | 48 × 5: 0.80 ($276) | 24 × 3: 0.80 ($83) |

- Cost per correct is noisy because pass flips on cheap tasks swing the ratio; cost per cell is not, so more tasks help it and more reps barely do.
- The rep noise comes from resampling only 3 observed reps per task, so it is a lower bound.

_Source: `power.py` in the session scratchpad over `benchmark/results/stock-ab/rows.jsonl`; `goal6.sh` dry run (2026-11-08); the goal 6 run rows `benchmark/results/goal6-powered.jsonl` and `goal6_verdict.py` (2026-11-09) · Updated: 2026-11-09 · Supersedes: none_
