# Gin render-context benchmark

Keep `gin_edit_render_context` as a substantial API-migration anchor.
It is closer to a real coding task than keyword navigation or a one-line injected repair.

## Version and purpose

PR #280 merges this task into main on 2026-09-29.
This page describes `9fa37b51fb655294f6718ff4da1a00ba52a68a53`, not an unmerged proposal.[^1]
The fixture pins Gin at `d7776de7d444935ea4385999711bd6331a98fecb`.

The task changes `render.Render` to `Render(context.Context, http.ResponseWriter) error`.
It requires all renderer implementations and direct callers to migrate.
It preserves exported helper signatures and `Context.Render`'s public signature.[^2]

## Coding work exercised

- Propagate the request context, with a background context when the request is absent.
- Handle canceled and expired contexts before changing headers or body.
- Prevent canceled renderers from reading Reader input or executing templates.
- Preserve active rendering, custom content types, and error behavior.
- Adapt external `sse.Event` through a private wrapper without changing the dependency.
- Preserve status, no-body responses, error recording, and abort behavior.
- Migrate existing tests without weakening their assertions.
- Compile all packages and pass selected suites under default and `nomsgpack` builds.

The prompt explicitly names the binding PureJSON caller, SSE seam, build variants, and cancellation boundaries.
This reduces discovery difficulty.
The task does not require interruption of I/O already in progress.[^2]

## Size is not difficulty

The recorded, formatted reference changes 20 files: 14 production files and six test files.
Its line-based measurement reports 90 edit sites, 165 additions, and 72 removals.[^3]
This is broad migration work with repeated edits and several nontrivial integration seams.

FeatureBench's paper reports Level-1 averages of 790.2 solution lines, 15.7 files, and 29.2 functions.
Those are not Lite-only statistics.
“Solution lines” also differs from Gin's added/deleted-line metric.[^4]

| Dimension | Gin render-context | FeatureBench | Pro V2 HARD-51 |
| --- | --- | --- | --- |
| Main work | Forward API and behavior migration | Missing feature implementation; some from-scratch tasks | Difficult real issue resolution |
| Discovery burden | Prompt names important seams | Task-dependent repository discovery | Selected for empirical model difficulty |
| Change breadth | 14 production files in the reference | Often broad feature implementations | Variable; patch size does not define selection |
| Grading | Protected assertions in a fresh host copy, then candidate tests | Official F2P/P2P protocol; adapter fidelity must be checked | Published fresh-sandbox patch replay |
| Our execution evidence | Recorded reference and negative controls | No local model evaluation | No local model evaluation |

The [FeatureBench and Pro sources](sources/benchmark-research.md) define those benchmark properties.
They do not provide a shared difficulty scale.

<speculative> Gin is a medium-complexity integration migration.
It likely requires less new implementation than an average FeatureBench Level-1 task.
It can overlap the lower or middle range of feature work, but this placement is qualitative.
We cannot assign a HARD-51 rank without running comparable agents and budgets.

## What the grader protects

The grader rejects unapproved changed paths, symlink entries, missing files, and patches without source changes.
It copies permitted source changes into a fresh copy of the pinned fixture.
It restores original assertions with mechanical call-signature changes.
It then injects three held-out test files.[^5]

Trusted checks run complete render and binding suites.
Root execution matches `^(TestContext|TestMiddleware|TestHeldOut)`.
All packages compile with `go test -run '^$' ./...`.
The trusted checks and candidate-workspace checks run under both build variants.[^3]

Held-out behavior covers cancellation, context propagation, nil-request fallback, SSE, error/abort behavior, headers, and helper signatures.
The grader does not run every root integration test.
The evidence records a fixed-delay Unix-socket test failure during a separate full-root diagnostic.[^3]

## Recorded preflight, not a new run

The committed evidence records:

- Reference passes; unchanged source fails.
- Incomplete API migration fails.
- Missing cancellation with a weakened candidate assertion fails.
- Header mutation before cancellation fails.
- Lost request-context dispatch fails.
- An unmigrated binding caller fails.
- A legitimate new local helper and test pass.
- The prior Python gate passes 148 tests; two existing basedpyright convention errors remain.

These results come from the committed evidence file.
This documentation session does not rerun that preflight or run a model.

## Remaining rigor gap

A fresh temporary directory is not a separate security sandbox.
The host grader does not establish offline execution or prevent all access to sibling fixtures.
Treat hidden-test confidentiality as an environment requirement, not a consequence of delayed test copying.

<speculative> Keep the current behavioral contract.
Next qualify process isolation, fixture confidentiality, and negative controls under the actual agent tool permissions.
Add other task families rather than many correlated variants of this migration.
See [benchmark selection and rigor](benchmark-selection-and-rigor.md).

[^1]: [PR #280](https://github.com/paulnsorensen/tilth/pull/280).
[^2]: `benchmark/tasks/gin_render_context_tasks.py:93-113`.
[^3]: [Committed benchmark evidence](https://github.com/paulnsorensen/tilth/blob/9fa37b51fb655294f6718ff4da1a00ba52a68a53/benchmark/tasks/gin_render_context_fixtures/evidence.md).
[^4]: [FeatureBench paper, Table 3](https://arxiv.org/html/2602.10975v1).
[^5]: `benchmark/tasks/gin_render_context_tasks.py:21-70,116-167`.

_Source: PR #280 implementation and recorded evidence; FeatureBench paper · Updated: 2026-09-29 · Supersedes: Pre-merge descriptions of the Gin task._
