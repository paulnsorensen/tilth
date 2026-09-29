# Luna 5.6: corrected edit benchmark

## Verdict

The benchmark now exercises tilth reads and writes. It does not show a performance gain on these two tasks.
All 12 cells complete and pass the task tests. Every tilth cell uses successful batched reads and writes.
No host-skill tool inputs or native-edit fallbacks appear in the six tilth traces.

## Configuration

- Model: gpt-5.6-luna only; reasoning effort: xhigh.
- Tasks: gin_edit_render_cascade and gin_edit_render_runtime.
- Arms: baseline and instructed tilth hybrid; three repetitions each.
- No Claude model calls.
- Result: benchmark/results/benchmark_20260928_131346_luna56.jsonl.
- Traces: benchmark/results/streams/20260928_131346/.
- Tilth binary SHA-256: 78712ae9f83d1f9288f2dbe06f7987e3c9c6e601918aadcabe106cca1b34e214.

## Results

| Metric | Baseline | Tilth |
| --- | ---: | ---: |
| Correct | 6/6 | 6/6 |
| Average seconds | 80.52 | 101.75 |
| Median seconds | 79.86 | 100.64 |
| Total processed context tokens | 1,335,878 | 2,731,343 |
| Tool calls | 52 | 70 |
| Harness-estimated total cost | $0.12577 | $0.24644 |

Tilth averages 26.37% more elapsed time and processes 104.46% more context tokens.
Tilth is faster in one of six matched pairs.
Cost figures are harness estimates from aggregate usage, not account billing. Pricing accuracy is not validated by this run.

| Task | Baseline mean seconds | Tilth mean seconds |
| --- | ---: | ---: |
| Render cascade | 82.93 | 108.22 |
| Render runtime | 78.11 | 95.29 |

## Trace audit

All twelve traces contain turn.completed.
The tilth arm has 39 successful MCP calls.
Eleven of fourteen successful reads contain multiple paths; the largest contains sixteen paths.
Six of seven successful writes contain multiple files.
All write paths target the intended render source files inside their disposable benchmark copies.
The six tilth cells use no native Edit events; their shell calls run Go validation commands.
No skill-access inputs appear in any of the twelve traces.

Eight MCP calls fail: six tilth_diff calls encounter intentionally hidden Git metadata; two search calls submit incomplete continuation hints.
Those failures remain in the measured results. They are not excluded or retried as replacement cells.
The benchmark validates batching within one MCP request, not simultaneous execution of separate tool calls.

## Fixes and verification

- Explicit per-skill disabling removes host and project skills from Codex discovery.
- Developer instructions direct the tilth arm to batch source work through MCP.
- Validity checks reject absent successful MCP usage and detected skill access.
- Raw stdout and Codex stderr remain available for audit.
- The user-approved per-tool approval override permits tilth_write only in the benchmark invocation.
- No global configuration, native shell sandbox, auth, or other MCP approval changes occur.
- MCP approval is not filesystem confinement; tasks target disposable copies by harness design.

Parent verification: 144 benchmark tests pass; Python compileall passes; fresh taste-test passes.
Direct type checking remains non-green: 14 errors and 369 warnings in the wider benchmark files.
Rust gates are not rerun because Rust source does not change.

## Limits

This is a small Gin-only diagnostic with two tasks and three repetitions.
Baseline always runs first in each pair; cache and ordering effects remain possible.
Both arms can hit sandbox build-cache or broader-test restrictions; host task grading passes.
The tilth arm receives explicit tool-use guidance, so this does not measure spontaneous tool adoption.
These observations do not establish that batching alone causes the regression.
