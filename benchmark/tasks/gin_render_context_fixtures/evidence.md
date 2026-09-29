# Gin render-context benchmark evidence

Fixture: Gin `d7776de7d444935ea4385999711bd6331a98fecb`, clean before preflight.

Run `GOCACHE=/tmp/tilth-context-go-cache.SA1y6u mise exec go@1.24.7 -- python3 benchmark/tasks/gin_render_context_fixtures/preflight.py` from the repository root. The reference generator formats changed Go files before measurement. The reference changes 20 files: 14 production files and six mechanically adapted original test files, including `binding/json_test.go`. A line-level `difflib.SequenceMatcher` measures 90 edit sites, 165 added lines, and 72 removed lines.

The frozen grade runs the complete `./render` and `./binding` suites, root tests matching `^(TestContext|TestMiddleware|TestHeldOut)`, and `go test -run '^$' ./...` compilation under both builds. Root tests still compile as a package. The selection includes `TestContextRenderIfErr`, `TestContextRenderSSE`, and `TestMiddlewareWrite`. It excludes unrelated network integration execution; one full-root diagnostic failed on the pinned Unix-socket test's fixed 5 ms readiness wait.

Preflight results: unchanged source rejects; reference passes trusted and candidate tests under default and `nomsgpack` builds. A new local Go helper and test still pass. The migrated binding caller passes; its unmigrated version rejects. An incomplete renderer API, missing cancellation with a weakened candidate assertion, header mutation before cancellation, and lost request-context dispatch all reject. The grader restores pinned assertions and injects held-out tests only after the candidate run.

Full Python gate: `mise exec go@1.24.7 -- .context/benchmark-venv/bin/python -m pytest -q benchmark/tests` passed 148 tests. Targeted basedpyright leaves two existing local-convention errors on the class-level `capability` attribute override; four implicit import errors were corrected.

The runner allowlists an explicit `GOCACHE` for both arms. The preflight used the writable prewarmed `/tmp/tilth-context-go-cache.SA1y6u`; no model timeout or sandbox setting changed.
