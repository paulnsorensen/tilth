# Luna 5.6: larger render-context benchmark

## Verdict

The larger task works. All six corrected cells pass the trusted grader.
Tilth uses fewer tool calls, but does not improve elapsed time or processed context in this run.
Three matched pairs all take longer with tilth.

## Task size and contract

Task: `gin_edit_render_context`.
The formatted reference changes 20 files: 14 production files and six original test files.
It has 90 line-diff edit sites, with 165 added and 72 removed lines.
An edit site is a non-equal `difflib.SequenceMatcher` span, not a tool invocation.
The earlier repair tasks each need only three restored source lines.

The task migrates 17 renderer implementations to `Render(context.Context, http.ResponseWriter) error`.
It propagates request context, handles nil requests, preserves normal output, and adapts the external SSE renderer privately.
Already-cancelled and expired contexts must return before rendering side effects.
Blocking IO cancellation after entry is outside the contract.

Trusted grading copies candidate source changes into a clean pinned fixture.
It restores original assertions with mechanical call migration and injects held-out tests only after inference.
It also validates the candidate test suite.
The grade runs full render and binding suites, relevant root regressions, and all-package compilation in both default and `nomsgpack` builds.
Unrelated network integration execution is outside the correctness grade.

## Frozen configuration

- Model: `gpt-5.6-luna` only; reasoning effort: `xhigh`.
- Runner: Codex CLI 0.154.0. No Claude or OpenCode model calls.
- Three repetitions per arm: native baseline and instructed tilth hybrid.
- Agent timeout: 600 seconds per cell.
- Fixed order: baseline then tilth within each repetition.
- Gin pin: `d7776de7d444935ea4385999711bd6331a98fecb`.
- Go: 1.24.7 through mise.
- Tilth binary: workspace `target/release/tilth`.
- Binary SHA-256: `78712ae9f83d1f9288f2dbe06f7987e3c9c6e601918aadcabe106cca1b34e214`.
- Both arms use prewarmed temporary `GOCACHE=/tmp/tilth-context-go-cache.SA1y6u`.
- Existing invocation-only MCP write approval remains unchanged. No global configuration or sandbox expansion.
- Input hashes: `.context/luna56-render-context-inputs.sha256`; all still match after the run.

## Results

| Metric | Baseline | Tilth |
| --- | ---: | ---: |
| Correct | 3/3 | 3/3 |
| Mean agent seconds | 431.49 | 448.65 |
| Median agent seconds | 428.49 | 447.34 |
| Total processed context tokens | 5,977,630 | 8,940,423 |
| Total tool calls | 150 | 116 |
| Harness-estimated cost | $0.46619 | $0.63264 |

Tilth uses 22.67% fewer tool calls, takes 3.98% longer, and processes 49.56% more context tokens.
The cost estimate rises 35.70%; it is not account billing and pricing accuracy is not audited here.
Agent duration excludes external grading.

| Repetition | Baseline seconds | Tilth seconds |
| --- | ---: | ---: |
| 1 | 412.370 | 429.073 |
| 2 | 453.602 | 469.547 |
| 3 | 428.490 | 447.339 |

Result rows: `benchmark/results/benchmark_20260928_154153_luna56.jsonl`.
Raw streams and stderr: `benchmark/results/streams/20260928_154153/`.
Run log: `.context/luna56-render-context-corrected-run.log`.

## Trace audit

All six streams contain a completed turn and no detected host-skill tool inputs.
The three tilth runs use 81 MCP calls, including 25 reads and 23 write attempts.
Seventeen reads contain multiple paths; the largest read has 20 paths.
Twenty write calls apply at least one section; eight of those contain multiple files.
The largest write applies 14 files in one call.
All nonempty write arguments target the corresponding disposable Gin workspace.
No tilth run uses native edit events or shell-based source editing; shell commands run tests and read-only formatter checks.
The canonical Gin fixture remains clean after the run.

Three MCP write calls fail outright: two unread-line edits and one empty batch.
Two other write calls apply some sections but reject another section: one stale tag and one ambiguous text replacement.
These failures, corrections, and their time remain included in the results.
A completed MCP event does not guarantee that every section applies.

Baseline also batches native patches: its three runs contain 21 patch events, including batches of 13, 12, and nine files.
Therefore grouped edits are not unique to tilth.
The test measures batching within a request, not simultaneous execution of separate calls.

## Verification and setup history

Final parent Python gate: `GOCACHE=/tmp/tilth-context-go-cache.SA1y6u mise exec go@1.24.7 -- .context/benchmark-venv/bin/python -m pytest benchmark/tests -q`.
Result: 148 passed in 63.91 seconds.
The preflight accepts the reference and rejects unchanged source, incomplete migration, missing binding migration, omitted cancellation, premature header writes, and lost context dispatch.
A weakened candidate assertion does not rescue broken source.
Python compileall passes. Rust source does not change; Rust gates are not rerun.
Targeted type checking of four new Python files still reports two capability-property override errors from the required local registry convention.
Fresh-context taste-test passes after the root-test selection correction; parent verifies the later binding and cache correction with integrated gates.
A no-model Codex sandbox probe confirms that the temporary Go cache is writable.

Attempt `20260928_152154` is invalid setup evidence, not part of the table.
It contains one baseline timeout and an interrupted tilth cell.
Its trace exposes a valid binding caller migration that the first grader incorrectly rejects.
The corrected reference and grader include that caller before this six-cell run.
See `.context/luna56-render-context-invalid-attempt.md` and the preserved initial input hashes.
No valid measured cell is replaced or excluded from the corrected comparison.

## Limits

This is one synthetic migration, one repository, and three repetitions.
Fixed arm order and shared warmed caches permit ordering effects.
Some agents attempt broader root tests and encounter sandbox listener restrictions; those attempts remain in agent duration.
The earlier tiny-task run differs in task, Git visibility, and cache setup; its 26% slowdown is not a controlled size comparison.
This run demonstrates effective multi-file editing but does not establish a causal batching speed benefit.
