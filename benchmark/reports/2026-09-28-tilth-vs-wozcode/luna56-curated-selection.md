# Luna 5.6 edit-only batching benchmark

Selection date: 2026-09-28. Selection precedes measured results.

## Contract

Use only gpt-5.6-luna with xhigh reasoning through Codex headless.
Use three repetitions per task and arm.
Compare baseline with optional tilth MCP access.
Run two tasks, two arms, and three repetitions: 12 cells.
Do not make Claude calls.

## Included tasks

- gin_edit_render_cascade: helper contract plus caller changes across files. Compilation and four renderer tests grade the repair.
- gin_edit_render_runtime: independent Content-Type regressions in json.go, text.go, and xml.go. The YAML test guards unchanged behavior.

Both tasks pass native preflight: clean tests pass and injected mutations fail.
See .context/luna56-preflight.log, lines 60–61.

## Exclusions

Exclude every read-only task.
Exclude single-file and same-function edits, including gin_edit_multi_context and fastapi_edit_multi_response.
Exclude express_diff_multi_mutation: its supposedly harmless rename leaves an unresolved app reference.
Exclude history-only repairs and keyword-graded edits.

## Interpretation

This is a targeted Gin-only diagnostic, not a representative general benchmark.
Three repetitions provide limited evidence and no strong statistical claim.
Report correctness, wall time, tokens, estimated cost, and actual batched file calls.
Distinguish batched path arrays from concurrent calls; neither follows from MCP availability alone.
Optional native tools remain available in the tilth arm.
The local A/B scheduler runs baseline before tilth within each repetition; cache and ordering effects remain possible.

## Command

```sh
TILTH_BIN="$PWD/target/release/tilth" mise exec go@1.24.7 -- \
  .context/benchmark-venv/bin/python -u benchmark/run.py \
  --runner codex --models luna56 --reasoning-effort xhigh \
  --tasks gin_edit_render_cascade,gin_edit_render_runtime \
  --modes baseline,tilth --reps 3 --max-cells 12
```

## Live run

Started 2026-09-28 at 11:56 local time.
Results: benchmark/results/benchmark_20260928_115611_luna56.jsonl
Streams: benchmark/results/streams/20260928_115611/
Progress log: .context/luna56-xhigh-curated.log
Execution session: 34863.
Binary SHA-256: 78712ae9f83d1f9288f2dbe06f7987e3c9c6e601918aadcabe106cca1b34e214

The first baseline cell passes the host correctness gate in 328,445 ms.
The second cell, the first tilth arm, is running at this checkpoint.
Do not infer a performance result from one completed cell.
The first agent's broader Go suite hits sandbox cache and socket restrictions.
Its focused render tests pass; host grading also passes.
