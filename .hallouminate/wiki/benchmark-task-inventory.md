# Benchmark task inventory

Keep the local suite as a tool regression suite. Strengthen its graders before treating its score as coding-task success.

## Snapshot and count correction

The opening inventory contains **57 tasks** at `18b7534eecde023cb4a70e5d13d7de29178073de`.
PR #280 merges during this session and adds `gin_edit_render_context`.
The publication baseline is **58 tasks** at `9fa37b51fb655294f6718ff4da1a00ba52a68a53`.[^1]
The older “51 tasks” prose in `CLAUDE.md` does not match either registry snapshot.

| Repository | Original tasks | Current tasks | Read/navigation | Edit |
| --- | ---: | ---: | ---: | ---: |
| Synthetic | 11 | 11 | 10 | 1 |
| ripgrep / Rust | 10 | 10 | 6 | 4 |
| FastAPI / Python | 10 | 10 | 5 | 5 |
| Gin / Go | 17 | 18 | 9 | 9 |
| Express / JavaScript | 9 | 9 | 5 | 4 |
| **Total** | **57** | **58** | **35** | **23** |

“Read/navigation” groups the literal `read` and `navigate` task types.
Current capability labels count 12 locate, 18 trace, 20 fix, four debug, and four control tasks.
These are registry labels, not measured difficulty ratings.

## All registered items

The table comes from registry-object introspection at the publication baseline.
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
| `fastapi_edit_multi_response` | fastapi | fix | workspace tests |
| `fastapi_dependency_resolution` | fastapi | trace | response strings |
| `fastapi_request_validation` | fastapi | trace | response strings |
| `fastapi_depends_internals` | fastapi | locate | response strings |
| `fastapi_edit_dep_cache` | fastapi | fix | workspace tests |
| `fastapi_edit_response_filter` | fastapi | fix | workspace tests |
| `fastapi_edit_scope_cache` | fastapi | fix | workspace tests |
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
| `express_diff_multi_mutation` | express | debug | workspace tests |
| `fastapi_diff_which_commit` | fastapi | debug | workspace tests |
| `rg_diff_misdirected_error` | ripgrep | debug | workspace tests |
| `gin_diff_comprehension` | gin | debug | response strings |
| `grok_gin_new` | gin | trace | response strings |
| `grok_depends` | fastapi | trace | response strings |
| `grok_context_next` | gin | trace | response strings |

## Keep and strengthen

The current grading mix is 35 response-string checks, 21 workspace-test checks, one diff-pattern check, and one held-out replay.
The opening snapshot has the same mix without the held-out replay task.

<speculative> Apply these changes only after the execution gate permits task work.

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

The accepted Phase 4 plan gates task growth on a power readout.[^4]
This inventory does not approve execution or replace that plan.
See [benchmark selection and rigor](benchmark-selection-and-rigor.md) and [Gin render-context benchmark](benchmark-gin-render-context.md).

[^1]: [Registry at the publication baseline](https://github.com/paulnsorensen/tilth/blob/9fa37b51fb655294f6718ff4da1a00ba52a68a53/benchmark/tasks/__init__.py); [merged PR #280](https://github.com/paulnsorensen/tilth/pull/280), merged 2026-09-29.
[^2]: `benchmark/tasks/base.py:143-186`, at the publication baseline.
[^3]: `benchmark/tasks/gin_render_context_tasks.py:116-167`, at the publication baseline.
[^4]: `benchmark/PHASE4_PLAN.md:1-29`.

_Source: Registry introspection and grader inspection at 9fa37b51fb655294f6718ff4da1a00ba52a68a53 · Updated: 2026-09-29 · Supersedes: The opening 57-task count for current-main use only._
