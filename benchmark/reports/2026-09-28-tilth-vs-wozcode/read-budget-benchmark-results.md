# Patched read-budget benchmark

## Verdict

The patched binary fixes the observed read-truncation symptom in this run.
It does not establish an overall speed or correctness improvement.
Native baseline passes three cells. Patched tilth passes two cells and times out on the third.
Keep the timeout in the comparison; do not replace it with another sample.

## Binary and setup

Binary: `/Users/paul/conductor/workspaces/tilth/port-louis/target/release/tilth`.
SHA-256: `67385086492d457aaadea0e2caa225fccb935ac3d653dfb1d1fbe52ddbc0060d`.
The release build includes the uncommitted `src/mcp/tools/read.rs` allocation change.
The build succeeds. A direct edit-mode MCP smoke preserves all content at an exact 792-token estimated budget.
The runner resolves this absolute binary through `TILTH_BIN`.
A live process check confirms this executable runs with `--mcp --edit`.
All tilth result rows record this binary path, including the timeout row.
The local-mode result rows do not populate binary SHA-256; the separate frozen hash file records it.
Binary and source hashes match after the benchmark.

Configuration remains Luna 5.6, xhigh, three repetitions per arm, and a 600-second cell timeout.
The Codex-only runner makes no Claude or OpenCode model calls.
Task: `gin_edit_render_context`, with 20 reference files and 90 edit sites.
The same Go 1.24.7 toolchain and warmed `GOCACHE=/tmp/tilth-context-go-cache.SA1y6u` serve both arms.
Codex CLI remains 0.154.0.
Invocation-only MCP write approval and sandbox settings remain unchanged.
Reference preflight and all negative controls pass before inference.
The canonical Gin fixture remains clean at `d7776de7d444935ea4385999711bd6331a98fecb`.

Command:

```sh
TILTH_BIN="$PWD/target/release/tilth" \
GOCACHE=/tmp/tilth-context-go-cache.SA1y6u \
mise exec go@1.24.7 -- .context/benchmark-venv/bin/python -u benchmark/run.py \
  --runner codex --models luna56 --reasoning-effort xhigh \
  --tasks gin_edit_render_context --modes baseline,tilth --reps 3 --max-cells 6
```

## Results

| Repetition | Native baseline | Patched tilth |
| --- | --- | --- |
| 1 | Pass, 444.812 s | Pass, 403.962 s |
| 2 | Pass, 380.572 s | Pass, 467.374 s |
| 3 | Pass, 349.220 s | Timeout, over 600 s |
| Correct | 3/3 | 2/3 |
| Mean time, timeout included at its lower bound | 391.535 s | At least 490.445 s |
| Observed completed tool events | 114 | 119 |

The timeout-inclusive mean is at least 25.26% slower with tilth.
This lower bound substitutes 600 seconds only for the missing timeout duration; it is not an exact measured duration.
The two completed matched pairs average 412.692 seconds native and 435.668 seconds tilth, a 5.57% increase.
This completed-pair subset excludes the failure and is not the overall verdict.
Observed tool counts include 44 completed tool events from the timeout trace.

The timeout row has no token usage, cost, or duration measurement.
Do not treat these missing fields as zero or compare incomplete arm token totals as complete totals.
For the two completed matched pairs, context totals are 2,797,079 native versus 6,802,091 tilth.
The native total across all three cells is 4,171,662 context tokens.

## Read and write audit

All three tilth traces contain eight read calls each: 24 total.
None contains a read-budget truncation notice, including the incomplete third trace.
The earlier unpatched run has six truncated read calls among 25 reads.
Batch sizes reach 19 paths. This observes the intended symptom reduction, not a controlled causal latency estimate.

The first two tilth cells have no rejected write sections.
The timeout cell has four wholly failed write calls and one partially applied call.
Those calls reject 18 file sections in total.
Two wholly failed calls and the partial call contain literal backslash-n/backslash-t sequences in replacement text.
The next attempts correct these strings to actual newline and tab characters.
Two other calls fail the unread-line guard and trigger additional reads.
These retries remain part of the measurement.

The timeout trace ends after candidate test and compile commands complete successfully.
It has no `turn.completed` event and receives no trusted external grade.
The candidate's test outputs do not justify marking the timeout correct.
The agent also runs an unrelated root suite that encounters a sandbox listener failure.
Raw events lack per-call timestamps, so the trace does not assign an exact duration to each source of delay.

## Evidence

- Results: `benchmark/results/benchmark_20260928_173414_luna56.jsonl`.
- Traces: `benchmark/results/streams/20260928_173414/`.
- Run log: `.context/read-budget-benchmark-run.log`.
- Preflight: `.context/read-budget-benchmark-preflight.log`.
- Frozen binary and input hashes: `.context/read-budget-benchmark-inputs.sha256`.
- Previous comparison: `.context/luna56-render-context-results.md`.

## Limits

This is one synthetic migration with three repetitions and fixed baseline-first arm order.
Old and patched tilth are not interleaved in the same experiment.
Model variation and shared cache state limit before/after causal claims.
No timeout replacement, scope change, or additional inference occurs after inspecting results.
