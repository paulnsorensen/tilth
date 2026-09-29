# Invalid larger-task setup attempt

Attempt: `benchmark/results/benchmark_20260928_152154_luna56.jsonl`.
Raw traces: `benchmark/results/streams/20260928_152154/`.

The baseline reaches the unchanged 600-second model timeout.
The next tilth cell starts, then the parent terminates the scheduler and its Codex child.
The parent discovers a grader scope defect while auditing the first raw trace.
The candidate correctly migrates `binding/json_test.go:59`, a real `render.PureJSON.Render` caller.
The reference and grader omit this caller and reject changes outside root/render.
Therefore this attempt cannot compare correctness or performance fairly.
Keep the timeout row and both raw traces; do not merge them into corrected results.
No successful measured result exists in this attempt.

Correction: include binding regression tests, migrate their original calls without changing assertions, and compile every package in both build variants.
The next attempt still uses Luna 5.6 xhigh, three repetitions per arm, and a 600-second model timeout.
The frozen grader version changes because of a proven false-negative path, not a measured speed result.
