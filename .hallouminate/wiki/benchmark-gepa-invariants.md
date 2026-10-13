# Benchmark GEPA loop invariants and guards

The benchmark GEPA overhaul adds a result store, native external tasks, a locked panel, a calibrated judge, and a GEPA evolution loop under `benchmark/`.
This page lists the invariants and guards a future change must keep. Code citations are at `e77a567` unless a line names `70466b9`.
The reasons behind them, the goals, and the task picks are in [ADR: benchmark GEPA overhaul](adr/benchmark-gepa-overhaul.md). The umbrella and child specs that held the contracts are removed from the stack; the ADR page keeps their durable content.

## Cross-curd invariants

The umbrella spec states six invariants, and `benchmark/tests/test_invariants.py` tests them.

- The only score is grader correctness. A contaminated rollout counts as incorrect (`benchmark/evolve/loop.py:119-124`).
- No optimizer-side model reads grader inputs. Reflective records keep `correct`, `pass_rate`, and F2P/P2P counts, and drop `correctness_reason`, ground-truth strings, gold patches, and held-out test source or output. Text the agent itself produced stays.
- One `--max-usd` ceiling and one OAuth guard cover agent cells, judge calls, reflection calls, and proposer calls.
- Baselines are reused, never silently re-bought.
- Nothing runs in a container.
- A winner lands only as an unmerged draft PR that changes `src/**`, `prompts/**`, and the regenerated `AGENTS.md`.

## Run keys and baseline drift

A benchmark run key hashes `CELL_KEY_FIELDS` (task, model, CLI version, effort, timeout, mode, repetition, `git_sha`, `binary_sha256`) plus `harness_digest`, `task_digest`, and `env_fingerprint` (`benchmark/baselines.py:22-26`).
`BASELINE_SLOT_FIELDS` match a stored stock-arm row to a planned cell. The `bare`, `strict_file_tools`, and `max_budget_usd` variant flags are slot fields, so each variant is its own baseline (`benchmark/baselines.py:32-35`).
Any other key input that differs for a matched slot, including `harness_digest`, is drift, and the run refuses without `--refreeze-baselines` (`benchmark/baselines.py:165-178`).
The task digest covers grader source and every `<stem>_fixtures` directory (`benchmark/run.py:662-710`). The first version missed both.
The evolve preflight checks test-split baseline drift before any paid call (`benchmark/evolve/loop.py:245-249`).

## Result store and paid runs

- A completed row has no `error`, no `infra` value, and no timeout (`benchmark/baselines.py:109-111`, at `70466b9`). Only a completed row is reusable. The store re-runs error, quota, and timeout rows.
- A reused row goes to the run's output JSONL like a fresh row, with `reused: true`, so `analyze.py` sees it.
- A run is paid when at least one planned cell has no reusable row. A run that the store answers fully is not paid and needs no `--max-usd` (spec claim, not re-verified).
- A row records `cost_source`: `native` when the stream reports `total_cost_usd`, else `pricing` from `pricing.yaml` (`benchmark/parse.py:62-63,223`, at `70466b9`). A failed cell charges its native cost, else its pre-run estimate as `estimate` (`benchmark/run.py:1703-1709`, at `70466b9`). Parsing keeps an absent cost apart from a reported 0.0.
- The cell estimate is the mean stored cost for that task and arm, else the run's largest cell cost so far, else `--cell-estimate-usd` (`benchmark/run.py:1674`, at `70466b9`; tier order is a spec claim, not re-verified).
- Runner subprocesses set `DISABLE_AUTOUPDATER=1` (`benchmark/run.py:249`), so the agent CLI version cannot change in a run.
- Claude and Codex cells write an untruncated trajectory sidecar. OpenCode cells record `trajectory_path: null`.

## Spend ledger and finish reserve

The shared `spend.SpendLedger` refuses a paid call when spent plus estimate plus reserve would cross `--max-usd` (`benchmark/spend.py:24-26`).
The evolve loop holds a finish reserve of `min(3 * finalist_test_cost, max_usd)`, where `finalist_test_cost` estimates one finalist's test-split cells plus unstored baseline test cells (`benchmark/evolve/loop.py:219-256`). The reserve is released at finish.
The reserve over-counts baseline cells; review left this because it is conservative.
Reused rows charge `0.0`. `--cell-estimate-usd` sets the per-cell estimate.

### No paid call spends the finish reserve

A paid call never spends into the finish reserve. These citations are at `70466b9`.

- `SpendLedger.headroom()` returns `max_usd - spent - reserve`, or `None` when the run has no ceiling (`benchmark/spend.py:28-30`).
- `_run_plan` caps each Claude cell at `min(DEFAULT_MAX_BUDGET_USD, headroom)` and passes that cap to the runner (`benchmark/run.py:1688-1695,1713`). `DEFAULT_MAX_BUDGET_USD` is `1.0` (`benchmark/config.py:174`). The row records the cap as `applied_max_budget_usd`. The run key still pins the default `max_budget_usd`. A cap at or below 0 stops the plan as `ceiling`.
- `ClaudeJudgeClient` passes the same headroom as `--max-budget-usd` (`benchmark/judge/core.py:302-315`). `Judge._call` raises `JudgeSpendCeiling` when headroom is at or below 0, or when the estimate would cross (`benchmark/judge/core.py:442-447`).
- Reflection and proposer calls pass the headroom as `--max-budget-usd` (`benchmark/evolve/calls.py:102-105`).
- `_store_reused_panel_row` stores a copy of a reused row with the current panel stamp, `charged_usd: 0.0`, and `reused: true` when the stored stamp differs (`benchmark/run.py:1612-1620`). `_run_plan` and `main` both call it (`benchmark/run.py:1666,2204`). So the split lock holds on a run where every cell is a cache hit.

## Admission, grading, and contamination

External-task admission (`benchmark/external/preflight.py`) refuses `gold_unresolved`, `empty_resolved`, and `tampered_resolved` tasks; see ADR-007 for the single-hunk rule.
Grading never uses the agent's `.venv`. It builds a cached venv under `$TILTH_BENCH_DATA/envs/` from the prepared tree (`benchmark/external/task.py:372`).
Before grading, Python tasks reset `conftest.py`, `pytest.ini`, `.pytest.ini`, `pytest.toml`, `.pytest.toml`, `tox.ini`, and `setup.cfg` to `base_commit` (`benchmark/external/task.py:56-58`), so agent-written test infrastructure cannot steer a grade.
Every `benchmark/external/*.py` is hashed into the external run key.
The contamination scan flags tool-call inputs that reach the `benchmark/` tree, the `.cheese/` tree (`harness_notes`), the harness data directory, a foreign clone, an upstream fetch, or package source; a missing sidecar is `unscanned` (`benchmark/external/contamination.py:138-160`). It runs on every panel cell, local tasks included.
Dataset rows hold gold patches and held-out tests. Keep them in `$TILTH_BENCH_DATA`, never in the checkout.

### Prepare and grade

The prepare and restore steps below are spec claims, not re-verified, except where a line cites code.

- `external.fetch` is the only download path. Admission, the loaders, and `run.py` never fetch.
- `prepare` exports the `base_commit` tree with `git archive` and commits it as the one commit of a fresh repository with no history and no remote.
- For FeatureBench, `prepare` applies the row `patch` hunks for files outside FAIL_TO_PASS forward, which masks the feature. It drops the hunks for FAIL_TO_PASS files. Then it applies `test_patch`, a deleted-file diff, so the held-out tests are gone. The transformation name is `mask_patch_forward` (`benchmark/external/featurebench.py:78`).
- The workdir never holds a gold patch, a test patch, a held-out test file, or a FAIL_TO_PASS or PASS_TO_PASS list.
- Grading copies the agent's changes onto a clean prepared checkout. FeatureBench resets every FAIL_TO_PASS and PASS_TO_PASS file to its `base_commit` content and does not apply `test_patch`. SWE-bench Multilingual resets the files `test_patch` touches, then applies `test_patch`. Agent edits to graded test files do not count.
- A listed entry with no matching result fails. `correct` is true only when every entry passes. `grade_details` returns `pass_rate` over FAIL_TO_PASS plus PASS_TO_PASS entries and the per-list counts (`benchmark/external/task.py:460`).
- External cells time out at 1200 s for Python and 1800 s for Go and Rust (`benchmark/external/task.py:53-54,220`).

## Panel file and split lock

The `gepa-v1` panel (`benchmark/panels/gepa-v1.json`) has a cheap tier of the three local hard tasks, which is dev-only, and a stratified dev/test split of the rest (`benchmark/panels.py:29,113-127`).

- A panel file holds `name`, `split_seed`, and `members`. Each member has `id`, `family`, `language`, and `split`. A fallback member also has `fallback_for` and `fallback_reason`. The loader refuses unknown and missing keys (`benchmark/panels.py:150-154`, at `70466b9`).
- `stratify` groups the non-cheap members by source family. Inside a family it sorts by `(language, sha256("<seed>:<id>"))` and alternates dev and test, so dev gets the extra member (`benchmark/panels.py:115-129`, at `70466b9`). The loader refuses a recorded split that differs from `stratify` (spec claim, not re-verified).
- `split_digest` hashes only the sorted id lists of each split (`benchmark/panels.py:132-137`, at `70466b9`). Member order and `fallback_reason` text do not change it.
- The split locks at the first completed result: a stored row that `baselines.is_completed` accepts, with the panel name and a different `panel_split_digest`, raises `PanelError` (`benchmark/panels.py:282-285`, at `70466b9`). There is no unlock flag. Register a changed split under a new panel name.
- Committed panel fixtures hold redacted rows only: `instance_id`, `repo`, `base_commit`, and the language column. No file in the checkout holds a real `patch`, `test_patch`, FAIL_TO_PASS, PASS_TO_PASS, or `problem_statement` value (spec claim, not re-verified).
- `analyze.py` counts a contaminated panel row as incorrect and keeps it in its denominators (`benchmark/analyze.py:65-79`, at `70466b9`).
- Build panels through the loaders; constructing `Panel` directly bypasses checks by convention only.

## Judge calibration gate

The judge is pinned to `claude-sonnet-5` with labels `strong`, `weak`, `none` and verdicts `apt`, `missed`, `misapplied` (`benchmark/judge/config.py:9-15`).
It is calibrated only when unweighted Cohen's kappa reaches 0.6 on label and verdict dimensions, with at least 20 calibration trajectories (`benchmark/judge/core.py:371-375`).
A missing or invalid `benchmark/judge/calibration.json` raises `CalibrationInvalid`, and evolve continues uncalibrated with labels and critiques withheld. The committed file is an empty placeholder.
The label cache key is `sha256(subject, model, prompt_hash)`, where the subject is the task digest for labels and the run key for critiques (`benchmark/judge/store.py:75-84`).

### Judge call isolation

These citations are at `70466b9`.

- Every judge call runs `claude -p --output-format stream-json --verbose --model JUDGE_MODEL --tools "" --strict-mcp-config --setting-sources "" --no-session-persistence --disable-slash-commands` (`benchmark/judge/core.py:295-304`). It never passes `--bare`, which refuses OAuth, or `--mcp-config`.
- The call runs in a fresh empty temporary cwd with a fresh empty `CLAUDE_CONFIG_DIR`, so no settings, hooks, MCP servers, or files load (`benchmark/judge/core.py:309-316`).
- `stripped_record` is the one grader-free view of a rollout: the task prompt, allowlisted row fields, integer `f2p_`/`p2p_` counts, and the verbatim sidecar (`benchmark/judge/core.py:107-120`). It raises `UnresolvableTask` when the task does not resolve. Evolve builds its reflective records with it (`benchmark/evolve/loop.py:385`).
- `critique` withholds a row as `contaminated` (when `contaminated` is not exactly false), `no-trajectory`, or `uncalibrated` before any call (`benchmark/judge/core.py:370-375`).
- Labels and critiques never enter result rows or the result store.

## GEPA candidate guards

The materializer refuses a candidate before any paid call when it breaks a guard.

- Paths outside `src/` and `prompts/` are refused (`benchmark/evolve/materialize.py:15`).
- The byte-lock test exemption covers only the three literal spans (count, `starts_with`, `ends_with`). The rest of each line must stay byte-identical (`benchmark/evolve/rust.py:179`). Whole-line exemption once let a candidate append `return;` and skip later assertions.
- Any `cfg` or `cfg_attr` predicate that names `test` guards its item; an inner `#![cfg(..test..)]` guards the file.
- Added lines may not name harness terms (`benchmark`, `.cheese`, `tilth_bench`, `TILTH_BENCH_DATA`, the panel path, the data dir) or `include!`/`include_str!`/`include_bytes!` a path outside `src/` or `prompts/` (`benchmark/evolve/materialize.py:17,186-197`).
- The proposer works in a worktree without `benchmark/`, and Glob patterns are normalised before the path check.
- A failed build scores 0 as stage `build` (`benchmark/evolve/loop.py:343-344`). `Cascade.side_info` returns the stage and the build-output tail (`benchmark/evolve/loop.py:101-103`), and the dispatcher adds that record to both the reflection prompt and the `src_patch` proposer prompt (`benchmark/evolve/engine.py:94-104`). Verified at `70466b9`. The #312 archive README says reflection gets no build-failure feedback; that follow-up is stale.

Candidate refs live at `refs/evolve/<run-id>/<content-id[:12]>`. Builds are cached once per sha. Defaults are 3 frontier re-runs and a 5-round plateau. The six stop reasons are `ceiling`, `quota`, `plateau`, `cli-version`, `mcp-unavailable`, and `invalid-cell` (`benchmark/evolve/calls.py:16`, at `70466b9`). Finish pushes with an empty-ref lease, so an existing winner branch is a logged refusal, and opens `gh pr create --draft` with no merge.

## Before the first paid run

The archive left this checklist open on 2026-10-08.

1. Hand-label `benchmark/judge/calibration.json`: every panel task plus at least 20 clean trajectories.
2. Run `python3 benchmark/external/preflight.py --panel benchmark/panels/gepa-v1.json` on the paid host.
3. Record a scrubbed real `claude -p` OAuth stream as a parser fixture (`bench-oauth-fixture`).
4. Expect one `--refreeze-baselines` if an older result store exists.

## Open risks and deferred work

These risks remained open at the end of the PR #312 session, the provenance of this page.

- Whether OAuth streams carry `total_cost_usd` and `rate_limit_event` is untested; cost falls back to `pricing.yaml`.
- The contamination scan detects known paths only; native cells have no isolation.
- A symlinked test directory could let config restore write through the link.
- Per-instance Pareto selection can chase noise on expensive rollouts.
- The live `claude -p` judge command line has not run against a real CLI.
- Deferred: `run_plan` duplicates `run.main`'s cell loop; reflection prompt size is unbounded; pre-runner errors are charged the estimate and timeouts are undercharged; the result store is local-only.

_Source: PR #312 session archive (phase outcomes, hardening cure, wheypoint), verified against code at `e77a567`; child specs at `e42d451` and the stack's finish-reserve fixes, verified at `70466b9` · Updated: 2026-10-13_
