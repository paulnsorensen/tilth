# Benchmark GEPA loop invariants and guards

The benchmark GEPA overhaul adds a result store, native external tasks, a locked panel, a calibrated judge, and a GEPA evolution loop under `benchmark/`.
This page lists the invariants and guards a future change must keep. Code citations match the layer 6 head of PR #319 (diff base `085c44b`).
The commits `e42d451`, `70466b9`, and `e77a567` exist only in the stack. Fetch them through `refs/pull/319/head`.
The reasons behind the guards, the goals, and the task picks are in [ADR: benchmark GEPA overhaul](adr/benchmark-gepa-overhaul.md).
The umbrella and child specs that held the contracts are removed from the stack. The ADR page keeps their durable content.

## Cross-curd invariants

The umbrella spec states six invariants, and `benchmark/tests/test_invariants.py` tests them.

- The only score is grader correctness. A contaminated rollout counts as incorrect (`correct()`, `benchmark/evolve/loop.py:121-126`).
- No optimizer-side model reads grader inputs. Reflective records keep `correct`, `pass_rate`, and F2P/P2P counts. They drop `correctness_reason`, ground-truth strings, gold patches, and held-out test source or output. Text the agent itself produced stays.
- One `--max-usd` ceiling and one OAuth guard cover agent cells, judge calls, reflection calls, and proposer calls.
- Baselines are reused, never silently re-bought.
- Nothing runs in a container.
- A winner lands only as an unmerged draft PR that changes `src/**`, `prompts/**`, and the regenerated `AGENTS.md`.

## Run keys and baseline drift

A benchmark run key hashes `CELL_KEY_FIELDS`: task, model, CLI version, effort, timeout, mode, repetition, `git_sha`, `binary_sha256`, and `plugin_sha256` (`benchmark/baselines.py:23-26`). It also hashes `harness_digest`, `task_digest`, and `env_fingerprint` (`benchmark/baselines.py:27`).
`BASELINE_SLOT_FIELDS` match a stored stock-arm row to a planned cell. The `bare`, `strict_file_tools`, and `max_budget_usd` variant flags are slot fields, so each variant is its own baseline (`benchmark/baselines.py:33-36`).
Any other key input that differs for a matched slot, including `harness_digest`, is drift. The run refuses without `--refreeze-baselines` (`benchmark/baselines.py:233-254`).
The task digest covers grader source and every `<stem>_fixtures` directory (`_cell_task_digest`, `benchmark/run.py:662-675`; `task_digest`, `benchmark/baselines.py:69-97`). The first version missed both.
The digest hashes `identity_inputs` only when a task gives them. An external task gives `external_package = package_digest()`. `package_digest` hashes every `benchmark/external/*.py` except `contamination.py`, `download.py`, and `preflight.py` (`benchmark/external/task.py:40,183-188,291-302`).
The evolve preflight checks test-split baseline drift before any paid call (`benchmark/evolve/loop.py:247-253`).

## Result store and paid runs

- A completed row has no `error`, no `infra` value, and no timeout (`is_completed`, `benchmark/baselines.py:185-187`). Only a completed row is reusable. The store re-runs error, quota, and timeout rows.
- A reused row goes to the run's output JSONL like a fresh row, with `reused: true`, so `analyze.py` sees it.
- A run is paid when at least one planned cell has no reusable row. A run that the store answers fully is not paid and needs no `--max-usd` (spec claim, not re-verified).
- A row records `cost_source`: `native` when the stream reports `total_cost_usd`, else `pricing` from `pricing.yaml` (`benchmark/parse.py:63,223`). Parsing keeps an absent cost apart from a reported 0.0. A failed cell charges its native cost, else its pre-run estimate as `estimate` (`fail()` in `_run_plan`, `benchmark/run.py:1636-1642`).
- The cell estimate is the mean stored cost for that task and arm, else the run's largest cell cost so far, else `--cell-estimate-usd`. Reused rows do not enter it (`estimate_cell_cost`, `benchmark/run.py:1307-1330`).
- Runner subprocesses set `DISABLE_AUTOUPDATER=1` (`benchmark/run.py:250`), so the agent CLI version cannot change in a run.
- Claude and Codex cells write an untruncated trajectory sidecar. OpenCode cells record `trajectory_path: null`.

## Spend ledger and finish reserve

The shared `spend.SpendLedger` refuses a paid call when spent plus estimate plus reserve would cross `--max-usd` (`would_cross`, `benchmark/spend.py:27`).
The evolve loop holds a finish reserve of `3 * finalist_test_cost`. The value `finalist_test_cost` estimates one finalist's test-split cells plus unstored baseline test cells (`benchmark/evolve/loop.py:221-234`).
The preflight refuses to start when `max_usd <= reserve` (`benchmark/evolve/loop.py:254-260`). The loop releases the reserve at finish. The reserve over-counts baseline cells. Review left this because it is conservative; `loop.py` records the rationale next to the code.
Reused rows charge `0.0`, and `main` charges `0.0` when a cell fails in prepare (`benchmark/run.py:2267-2275`). `--cell-estimate-usd` sets the per-cell estimate.

### No paid call spends the finish reserve

A paid call never spends into the finish reserve.

- `SpendLedger.headroom()` returns `max_usd - spent - reserve`, or `None` when the run has no ceiling. `exhausted()` is true when headroom is below `MIN_HEADROOM_USD` (0.01) (`benchmark/spend.py:15,31,35-38`).
- `_run_plan` stops the plan as `ceiling` when `ledger.exhausted()` holds, for every runner (`benchmark/run.py:1622`). The 0.01 floor therefore applies to Codex and OpenCode cells too.
- `_run_plan` caps each Claude cell at `min(DEFAULT_MAX_BUDGET_USD, headroom)` and passes that cap to the runner (`benchmark/run.py:1625-1628`). The row records the cap as `applied_max_budget_usd`. The run key still uses the `<max-budget>` placeholder (`benchmark/run.py:680`).
- A capped Claude cell that ends `error_max_budget_usd` stops the plan as `ceiling` (`benchmark/run.py:1666-1668`).
- `ClaudeJudgeClient` passes the headroom as `--max-budget-usd` (`benchmark/judge/core.py:324-333`). `Judge._call` raises `JudgeSpendCeiling` when `exhausted()` holds or the estimate would cross (`benchmark/judge/core.py:472-475`).
- Reflection and proposer calls pass the headroom as `--max-budget-usd` and check `would_cross` and `exhausted()` first (`PaidCalls.call`, `benchmark/evolve/calls.py:92-136`).
- A reused row with an older panel stamp is stored again with the current stamp, `charged_usd: 0.0`, and `reused: true` (`_store_reused_panel_row`, `benchmark/run.py:1690-1709`). The store skips the copy when a row for the run key already carries this panel name and split digest. `_run_plan` and `main` both call it (`benchmark/run.py:1593-1600,2157`). So the split lock holds on a run where every cell is a cache hit.

## Admission, grading, and contamination

External-task admission (`benchmark/external/preflight.py`) refuses `gold_unresolved`, `empty_resolved`, and `tampered_resolved` tasks. ADR-007 holds the single-hunk rule.

- The parquet files carry a pinned sha256. `download._verified` refuses a mismatch (`benchmark/external/data.py:25-28`).
- Grading never uses the agent's `.venv`. It builds a cached venv under `$TILTH_BENCH_DATA/envs/<data_rev>/<name>/<fingerprint>/.venv` (`grade_venv`, `benchmark/external/task.py:455-474`). Each grade runs on a private copy of that venv (`grading_env`, `benchmark/external/task.py:542-554`).
- Before grading, Python tasks reset `conftest.py`, `pytest.ini`, `.pytest.ini`, `pytest.toml`, `.pytest.toml`, `tox.ini`, and `setup.cfg` to `base_commit` (`benchmark/external/task.py:60-62`).
- `restore_test_config` also resets a `pyproject.toml` that has `tool.pytest` or does not parse. `prune_added_top_level` removes top-level paths the agent added (`benchmark/external/task.py:482-518`). So agent-written test infrastructure cannot steer a grade.
- Grading reads machine-readable reports only. FeatureBench reads JUnit XML (`benchmark/external/featurebench.py:43,63,99-113`). The Go path reads `go test -json` (`benchmark/external/swebench_ml.py:53,74`). Log text never decides a verdict.
- The contamination scan flags tool-call inputs that reach the `benchmark/` tree, the `.cheese/` tree (`harness_notes`), the harness data directory, a foreign clone, an upstream fetch, or package source (`_call_reasons`, `benchmark/external/contamination.py:163-181`). A missing sidecar is `unscanned` (`benchmark/external/contamination.py:187`). The scan runs on every panel cell, local tasks included.
- Dataset rows hold gold patches and held-out tests. Keep them in `$TILTH_BENCH_DATA`, never in the checkout.

### Prepare and grade

The prepare and restore steps below are spec claims, not re-verified, except where a line cites code.

- `download.fetch` is the only download path. `external/__init__.py:20` and the preflight CLI (`preflight._ensure_rows`, `benchmark/external/preflight.py:230`) call it. The loaders and `run.py` never fetch (spec claim).
- `prepare` exports the `base_commit` tree with `git archive` and commits it as the one commit of a fresh repository with no history and no remote.
- For FeatureBench, `prepare` applies the row `patch` hunks for files outside FAIL_TO_PASS forward, which masks the feature. It drops the hunks for FAIL_TO_PASS files. Then it applies `test_patch`, a deleted-file diff, so the held-out tests are gone. The transformation name is `mask_patch_forward` (`benchmark/external/featurebench.py:118`).
- The workdir never holds a gold patch, a test patch, a held-out test file, or a FAIL_TO_PASS or PASS_TO_PASS list.
- Grading copies the agent's changes onto a clean prepared checkout. FeatureBench resets every FAIL_TO_PASS and PASS_TO_PASS file to its `base_commit` content and does not apply `test_patch`. SWE-bench Multilingual resets the files `test_patch` touches, then applies `test_patch`. Agent edits to graded test files do not count.
- A listed entry with no matching result fails. `correct` is true only when every entry passes. `grade_details` returns `pass_rate` over FAIL_TO_PASS plus PASS_TO_PASS entries and the per-list counts (`benchmark/external/task.py:556-597`).
- External cells time out at 1200 s for Python and 1800 s for Go and Rust (`benchmark/external/task.py:58-59,261`).

## Panel file and split lock

The `gepa-v1` panel (`benchmark/panels/gepa-v1.json`) has a cheap tier of the three local hard tasks, which is dev-only. The rest form a stratified dev/test split (`benchmark/panels.py:116-130`).

- A panel file holds `name`, `split_seed`, and `members`. Each member has `id`, `family`, `language`, and `split`. A fallback member also has `fallback_for` and `fallback_reason`. The loader refuses unknown and missing keys (`_parse`, `benchmark/panels.py:141`). Fallbacks exist only for `swebench_ml`.
- `stratify` groups the non-cheap members by source family. Inside a family it sorts by `(language, sha256("<seed>:<id>"))` and alternates dev and test, so dev gets the extra member (`benchmark/panels.py:116-130`). The loader refuses a recorded split that differs from `stratify` (spec claim, not re-verified).
- `split_digest` hashes only the sorted id lists of each split (`benchmark/panels.py:133-138`). Member order and `fallback_reason` text do not change it.
- The split locks at the first completed result. A stored row that `baselines.is_completed` accepts, with the panel name and a different `panel_split_digest`, raises `PanelError` (`_check_lock`, `benchmark/panels.py:277-283`). There is no unlock flag. Register a changed split under a new panel name.
- Committed panel fixtures hold redacted rows only: `instance_id`, `repo`, `base_commit`, and the language column. No file in the checkout holds a real `patch`, `test_patch`, FAIL_TO_PASS, PASS_TO_PASS, or `problem_statement` value (spec claim, not re-verified).
- `analyze.py` counts a contaminated panel row as incorrect and keeps it in its denominators (`paired.is_contaminated_panel_row`, `benchmark/paired.py:43-63`; `_contaminated_section`, `benchmark/analyze.py:71-94`).
- Build panels through `load_panel` and the other loaders. The `Panel` docstring says so (`benchmark/panels.py:57`); constructing `Panel` directly bypasses checks by convention only.

## Judge calibration gate

The judge is pinned to `claude-sonnet-5` with labels `strong`, `weak`, `none` and verdicts `apt`, `missed`, `misapplied` (`benchmark/judge/config.py:9-15`).
It is calibrated only when unweighted Cohen's kappa reaches 0.6 on label and verdict dimensions. It also needs at least 20 calibration trajectories (`benchmark/judge/core.py:281-289,420-424`).
A missing or invalid `benchmark/judge/calibration.json` raises `CalibrationInvalid`. Evolve then continues uncalibrated with labels and critiques withheld. The committed file is an empty placeholder.
The label cache key is `sha256(subject, model, prompt_hash)`, where the subject is the task digest for labels and the run key for critiques (`benchmark/judge/store.py:77-86`). `judge_stamp` also covers the threshold and minimum trajectories (`benchmark/judge/store.py:61-69`).

### Judge call isolation

- Every judge call runs `claude -p --output-format stream-json --verbose --model JUDGE_MODEL --tools "" --strict-mcp-config --setting-sources "" --no-session-persistence --disable-slash-commands` (`benchmark/judge/core.py:324-333`). It never passes `--bare`, which refuses OAuth, or `--mcp-config`.
- The call runs in a fresh empty temporary cwd with a fresh empty `CLAUDE_CONFIG_DIR`. So no settings, hooks, MCP servers, or files load (`benchmark/judge/core.py:339-342`).
- `stripped_record` is the one grader-free view of a rollout: the task prompt, allowlisted row fields, integer `f2p_`/`p2p_` counts, and the verbatim sidecar (`benchmark/judge/core.py:131`). It raises `UnresolvableTask` when the task does not resolve. Evolve builds its reflective records with it (`_records`, `benchmark/evolve/loop.py:387-410`).
- The default resolver loads a cached external task without admission (`default_resolve_task`, `benchmark/judge/core.py:112-119`). The judge must read rollouts of tasks that admission may since refuse.
- `critique` withholds a row as `contaminated` (when `contaminated` is not exactly false), `no-trajectory`, or `uncalibrated` before any call (`benchmark/judge/core.py:397-405`).
- Evolve releases labels only through `judge.store.released_label` (`benchmark/judge/store.py:160`).
- Labels and critiques never enter result rows or the result store.

## GEPA candidate guards

The materializer refuses a candidate before any paid call when it breaks a guard.

- Paths outside `src/` and `prompts/` are refused (`benchmark/evolve/materialize.py:15,170-172`).
- The materializer lexes every changed file under `src/` (`_refuse_harness_reach`, `benchmark/evolve/materialize.py:199-239`). Added lines may not name the forbidden terms: `benchmark/`, the panel path, `panel.resolve()`, `panel.name`, and the data directory (`benchmark/evolve/loop.py:178-180`).
- The same guard refuses `include!`, `include_str!`, and `include_bytes!` targets outside `src/` and `prompts/`. It resolves `include` aliases, `cfg!(test)` macros, and `#[path]` attributes (`benchmark/evolve/rust.py:130-165`).
- Any `cfg` or `cfg_attr` predicate that names `test` guards its item, and an inner `#![cfg(..test..)]` guards the file (`_refuse_cfg_test`, `benchmark/evolve/materialize.py:241-266`).
- The byte-lock test exemption covers only the three literal spans: count, `starts_with`, and `ends_with`. The rest of each line must stay byte-identical (`blank_byte_lock`, `benchmark/evolve/rust.py:202-216`). Whole-line exemption once let a candidate append `return;` and skip later assertions.
- The proposer works in a worktree without `benchmark/`, and Glob patterns are normalised before the path check.
- A failed build scores 0 as stage `build` (`benchmark/evolve/loop.py:351-352`). `Cascade.side_info` returns the stage and the build-output tail (`benchmark/evolve/loop.py:103-109`). The dispatcher adds that record to both the reflection prompt and the `src_patch` proposer prompt (`benchmark/evolve/engine.py:76-106`). The #312 archive README says reflection gets no build-failure feedback; that follow-up is stale.
- The GEPA score uses the means of the re-runs (`Cascade.score`, `benchmark/evolve/loop.py:91-97`). `finish` lets the seed win test-score ties (`benchmark/evolve/loop.py:530`).

Candidate refs live at `refs/evolve/<run-id>/<content-id[:12]>`. Builds are cached once per sha. Defaults are 3 frontier re-runs and a 5-round plateau.
The six stop reasons are `ceiling`, `quota`, `plateau`, `cli-version`, `mcp-unavailable`, and `invalid-cell` (`benchmark/evolve/calls.py:16`).
Finish pushes with an empty-ref lease, so an existing winner branch is a logged refusal. It then opens `gh pr create --draft` with no merge.

## Before the first paid run

The archive left this checklist open on 2026-10-08.

1. Hand-label `benchmark/judge/calibration.json`: every panel task plus at least 20 clean trajectories.
2. Run `python3 benchmark/external/preflight.py --panel benchmark/panels/gepa-v1.json` on the paid host.
3. Record a scrubbed real `claude -p` OAuth stream as a parser fixture (`bench-oauth-fixture`).
4. Expect one `--refreeze-baselines` if an older result store exists.

## Open risks and deferred work

These risks remained open at the end of the PR #312 session, the provenance of this page.

- Whether OAuth streams carry `total_cost_usd` and `rate_limit_event` is untested. Cost falls back to `pricing.yaml`.
- The contamination scan detects known paths only. Native cells have no isolation. Issue [#322](https://github.com/paulnsorensen/tilth/issues/322) tracks sandboxing for grading, candidate builds, and agent cells. It also covers a symlinked test directory that could let config restore write through the link.
- Per-instance Pareto selection can chase noise on expensive rollouts.
- The live `claude -p` judge command line has not run against a real CLI.
- `run_plan` duplicates the cell loop of `run.main`. Issue [#320](https://github.com/paulnsorensen/tilth/issues/320) tracks one shared scheduler.
- Spend accounting is incomplete: the ledger does not carry across resumes, and partial streams are priced loosely. Issue [#325](https://github.com/paulnsorensen/tilth/issues/325) tracks it, with the pre-runner estimate charge and undercharged timeouts.
- Reflection prompt size is unbounded, and the result store is local-only.

_Source: PR #312 session archive (phase outcomes, hardening cure, wheypoint), verified against the layer 6 head of PR #319; stack-only commits `e42d451`, `70466b9`, `e77a567` via `refs/pull/319/head` · Updated: 2026-10-13_
