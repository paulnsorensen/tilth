# Benchmark task inventory

Keep the local suite as a tool regression suite. Strengthen its graders before treating its score as coding-task success.

## Snapshot and count correction

The built-in registry holds **61 tasks** at `50f3e46e109c60eda785cd71ca78c998275799c8` (main after #392).[^1]
The opening inventory held **57 tasks** at `18b7534eecde023cb4a70e5d13d7de29178073de`. #280 added `gin_edit_render_context`. #313 to #319 added no task.
Three migration tasks then raised the count to 61: `express_edit_example_start` (#369), `fastapi_edit_security_raise_on_missing` (#370), and `rg_edit_flow_migration` (#371).
`CLAUDE.md` still states 58 tasks, so it lags the registry. Trust the registry.
FeatureBench and SWE-bench Multilingual instances are external tasks. They are not in this registry (see [FeatureBench integration](featurebench-integration.md)).

| Repository | Original tasks | Current tasks | Read/navigation | Edit |
| --- | ---: | ---: | ---: | ---: |
| Synthetic | 11 | 11 | 10 | 1 |
| ripgrep / Rust | 10 | 11 | 6 | 5 |
| FastAPI / Python | 10 | 11 | 5 | 6 |
| Gin / Go | 17 | 18 | 9 | 9 |
| Express / JavaScript | 9 | 10 | 5 | 5 |
| **Total** | **57** | **61** | **35** | **26** |

“Read/navigation” groups the literal `read` and `navigate` task types.
Current capability labels count 12 locate, 18 trace, 23 fix, four debug, and four control tasks.
These are registry labels, not measured difficulty ratings.

## All registered items

The table comes from registry-object introspection. The three migration rows were added after checking their task names, types, and graders at the SHA of the first footnote.
Response strings check answer substrings, not executable behavior.
Workspace tests run inside the candidate workspace.
The base grader only selects that test route for edit tasks with mutations and a test command.[^2]

| Task ID | Repository | Capability | Grader |
| --- | --- | --- | --- |
| `find_definition` | synthetic | locate | response strings |
| `read_large_file` | synthetic | locate | response strings |
| `edit_task` | synthetic | fix | diff pattern |
| `codebase_navigation` | synthetic | locate | response strings |
| `markdown_section` | synthetic | control | response strings |
| `ts_express_middleware_locate` | synthetic | locate | response strings |
| `ts_generic_trace` | synthetic | trace | response strings |
| `java_locate_implementor` | synthetic | locate | response strings |
| `fapi_deps_target` | fastapi | locate | response strings |
| `gin_deps_target` | gin | locate | response strings |
| `grok_rg_matcher` | ripgrep | trace | response strings |
| `control_pkg_manifest` | synthetic | control | response strings |
| `control_changelog_read` | synthetic | control | response strings |
| `control_arith_util` | synthetic | control | response strings |
| `rg_trait_implementors` | ripgrep | trace | response strings |
| `rg_flag_definition` | ripgrep | locate | response strings |
| `rg_search_dispatch` | ripgrep | trace | response strings |
| `rg_walker_parallel` | ripgrep | trace | response strings |
| `rg_lineiter_usage` | ripgrep | trace | response strings |
| `rg_edit_line_count` | ripgrep | fix | workspace tests |
| `rg_edit_line_locate` | ripgrep | fix | workspace tests |
| `rg_edit_preceding` | ripgrep | fix | workspace tests |
| `rg_edit_flow_migration` | ripgrep | fix | held-out replay |
| `fastapi_edit_multi_response` | fastapi | fix | workspace tests |
| `fastapi_dependency_resolution` | fastapi | trace | response strings |
| `fastapi_request_validation` | fastapi | trace | response strings |
| `fastapi_depends_internals` | fastapi | locate | response strings |
| `fastapi_edit_dep_cache` | fastapi | fix | workspace tests |
| `fastapi_edit_response_filter` | fastapi | fix | workspace tests |
| `fastapi_edit_scope_cache` | fastapi | fix | workspace tests |
| `fastapi_edit_security_raise_on_missing` | fastapi | fix | held-out replay |
| `gin_radix_tree` | gin | trace | response strings |
| `gin_client_ip` | gin | locate | response strings |
| `gin_middleware_chain` | gin | trace | response strings |
| `gin_context_next` | gin | locate | response strings |
| `gin_servehttp_flow` | gin | trace | response strings |
| `gin_edit_middleware_skip` | gin | fix | workspace tests |
| `gin_edit_abort_check` | gin | fix | workspace tests |
| `gin_edit_context_reset` | gin | fix | workspace tests |
| `gin_edit_multi_context` | gin | fix | workspace tests |
| `gin_edit_render_cascade` | gin | fix | workspace tests |
| `gin_edit_render_runtime` | gin | fix | workspace tests |
| `gin_edit_render_context` | gin | fix | held-out replay |
| `gin_edit_route_catchall` | gin | fix | workspace tests |
| `gin_edit_route_catchall_nogit` | gin | fix | workspace tests |
| `express_json_send` | express | trace | response strings |
| `express_render_chain` | express | trace | response strings |
| `express_app_init` | express | trace | response strings |
| `express_res_send` | express | locate | response strings |
| `express_app_render` | express | trace | response strings |
| `express_edit_json_type` | express | fix | workspace tests |
| `express_edit_cookie_prefix` | express | fix | workspace tests |
| `express_edit_send_type` | express | fix | workspace tests |
| `express_edit_example_start` | express | fix | held-out replay |
| `express_diff_multi_mutation` | express | debug | workspace tests |
| `fastapi_diff_which_commit` | fastapi | debug | workspace tests |
| `rg_diff_misdirected_error` | ripgrep | debug | workspace tests |
| `gin_diff_comprehension` | gin | debug | response strings |
| `grok_gin_new` | gin | trace | response strings |
| `grok_depends` | fastapi | trace | response strings |
| `grok_context_next` | gin | trace | response strings |

## Keep and strengthen

The current grading mix is 35 response-string checks, 21 workspace-test checks, one diff-pattern check, and four held-out replays.
The opening snapshot has the same mix without the held-out replay tasks.
Each held-out replay copies the candidate source changes into a fresh fixture, injects tests the agent cannot edit, and then runs the candidate tests too.

<speculative> These changes are proposals. No gate blocks task growth, and every paid run needs `--max-usd`.

| Task group | Keep because | Required improvement |
| --- | --- | --- |
| Locate and trace tasks | Cheap detection of search, outline, dependency, and caller regressions | Require correct file/symbol evidence and causal explanation; reject keyword-only answers |
| Four controls | Detect irrelevant tool overhead and broken basic behavior | Report separately; do not use them as evidence of deep coding skill |
| Synthetic `edit_task` | Small edit-protocol smoke test | Replace diff substrings with behavioral tests and unchanged-source rejection |
| Single-bug mutation tasks | Fast diagnosis and regression checks | Replay allowed source changes against protected tests; reject weakened assertions |
| Multi-file and render cascades | Exercise interface and caller consistency | Add independent regression tests, alternate build paths, and incomplete-fix controls |
| Diff/debug tasks | Exercise history and change diagnosis | State whether history is part of the task; protect tests from candidate edits |
| Catch-all with/without Git | Measures a specific history-access effect | Keep as paired variants; do not count them as independent problem families |
| New render-context task | Forward migration with independent assertions | Preserve it as a migration anchor; strengthen process and filesystem isolation |

Do not replace every task with a Gin migration clone.
The suite needs discovery, diagnosis, new behavior, API migration, regression prevention, and build/configuration work.
It also needs error-path, compatibility, and test-authoring coverage.
No single score from this suite establishes coverage of every coding task.

## Evidence boundaries

The registry is the count authority; task names alone do not prove complexity.
The general grader permits candidate-workspace tests. This is weaker than a protected replay grader, not proof of an observed exploit.
The new Gin task improves that boundary but still grades on the host.[^3]

The `analyze.py` power readout is informational only; it gates nothing.
This inventory does not approve execution.
See [benchmark selection and rigor](benchmark-selection-and-rigor.md) and [Gin render-context benchmark](benchmark-gin-render-context.md).

[^1]: Registry introspection of `benchmark/tasks/__init__.py` at `50f3e46e109c60eda785cd71ca78c998275799c8`: 61 tasks; task types edit 26, read 20, navigate 15; repositories gin 18, synthetic 11, fastapi 11, ripgrep 11, express 10. PR #280 added the 58th task. PRs #369, #370, and #371 added the last three.
[^2]: `Task.check_correctness` in `benchmark/tasks/base.py`. The test route needs task type `edit`, mutations, and a test command, so `gin_diff_comprehension` (type `read`) uses response strings.
[^3]: `benchmark/tasks/gin_render_context_tasks.py`. The three migration tasks grade in `express_example_start_tasks.py`, `fastapi_security_migration_tasks.py`, and `rg_flow_migration_tasks.py` under `benchmark/tasks/`.

_Source: Registry introspection and grader inspection at 50f3e46e109c60eda785cd71ca78c998275799c8 · Updated: 2026-10-25 · Supersedes: The 58-task count at 1cbebb313dccce0166d8cfc82856dbe5a7c737a5 for current-main use._
