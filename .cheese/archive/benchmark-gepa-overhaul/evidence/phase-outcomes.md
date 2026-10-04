# Phase outcomes by curd

Consolidated from each phase agent's final report to the orchestrating session.
Full phase report bodies survive in `phase-reports/` for c1 and c5 only. For c0, c2, c3, and c4 they lived in per-curd git worktrees that were removed after merging. Their per-phase status records survive in `corpus/work/<slug>/`.

## c0 bench-prompt-files

- Commit `b0af49a` (landed as `9afcd8b`): `tilth_search` and `tilth_write` descriptions load from `prompts/tools/search.md` (196 bytes) and `prompts/tools/write.md` (811 bytes) via `include_str!`, with no trailing newline, like `read.md`.
- AC-2 landing check: `tools/list` from the da84a41 build and the post-move build have the same SHA-256 (`6d8d10f4c0071ea122c1368d649aed63130304abd68ba305ff057c23c4556306`).
- Guard test `test_descriptions_match_prompt_files` checks the served descriptions against the prompt files, not frozen bytes, so c5 candidates can evolve those files.
- `scripts/regen-agents-md.sh` reads only `prompts/mcp.md`, so regeneration is a no-op.
- Gates: `verify.py` exit 0 (cargo test 981 passed, `mcp_v2` 71 OK, `tests/scripts` 6 OK, bash-guard 133/133); markdownlint clean. Press skipped (not-applicable refactor); Age found nothing.

## c1 bench-result-substrate

- `87f2e73`: run keys, untruncated trajectory sidecars, result store, drift refusal, `--max-usd`, auth guard, quota stop, informational power readout. Cook, Press (10 attack tests), Age (2 medium, 2 low), Cure.
- First independent `/age` of `87f2e73` found 2 blocking, 12 should-fix, and 6 nits:
  - Blocking: the task digest hashed `<stem>_fixtures`, which missed `gin_render_context_fixtures`, and hashed no grader source at all. Fixed in `806ea7b`, which also extracted `spend.SpendLedger`.
  - Should-fix: estimates written as real cost; drift guard blind to `no_tilth`; harness variants refused as drift; env fingerprint probing every toolchain while missing `uv`; harness digest covering only `SYSTEM_PROMPT`; `ANTHROPIC_AUTH_TOKEN` not refused; torn store line crashing the run; stale reused-row metadata; CLI version cached once per process; quota misclassification; no wiring tests; tilth-benchmark skill missing `--max-usd`. Fixed in `8efe6bd` (318 tests).
  - Deferred nits: pre-runner errors charged the estimate; timeouts undercharged; repeated per-cell work; result store local-only.
- Second independent `/age` (of `8efe6bd`, with mutation testing: 16 of 19 reversions caught) found:
  - Blocking: a quota hit with no result event was treated as an ordinary error.
  - Should-fix: `harness_digest` in the drift slot let harness changes re-buy baselines; strict mode hashed host paths; three tests did not catch their own fix being removed; a reporting error after `settle` crashed `run.main`.
  - Fixed in `49eb7eb` (334 tests). Drift slots now use explicit `bare`, `strict_file_tools`, and `max_budget_usd` flags.

## c2 bench-external-tasks

- Commits `96bc2a3`, `638c2e9`, `c4f27a6`, `69e9f3a` (merged at `c568c58`): 432 tests at the time. Press found a container step reachable through `bash -c` (fixed).
- Age pass 1 (2 high, 1 medium, 2 low), all fixed:
  - FeatureBench install order and `set -e` now match its setup script; sphinx's env build had failed.
  - The grader passes `-p no:pretty`; pytest-pretty had hidden the summary, so pydantic scored 0/6.
  - The container check now inspects only the command run.
  - Fetched rows are written atomically.
  - Old verdict caches are recomputed.
- Age pass 2 (2 low), fixed: a hung toolchain probe no longer crashes `admit`; `env`/`sudo` prefixes no longer hide a container command.
- First real preflight: FeatureBench 1 (seaborn `test_algorithms`), Go 1 (`gin-gonic__gin-3741`), Rust 1 (`sharkdp__bat-2650`). Four FeatureBench tasks passed gold 6/6 but were refused `tampered_resolved` under the gold-minus-last-hunk rule; pydantic (4/6) and xarray (missing `h5py`) were refused `gold_unresolved`.
- Follow-up `558ac65` (463 tests):
  - Tampered check now tries single-hunk removals, largest first, up to 8, and admits when any removal fails the held-out tests.
  - Grading restores `conftest.py`, `pytest.ini`, `tox.ini`, `setup.cfg`, and pytest-bearing `pyproject.toml` from `base_commit`.
  - Grading runs in a cached venv built from the prepared tree under `$TILTH_BENCH_DATA/envs/`, never the agent's `.venv`.
  - Every `benchmark/external/*.py` is hashed into external identity.
  - Re-run admitted 5 of 7 FeatureBench candidates.
- Residual risk noted: if an agent replaces a test directory with a symlink, restoring config there could write through the link.

## c3 bench-panel-manifest

- Commit `a232019` (merged at `ecd5b9e`): `benchmark/panels.py`, `benchmark/panels/gepa-v1.json`, `baselines.rows`, `run.py --panel` and `--panel-split`, and the contaminated-row tally in `analyze.py`. 522 tests at the time.
- Press passed on attempt 1 (6 attack tests). Age found 6 low findings and Cure fixed 5. The sixth, the contaminated adjustment in `paired.py`, was handed to c5, which fixed it.
- Seam deviations:
  - `load_panel` resolves `admit` and `row_source` at call time, so one stub covers c2's guard.
  - `register` passes `external.data.REVISIONS`.
  - Slot table reuses `external.swebench_ml.PICKS`.
  - Synthetic upstream repos are rebuilt from committed `projects.json`.
- `72b0261` rebuilt `gepa-v1` from 7 admitted external members, with split digest `639f297873f65839d60796808fd909408075c3c1c3dfc46e28fa480abe5d2703`.

## c4 bench-model-judge

- Commits `103fb32`, `07232e0`, `4ec78af`, `567ab51` (merged at `9e0384c`): 80 judge tests (58 contract, 18 press, 4 cure). All clients stubbed; no model call made.
- Press: ok-with-concerns. A calibration aborted by quota or ceiling writes no agreement record; Age judged this correct.
- Age pass 1 (4 low) and pass 2 (1 low), all fixed: a test depending on the local cache; analyze crashing on a missing `calibration.json`; verdicts accepted after leading blank lines; the prompt re-hashed per cache key; a traceback from `cli.py calibrate`.
- Seam deviations:
  - Calls `run._cell_task_digest` through the module.
  - Adds `JudgeCallFailed` and `CalibrationInvalid`.
  - `load_calibration()` returns a `CalibrationSet`.
  - `ClaudeJudgeClient(spawn=, timeout_s=)`.
  - `cli.main(argv, *, client=None)`.
- Risks: `trusted_reference` changes `gin_edit_render_context`'s task digest; the live `claude -p` judge command line has not been run against a real CLI.

## c5 bench-evolution-loop

- Commits `7c5497e`, `2ab667e`, `c3f2ac5`, `166589f` (merged at `36322c6`): 726 tests on the merged tree. Tests drive the real `gepa==0.1.4` and one real `cargo check --locked`; paid calls and the PR client are stubbed.
- Press: green on attempt 1 (7 attacks).
- Age pass 1 (2 medium, 6 low): duplicated reflective records and candidate worktrees never removed. Cure fixed both mediums and four lows, adding an on-disk build cache per sha, per-attempt stream names, and `analyze.load_results` delegating to `paired.load_runs`.
- Age pass 2 (1 low): a cleanup error could hide the cargo error. Fixed.
- Deferred:
  - `run_plan` duplicates `run.main`'s cell loop and wants a shared executor.
  - Prompt size is unbounded.
  - The reflection prompt carries records only, with no build-failure tail.
- Seam deviations:
  - Methods on `Evolution`, `Materializer`, and `Proposer` instead of free functions; `evolve/cli.py` instead of `evolve.main`.
  - `run_plan` gains keywords `output`, `cell_estimate_usd`, `store_only`, `repo`; adds `run.PlanStopped` and `run.BaselineDrift`.
  - Refs live at `refs/evolve/<run-id>/<cid12>`.
  - Reflection and the proposer use `claude-sonnet-5`.

## Review process note

The phase agents could not dispatch fresh-context reviewers, so their own Age passes were self-reviews. The orchestrating session ran independent reviews of c1 twice and of every spec through fresh-context fork-coherence rounds.
