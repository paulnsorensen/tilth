# Benchmark harness gotchas (Sonnet 5 investigation, 2026-08-08)

Four independent ways the benchmark harness silently produced invalid or
misleading numbers, found while investigating PR #196
(`jahala/tilth#196`). Full forensics in `.cheese/notes/tilth-pr196-sonnet5-audit.md`
and `.cheese/notes/tilth-sonnet5-cost-attribution.md`.

## `--safe-mode` strips ALL MCP servers, including `--mcp-config` ones

The 2026-08-07 sonnet5 run (`benchmark_20260807_075044_sonnet5.jsonl`) compared
three arms that were supposed to differ by which tilth MCP server was attached.
`--safe-mode` disabled every MCP server — including ones passed via
`--mcp-config` — so all three arms ran native-tools-only. Init messages showed
`tools=[5 native], mcp_servers=[]` in every cell; first-turn prompt size was
identical (~8,124 tokens) across arms. Every accuracy/cost delta in that run
was sampling noise (fork-upstream +$0.21 total, bootstrap 95% CI
`[-0.66, +1.13]`). The PR #196 failure-audit and benchmark comments posted
against this run (comments `5223809583`, `5216075559`) rest on the invalid
comparison.

Fix: `--setting-sources ""` for harness isolation instead of `--safe-mode`.

## Guards added on PR #168 (commit `d4d41b5`)

- Per-cell `available_tools`, `mcp_servers`, and `model_usage` recorded in
  every result row.
- `McpUnavailableError` — the harness now aborts fast if a pinned arm's MCP
  server fails to attach, instead of silently degrading to native tools.
- `check_experiment` validates tilth-availability per pinned cell (fails the
  run if a tilth arm didn't actually get tilth attached).
- Per-arm and per-task tool-usage reporting in `analyze.py`.

Verified by a 6-cell haiku smoke run
(`benchmark_20260808_021626_haiku.jsonl`): tilth attached and was called in
both pinned arms.

## `pricing.yaml` drift vs Claude Code's native billing

Sonnet 5 was initially priced in `benchmark/pricing.yaml` using the wrong
rate family, understating computed cost by **-33.7%** vs Claude Code's native
billing. Corrected to the introductory Sonnet-5 rates ($2/M input, $10/M
output, $2.50/M 5m cache writes, $4/M 1h cache writes, $0.20/M cache reads,
`as_of: 2026-08-07`) — residual dropped to -0.50% (remainder attributed to a
haiku sidecar call, decomposable via the new per-row `model_usage` field).
Reports now show a `Δnative` residual line so a future pricing drift is
visible instead of silent.

## Stale installed binary served pre-#151 instructions for weeks

`~/.local/bin/tilth` was a stale build that predated PR #151 (the 2KB
instruction-cap shortening) — it served an 8,688-char instructions blob for
weeks after #151 landed. It was mistaken for current HEAD behavior during an
early measurement pass (the "24,917-char fork surface" reading). Root cause:
version is pinned at `0.8.4` across the whole fork (see CLAUDE.md fork law),
so `tilth --version` cannot distinguish a stale binary from current HEAD.

**Check byte sizes (or rebuild from source), not `--version`, when a fork
binary's prompt surface is in question.**

## Related

- `.cheese/notes/tilth-pr196-sonnet5-audit.md`
- `.cheese/notes/tilth-sonnet5-cost-attribution.md`
- PR #168 (paulnsorensen/tilth): harness + reporting fixes, commits `d4d41b5`, `f8a92c9`

## September 2026 Codex isolation and trusted grading

The September benchmark adds explicit Codex isolation and a forward-migration grader, documented in [Tool Efficiency Report: tilth versus WOZCODE](sources/tilth-versus-wozcode-2026-09.md).[^sept-harness]
These are properties of the extracted harness commit, not claims that every installed runner supports them.
Codex scheduling rejects other providers and `tilth_forced` before inference.
The runner disables discovered skills explicitly; `skip_host_skill_discovery` alone is insufficient in CLI 0.154.0.
It disables skill search, plugins, hooks, apps, and delegation for the measured invocation.
The tilth server is required. A missing successful MCP call or detected skill-access attempt invalidates the run.
Codex lacks Claude's init inventory, so an empty `available_tools` field does not prove MCP absence.
The tilth arm retains native tools and receives explicit MCP-first guidance.
Invocation-only write approval enables `tilth_write`; it neither changes global settings nor confines MCP paths.

The small Luna selection uses two test-graded Gin repairs and excludes read-only, single-file, history-only, and keyword-graded tasks.
It excludes `express_diff_multi_mutation`: the supposedly harmless rename leaves an unresolved `app` reference.
Passing mutation preflight alone does not establish a valid task specification.[^sept-selection]

The larger `gin_edit_render_context` reference changes 20 files across 90 edit sites: 14 production files and six original test files.
It migrates 17 renderers, request-context dispatch, nil-request behavior, cancellation-before-side-effects, and private SSE adaptation.
Cancellation of blocking IO after entry is outside the task contract.
The grader copies candidate source into a clean pinned fixture, restores original assertions, and injects held-out tests after inference.
It accepts new local helpers and tests, then checks the candidate suite separately.
Both default and `nomsgpack` grades run full render/binding suites, selected root regressions, and all-package compilation.
Preflight accepts the reference and rejects incomplete migration, omitted cancellation, premature headers, lost dispatch, and weakened assertions.
The first grader misses `binding/json_test.go:59`; that setup attempt stays excluded.
Unrelated network tests stay outside grading because the pinned Unix-socket readiness test races.
An explicit writable temporary `GOCACHE` prevents unrelated global-cache failures.[^sept-large]

## September 2026 strict arms and invalid attempts

The strict Sonnet benchmark matches tool restrictions, not operating-system confinement.[^sept-strict]
Native retains file tools. Each MCP arm uses only its own file tools and Bash.
All arms share a fail-closed Go test/build/vet/formatting guard, with no delegation.
WOZCODE hooks remain active. Explicit `plugin_woz_code` registration is necessary under `--strict-mcp-config`.
Verify tool inventory and a successful edit before scored inference.
Fresh per-cell Claude configuration copies authentication only and preserves refreshed credentials between cells.
Without an explicit seed, legacy Claude authentication keeps its original path.
Require successful completion and `is_error: false` before grading.

| Excluded attempt | Reason | Disposition |
|---|---|---|
| Luna `20260928_152154` | Grader rejects a valid binding caller migration | Preserve timeout and interrupted trace; fix grader before a fresh schedule |
| Sonnet `20260928_191916` | Tilth uses 47 native edits while WOZCODE has different restrictions | Preserve stopped outcomes; restart all arms under strict tools |
| Sonnet `20260928_201559` | One pass followed by 14 revoked-token errors | Authentication-invalid, not task failures; restart all 15 cells |

The valid strict retry is `20260928_202801`.
Its wrapper checks credential lifetime and stops on authentication failure.
Do not copy an interactive access token and assume it survives an unattended schedule.
Keep credentials out of reports and logs.
Historical test results are evidence for their original tree, not substitutes for testing an extracted branch.

When rebasing this wiki-only PR after #280, retain the benchmark tree from `main`.
The split commit removes earlier benchmark files, not #280's replacement.
Retarget report links to the rebased archive commit, which remains in the PR history.

[^sept-harness]: https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/luna56-fixed-results.md; https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/run.py
[^sept-selection]: https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/luna56-curated-selection.md
[^sept-large]: https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/luna56-render-context-results.md; https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/luna56-render-context-invalid-attempt.md
[^sept-strict]: https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/sonnet5-strict-plan.md; https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/sonnet5-strict-auth-failure.md; https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/sonnet5-hybrid-stopped-results.md

## External harness qualification

The [FeatureBench integration page](featurebench-integration.md) records the native Lite Level 1 runner on main.
It extends these attachment and billing checks to that runner.
Native FeatureBench has failure-denominator, best-attempt, and credential-metadata pitfalls.
The page records the reporting rules and the native admission round trip. This page documents no FeatureBench run.

## Local migration graders: the agent workspace has no installed dependencies

`run._agent_repo` makes each cell's workspace with `git worktree add --detach`. That checkout holds tracked files only.
Gitignored dependencies such as `node_modules/` or `.venv/` are absent, so a grader must not run a project tool from the workspace.
PR #369 first ran `node_modules/.bin/mocha` in the workspace and crashed every real cell with `FileNotFoundError`.
Its preflight hid the bug: the preflight clone symlinks the fixture's `node_modules`.
Grade in a fresh fixture copy, as `express_edit_example_start` now does for both runs. Catch `OSError` as a failure, not a crash.

A grader that shares one cargo target dir across grades must lock it from the mtime refresh through the last cargo command.
Otherwise two parallel grades can build each other's sources. `rg_edit_flow_migration` holds `TARGET_DIR/.grade.lock` for the whole grade (PR #371).
## Related GEPA gotchas

The October 2026 GEPA overhaul gotchas moved to [Benchmark GEPA overhaul gotchas](benchmark-gepa-gotchas.md). That page covers the bare flag, quota events, gepa 0.1.4, and external-task data.

## Skill-plugin arms, `--tasks-json`, `--modes-json` and `--output` (PR #277)

`--tasks-json FILE` runs exactly the tasks in a JSON file (mutation shape with a top-level `repo`, or mined shape without it).
`--modes-json FILE` registers extra `ModeConfig` arms. It excludes `--modes` and `--experiment`.
Both flags refuse `--panel`. `--tasks-json` also refuses a `--tasks` value other than `all`.
`--output PATH` writes the result rows to `PATH` and refuses a directory.
Stream logs go to `<output.parent>/<output.stem>_streams/<run timestamp>/`, so a second run with the same `--output` keeps the first run's logs and trajectories. The reuse store stays global.
The harness keeps `DEFAULT_MAX_BUDGET_USD=1.0` and `CELL_TIMEOUT_S=600`.
The non-panel CLI loop passes `--max-budget-usd` unclamped. Only `run_plan` clamps a cell to the `--max-usd` headroom.
Every documented run command passes `--max-usd`.

### `--modes-json` allowlist

A `--modes-json` entry accepts only `name`, `tools`, `description`, `plugin_dir`, `prompt_mode` and `mcp_config_path` (`benchmark/run.py:1929`).
The loader rejects every other `ModeConfig` field as harness-computed provenance. So no arm can raise the per-cell cap or forge provenance.
It also rejects a taken mode name, a non-list `tools`, and a `prompt_mode` other than `replace` or `append`.
A `plugin_dir` must be absolute and must hold a manifest that declares skills.
A `plugin_dir` cannot combine with `mcp_config_path` (`benchmark/run.py:1916`).
`ArmUnavailableError` (`benchmark/run.py:443`) is the base class for MCP and skill aborts. It stops every later cell of the arm.

### Task validation, grading and fixtures

The loader validates JSON task files and raises `ValueError` with the path, entry and field.
Mutation tasks need `mutations` and `test_command`. Mined tasks need a pytest `test_command`, a nonempty `test_patch`, 40-hex `base_sha` and `head_sha`, and a `repo_url` that is `https://`, `ssh://`, `file://`, `git@host:` or an absolute path (never a leading `-`).
A JSON repo takes an optional `language` (default `python`; a top-level key for mutation files, a per-entry key for mined entries) so `env_fingerprint` selects toolchain probes.
A git failure while bootstrapping a mutation fixture is a `ValueError` that names the fixture.
`register_json_tasks` refuses an empty file and an id collision, including a duplicate id inside one file.
Mined grading restores held-out test files as bytes, with path containment. It handles renames, deletes, pure renames, CRLF and non-UTF-8 content.
It grades from a JUnit report passed through `PYTEST_ADDOPTS`. A skipped test does not count as a pass. The grader times out after 300 s.
Fixture copies are content-addressed: the registry name carries a digest, and `commit_sha` is the copy's root commit.
`RepoConfig.ensure_cloned()` clones in the producer, from one shared `--no-checkout` clone per `repo_url`.
Clone and fetch run with `GIT_TERMINAL_PROMPT=0` and a 900 s timeout. A process that loses the rename race onto a valid clone treats the clone as done.
Preflight uses `ensure_repo_clean` and reports each clone failure per task with its stderr.

`SkillUnavailableError` fails a cell closed when `<plugin>:<skill>` is missing from the `skills` list of the `subtype==init` event.
The init event is not always line 1. `parse.py` keeps `plugins`, `skills` and `plugin_errors` from it.

Live probes (two paid cells, subscription OAuth, `--max-usd 1`):

- Probe 1 used main's plugin argv with `--disable-slash-commands`. The plugin loaded, but `skills` was empty. Fixture: `plugin_probe1_skills_stripped.jsonl`.
- Probe 2 dropped `--disable-slash-commands` for skill arms only. The init event listed `probe:demo`. Fixture: `plugin_probe2_skills_loaded.jsonl`.

The flag change applies only to skill arms (`plugin_dir` without `mcp_config_path`). Woz arms and stock arms keep the flag, and their argv and run keys stay byte-identical.
Plugin cells keep `--setting-sources ""`, never pass real `--bare`, and keep `DISABLE_AUTOUPDATER=1`.
Because skill arms drop the flag, the init event also lists about 19 bundled Claude Code skills (probe 2). The agent can call them.

Mined clones live under `$TILTH_BENCH_DATA` (`data_dir()`), not in the checkout or the agent workdir.
The agent workdir holds no test patch and no `.git` history until grading.

### New result-row fields

The fields stay out of the run key, `CELL_KEY_FIELDS` and `harness_digest`, so stored baselines keep matching.
A reused row keeps its stored fields. Rows stored before PR #277 lack all seven fields, so read them with `.get()`.

Erosion fields (`benchmark/erosion.py`):

- `workspace_diff_lines`: added plus removed lines between a snapshot taken before the agent ran and the workdir after it. Binary changes count 0. Both sides use one filesystem walk (`erosion._walk_files`) and no git index, so an untouched tree diffs to 0, including a nested `git init`. The walk skips these directories: `.git`, `.git_hidden`, `.venv`, `venv`, `.tox`, `.nox`, `.mypy_cache`, `.ruff_cache`, `.pytest_cache`, `__pycache__`, `node_modules`, any `*.egg-info`, and any directory that holds a `pyvenv.cfg` (a virtualenv) or a `CACHEDIR.TAG` (a tool cache). A directory with one of those names is excluded even when the agent creates it. This is a known trade-off: real source in such a directory does not count. Everything else is counted, including the working files of a nested repository and a new directory that holds its own self-ignoring `.gitignore`. Git-ignored files do not count. The ignore rules come only from the `.gitignore` files in the snapshot, never from the agent's tree, so a new or edited `.gitignore` cannot hide work and `.gitignore` changes count. A new `.gitignore` under a directory that the snapshot rules already ignore does not count. Paths go to `git check-ignore` with a `./` prefix, so file names such as `:!x.py` measure. `None` when the diff is not computable or the cell failed.
- `reference_changed_lines`: the task's reference patch size (`Task.reference_changed_lines`, `benchmark/tasks/base.py:158`). Mined tasks read it from the JSON entry (default 0). `None` for tasks without one and for unmeasured rows.
- `lint_ok`: `True` when the task lint command exits 0, `False` when it fails. `None` when the task has no lint command or the lint gave no verdict.
- `lint_reason`: `failed (exit N)` when the lint exits nonzero, `timeout` (300 s) or `unavailable` (command missing or not runnable) when `lint_ok` is `None` for a failed lint. `None` otherwise. A command with a path separator, such as `./scripts/lint.sh`, resolves inside the workspace copy it runs in.

The lint runs on a throwaway copy. It runs offline under `unshare -rn` when the host allows it, and otherwise with a scrubbed environment (`PATH`, `LANG`, `LC_ALL`, temporary `HOME`).
Both measures run before grading, which restores and patches test files.

Skill-arm audit fields (`benchmark/run.py:498`). All three are `None` on non-skill arms and on failure and timeout rows:

- `skill_calls`: the `skill` argument of every `Skill` tool call, in order.
- `non_plugin_skill_calls`: the count of those calls whose name lacks the `<plugin>:` prefix.
- `bundled_skills_exposed`: sorted init-event skills outside the plugin, which are the bundled skills the agent could call.

### easy-cheese #728 compatibility check

The companion `agent_lab_tilth.py` still breaks against this harness. The breaks do not block the merge.

- It passes `--modes-json` together with `--modes`. The two flags stay mutually exclusive.
- It passes `--tasks-json` with `--tasks id1,id2`. The harness refuses a `--tasks` value other than `all`.
- Its mode sets `plugin_dir` and `mcp_config_path`. `load_modes_json` rejects that pair.
- It uses `--models fable`. The `claude-fable-5-1` pricing row and alias stay dropped, as the user decided, until real rates exist.

These breaks from the first check are fixed: `--output` exists, and result rows carry `workspace_diff_lines`, `reference_changed_lines` and `lint_ok`, so its erosion guard can fire on new rows.
The mined task shape, the `skills` and `error` row fields, and `model_alias` match.
The live Haiku cell with the real cook plugin stays open. No spend is approved for it.

_Source: PR #277 at 411e183 · Updated: 2026-10-15 · Supersedes: no historical measurements; narrows general claims to their measured configurations_
