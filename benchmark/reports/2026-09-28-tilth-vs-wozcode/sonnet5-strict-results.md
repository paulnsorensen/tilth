# Sonnet 5 strict three-way results

## Result

All 15 scheduled cells finish. Tilth and WOZCODE each pass 5/5. Native passes 4/5; its fifth cell times out after 600 seconds.
No failed scored cell is replaced.

| Arm | Correct | Mean agent seconds | Mean reported cost | Mean processed context |
|---|---:|---:|---:|---:|
| Native | 4/5 | ≥519.69, including timeout floor | $2.2195, four completed cells only | 7,266,408, four completed cells only |
| Tilth | 5/5 | 351.27 | $1.5137 | 3,959,322 |
| WOZCODE | 5/5 | 321.38 | $1.2529 | 3,230,145 |

The native timeout has no final usage or cost. Those missing values are not zero.
Agent time uses CLI duration_ms for completed cells. It excludes trusted grading.
Processed context sums input, cache creation, and cache reads across turns. It does not measure unique source tokens.
Costs are CLI-reported inference costs. They do not include unreported vendor charges.

## Matched comparison

Repetitions 0–3 complete in all arms. Compare these four matched repetitions for cost and tokens.

| Arm | Agent seconds, total | Processed context | Output tokens | Reported cost | Tool calls |
|---|---:|---:|---:|---:|---:|
| Native | 1,998.437 | 29,065,630 | 154,869 | $8.8778322 | 599 |
| Tilth | 1,423.402 | 16,096,904 | 132,049 | $6.1318890 | 230 |
| WOZCODE | 1,363.303 | 14,151,862 | 123,505 | $5.3496758 | 247 |

Against native, tilth reduces time 28.77%, processed context 44.62%, and reported cost 30.93%.
Against native, WOZCODE reduces time 31.78%, processed context 51.31%, and reported cost 39.74%.
This completed-cell comparison does not remove the fifth native timeout from the correctness result.

Across all five matched MCP repetitions, WOZCODE uses 18.42% less processed context and costs 17.22% less than tilth.
Its mean time is 8.51% lower. Individual repetitions vary; this is not a universal product ranking.

## Contract and identity

- Task: gin_edit_render_context. Same pinned Gin source, task prompt, and trusted grader across arms.
- Model: claude-sonnet-5. Effort: high. Claude Code: 2.1.283.
- Five repetitions per arm. Shuffled repetition blocks use seed 20260929.
- Timeout: 600 seconds per cell. Budget cap: $10 per cell.
- Native: Read, Edit, Write, Grep, Glob, Bash.
- Tilth: own MCP tools and Bash only.
- WOZCODE: own MCP tools and Bash only. Its plugin hooks remain active.
- All arms use the same Go test/build/vet/gofmt Bash guard. No delegation is available.
- Fresh per-cell Claude configuration. No unrelated settings, skills, memory, or sessions.
- Go: 1.24.7. Shared prewarmed GOCACHE: /tmp/tilth-context-go-cache.SA1y6u.
- Gin revision: d7776de7d444935ea4385999711bd6331a98fecb.
- WOZCODE: 0.3.92, revision 1c7687322e2e65ae85450160489ad78a3b63cbf0.
- Tilth: target/release/tilth, SHA256 67385086492d457aaadea0e2caa225fccb935ac3d653dfb1d1fbe52ddbc0060d.

## Verification and limits

Fixed task, binary, and strict harness hashes match after the run. Fixture and plugin checks show no tracked changes.
The integrated benchmark suite passes 190 tests in 101.75 seconds. Compileall passes.
The earlier optional basedpyright check is not green and has no baseline attribution.
No production code changes occur during this final analysis. Tests are not repeated for report-only writes.

Both MCP arms contain no native file-tool fallback. WOZCODE attempts eight unavailable short-name Search calls; all fail before execution.
All observed assistant model records name claude-sonnet-5. No Agent or Task calls occur.
The guard controls tool use, not operating-system isolation. Hook argument rewriting is not fully observable in session logs.

Native suffers eight Glob timeouts and more guard denials across its five traces.
These are part of this measured configuration, not proof of inherent native-tool inferiority.
The strict Bash guard is more restrictive than an ordinary coding session.
One task, five repetitions, shared caches, and product-specific hooks limit causal claims.
The result updates the earlier pessimistic Luna signal. It does not isolate model choice from harness differences.

## Evidence

- Results: benchmark/results/benchmark_20260928_202801_sonnet5.jsonl
- Streams: benchmark/results/streams/20260928_202801/
- Run log: .context/sonnet5-strict-auth-retry-run.log
- Frozen inputs: .context/sonnet5-three-way-fixed-inputs.sha256
- Frozen harness: .context/sonnet5-strict-harness.sha256
- Tests: .context/sonnet5-strict-integrated.log
- Tool analysis: .context/sonnet5-strict-tool-analysis.md

Exclude the stopped asymmetric run, 20260928_191916, from this comparison.
Exclude the authentication-invalid run, 20260928_201559, from this comparison.
Both remain preserved, with separate diagnostic reports. The valid retry starts all 15 cells afresh.
