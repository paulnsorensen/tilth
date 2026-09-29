# Luna 5.6 render-context benchmark plan

## Frozen scope before inference

Build and validate `gin_edit_render_context`, then run exactly six measured cells.
The task migrates the Gin render interface and every implementation to accept `context.Context`.
Already-cancelled and deadline-expired contexts return their error before rendering side effects.
In-progress blocking IO cancellation is out of scope.
`Context.Render` passes the request context, with `context.Background()` for a nil request.
Existing output, error handling, and body-disallowed behavior remain unchanged.
`SSEvent` uses a private context-aware adapter for the external legacy `sse.Event` renderer.
The task keeps the external dependency and public `Context.Render` signature unchanged.
Trusted tests adapt direct legacy SSE calls without changing their assertions.
The grader enforces the contract after inference with held-out tests and protected original assertions.
The reference must pass default and `nomsgpack` builds.
The original and deliberately incomplete solutions must fail.
Grade the full render suite and relevant root Context, Middleware, and held-out tests in both build variants.
The unrelated network integration suite is not a per-cell correctness gate.
A pre-measurement parent run exposes `TestUnixSocket` using a fixed five-millisecond readiness wait.
It fails with connection refused on a correct reference; no test assertion is changed or removed.

## Measurement

- Model: `gpt-5.6-luna`, alias `luna56`, effort `xhigh`.
- Runner: Codex CLI 0.154.0 only. No Claude or OpenCode calls.
- Arms: native baseline and instructed tilth hybrid.
- Repetitions: three per arm. Six cells total. No extra inference smoke.
- Schedule: baseline then tilth within each repetition, retaining existing harness behavior.
- Per-cell model timeout: existing 600 seconds.
- Selection and grading freeze before results. Keep all measured failures and timeouts.
- Audit raw traces for real multi-path reads, multi-file writes, failed tool calls, and native fallback.
- Report correctness first, then duration, processed context, tool calls, and batching.
- Token-derived costs remain estimates, not account billing.
- Batch arrays measure grouped file operations, not necessarily simultaneous execution.

## Pins and baseline

- Workspace: `/Users/paul/conductor/workspaces/tilth/port-louis`.
- Base HEAD: `18b7534eecde023cb4a70e5d13d7de29178073de`; existing uncommitted benchmark fixes remain.
- Gin: `d7776de7d444935ea4385999711bd6331a98fecb`; source fixture is clean.
- Go: 1.24.7 through `mise exec go@1.24.7 --`.
- Tilth binary: workspace `target/release/tilth`.
- Binary SHA-256: `78712ae9f83d1f9288f2dbe06f7987e3c9c6e601918aadcabe106cca1b34e214`.
- Python baseline: `.context/benchmark-venv/bin/python -m pytest benchmark/tests -q`: 144 passed in 12.25 seconds.
- No global approval or sandbox changes. Existing invocation-only approval permits benchmark MCP writes.

## Known limits

One synthetic migration on one pinned repository cannot establish general coding performance.
Three repetitions provide a diagnostic, not a stable causal estimate.
Fixed arm order permits cache and ordering effects.
This forward task keeps Git available; unlike the earlier repair tasks, it does not hide history.
Reference size is measured before inference. The previous 15–20 file count is only an estimate.

## Setup correction before the valid six-cell run

Attempt `20260928_152154` is invalid because its grader rejects a legitimate binding-package caller migration.
Its baseline timeout and partial tilth trace remain preserved in the invalid-attempt report.
Original hashes remain in `.context/luna56-render-context-initial-inputs.sha256`.
The corrected task includes `binding/json_test.go` and the full binding regression suite.
It compiles every package under both default and `nomsgpack` builds to detect omitted callers.
No remaining renderer API requirement changes.

The first trace also encounters an unwritable global Go cache.
The corrected invocation supplies a dedicated temporary `GOCACHE` to both arms.
The runner forwards only that additional runtime environment key; approvals and sandbox settings remain unchanged.
The cache is `/tmp/tilth-context-go-cache.SA1y6u`, prewarmed with the clean pinned fixture under both build variants.
No model solution is placed in the cache or candidate workspace.
This environment change addresses build execution, not measured model quality.
The corrected schedule still has three repetitions per arm and the existing 600-second timeout.
