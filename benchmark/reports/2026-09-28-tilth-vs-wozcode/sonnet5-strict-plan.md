# Strict Sonnet 5 three-way benchmark

## Accepted contract

The user requests equal tool restrictions for tilth and WOZCODE.
Run gin_edit_render_context on claude-sonnet-5 at high effort, five repetitions per arm.
Use the same task, pinned Gin fixture, trusted grader, and compiled tilth binary as before.

- Native: Read, Edit, Write, Grep, Glob, Bash.
- Tilth: Bash and tilth MCP only.
- WOZCODE: Bash and WOZCODE MCP only, with its plugin hooks active.
- All arms: no delegation; identical Go test/build/vet/gofmt Bash guard.
- No source reads or edits through Bash. Denied attempts remain measured recovery overhead.
- This policy controls tool use. It is not a security sandbox for hostile code.
- Five repetitions per arm; shuffle seed 20260929; 600-second cell timeout; $10 per-cell cap.
- Do not replace timed-out or failed scored cells with extra successful runs.

## Evidence

The strict guard denies a harmless cat request in live Claude CLI probes for both MCP arms.
Both probes then read and edit through MCP and run go test successfully.
Probe streams: .context/sonnet5-strict-tilth-smoke.jsonl and .context/sonnet5-strict-wozcode-smoke.jsonl.
Parent replays both streams through the actual strict auditor; each reports one denied Bash call.
Fresh taste-test: all seven lenses pass.
Integrated gate: 190 tests pass in 101.75 seconds.
Command: GOCACHE=/tmp/tilth-context-go-cache.SA1y6u mise exec go@1.24.7 -- .context/benchmark-venv/bin/python -m pytest benchmark/tests -q.
Log: .context/sonnet5-strict-integrated.log.
Compileall passes. The earlier optional basedpyright invocation is not green and has no baseline attribution.

## Frozen inputs and output

Fixed input hashes: .context/sonnet5-three-way-fixed-inputs.sha256; all match before this run.
Harness hashes: .context/sonnet5-strict-harness.sha256.
WOZCODE: v0.3.92, commit 1c7687322e2e65ae85450160489ad78a3b63cbf0.
Tilth binary SHA256: 67385086492d457aaadea0e2caa225fccb935ac3d653dfb1d1fbe52ddbc0060d.
Valid results: benchmark/results/benchmark_20260928_202801_sonnet5.jsonl.
Valid streams: benchmark/results/streams/20260928_202801/.
Run log: .context/sonnet5-strict-auth-retry-run.log.
The earlier 201559 run is authentication-invalid and remains excluded.
All 15 valid cells finish: tilth 5/5, WOZCODE 5/5, native 4/5 with one timeout.
Final reports: .context/sonnet5-strict-results.md and .context/sonnet5-strict-tool-analysis.md.

The stopped hybrid schedule stays separate: .context/sonnet5-hybrid-stopped-results.md.

## Requested follow-up

After all scored cells, use /Users/paul/.agents/skills/session-analytics/SKILL.md.
Build a separate benchmark-only DuckDB database, not an aggregate of unrelated personal sessions.
Normalize stream session_id to canonical sessionId and preserve full messages for response analysis.
Do not invent missing per-call timestamps.
Use canonical tool tables for usage and raw_entries for responses beyond the 500-character summary limit.
Compare argument shapes, batching, response size/structure, truncation, edit errors, retries, and repeated reads.
Separate measured findings from causal hypotheses.
