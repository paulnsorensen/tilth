# Benchmark GEPA loop invariants and guards

The benchmark GEPA overhaul adds a result store, native external tasks, a locked panel, a calibrated judge, and a GEPA evolution loop under `benchmark/`.
This page lists the invariants and guards a future change must keep, with code citations at `e77a567`.
The reasons behind them are in [ADR: benchmark GEPA overhaul](adr/benchmark-gepa-overhaul.md). The contracts are the specs in `.cheese/specs/` (`benchmark-gepa-overhaul.md` and the `bench-*.md` children).

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

## Spend ledger and finish reserve

The shared `spend.SpendLedger` refuses a paid call when spent plus estimate plus reserve would cross `--max-usd` (`benchmark/spend.py:24-26`).
The evolve loop holds a finish reserve of `min(3 * finalist_test_cost, max_usd)`, where `finalist_test_cost` estimates one finalist's test-split cells plus unstored baseline test cells (`benchmark/evolve/loop.py:219-256`). The reserve is released at finish.
The reserve over-counts baseline cells; review left this because it is conservative.
Reused rows charge `0.0`. `--cell-estimate-usd` sets the per-cell estimate.

## Admission, grading, and contamination

External-task admission (`benchmark/external/preflight.py`) refuses `gold_unresolved`, `empty_resolved`, and `tampered_resolved` tasks; see ADR-007 for the single-hunk rule.
Grading never uses the agent's `.venv`. It builds a cached venv under `$TILTH_BENCH_DATA/envs/` from the prepared tree (`benchmark/external/task.py:372`).
Before grading, Python tasks reset `conftest.py`, `pytest.ini`, `.pytest.ini`, `pytest.toml`, `.pytest.toml`, `tox.ini`, and `setup.cfg` to `base_commit` (`benchmark/external/task.py:56-58`), so agent-written test infrastructure cannot steer a grade.
Every `benchmark/external/*.py` is hashed into the external run key.
The contamination scan flags tool-call inputs that reach the `benchmark/` tree, the `.cheese/` tree (`harness_notes`), the harness data directory, a foreign clone, an upstream fetch, or package source; a missing sidecar is `unscanned` (`benchmark/external/contamination.py:138-160`). It runs on every panel cell, local tasks included.
Dataset rows hold gold patches and held-out tests. Keep them in `$TILTH_BENCH_DATA`, never in the checkout.

## Panel split lock

The `gepa-v1` panel (`benchmark/panels/gepa-v1.json`) has a cheap tier of the three local hard tasks, which is dev-only, and a stratified dev/test split of the rest (`benchmark/panels.py:29,113-127`).
The split locks at the first recorded result: a completed stored row with the panel name and a different `split_digest` raises `PanelError` (`benchmark/panels.py:278-284`).
Build panels through the loaders; constructing `Panel` directly bypasses checks by convention only.

## Judge calibration gate

The judge is pinned to `claude-sonnet-5` with labels `strong`, `weak`, `none` and verdicts `apt`, `missed`, `misapplied` (`benchmark/judge/config.py:9-15`).
It is calibrated only when unweighted Cohen's kappa reaches 0.6 on label and verdict dimensions, with at least 20 calibration trajectories (`benchmark/judge/core.py:371-375`).
A missing or invalid `benchmark/judge/calibration.json` raises `CalibrationInvalid`, and evolve continues uncalibrated with labels and critiques withheld. The committed file is an empty placeholder.
The label cache key is `sha256(subject, model, prompt_hash)`, where the subject is the task digest for labels and the run key for critiques (`benchmark/judge/store.py:75-84`).

## GEPA candidate guards

The materializer refuses a candidate before any paid call when it breaks a guard.

- Paths outside `src/` and `prompts/` are refused (`benchmark/evolve/materialize.py:15`).
- The byte-lock test exemption covers only the three literal spans (count, `starts_with`, `ends_with`). The rest of each line must stay byte-identical (`benchmark/evolve/rust.py:179`). Whole-line exemption once let a candidate append `return;` and skip later assertions.
- Any `cfg` or `cfg_attr` predicate that names `test` guards its item; an inner `#![cfg(..test..)]` guards the file.
- Added lines may not name harness terms (`benchmark`, `.cheese`, `tilth_bench`, `TILTH_BENCH_DATA`, the panel path, the data dir) or `include!`/`include_str!`/`include_bytes!` a path outside `src/` or `prompts/` (`benchmark/evolve/materialize.py:17,186-197`).
- The proposer works in a worktree without `benchmark/`, and Glob patterns are normalised before the path check.
- A failed build scores 0 as stage `build` and passes the failure tail to reflection (`benchmark/evolve/loop.py:343`).

Candidate refs live at `refs/evolve/<run-id>/<content-id[:12]>`. Builds are cached once per sha. Defaults are 3 frontier re-runs and a 5-round plateau; stop reasons are `ceiling`, `quota`, `plateau`, and `cli-version`. Finish pushes with an empty-ref lease, so an existing winner branch is a logged refusal, and opens `gh pr create --draft` with no merge.

## Before the first paid run

The archive left this checklist open on 2026-10-08.

1. Hand-label `benchmark/judge/calibration.json`: every panel task plus at least 20 clean trajectories.
2. Run `python3 benchmark/external/preflight.py --panel benchmark/panels/gepa-v1.json` on the paid host.
3. Record a scrubbed real `claude -p` OAuth stream as a parser fixture (`bench-oauth-fixture`).
4. Expect one `--refreeze-baselines` if an older result store exists.

## Open risks and deferred work

These risks remained open when PR #312 closed.

- Whether OAuth streams carry `total_cost_usd` and `rate_limit_event` is untested; cost falls back to `pricing.yaml`.
- The contamination scan detects known paths only; native cells have no isolation.
- A symlinked test directory could let config restore write through the link.
- Per-instance Pareto selection can chase noise on expensive rollouts.
- The live `claude -p` judge command line has not run against a real CLI.
- Deferred: `run_plan` duplicates `run.main`'s cell loop; reflection prompt size is unbounded; pre-runner errors are charged the estimate and timeouts are undercharged; the result store is local-only.

_Source: PR #312 session archive (phase outcomes, hardening cure, wheypoint), verified against code at `e77a567` · Updated: 2026-10-08_
