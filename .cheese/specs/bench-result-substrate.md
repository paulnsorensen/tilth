---
slug: bench-result-substrate
status: approved
source: mold-curd-mini-spec
created: 2026-10-04
confidence: medium
intent: Give every benchmark row a run key and an untruncated trajectory, store rows so matching cells are reused instead of re-run, and add run-wide spend, auth, and quota controls for subscription runs.
blast_radius: medium
leverage: []
inputs: benchmark/run.py cell loop, runner environment, stream tee, and row builder; benchmark/analyze.py power readout
outputs: run_key and trajectory fields on every row; a reusable result store; --max-usd, --refreeze-baselines, and --cell-estimate-usd flags; auth guard and quota stop; informational power readout
agent_resolution: []
gates_overridden: ["fork-taste-third-failure: on 2026-10-04 the user authorized one fresh coherence round on the amended draft after the r2 test-contract gaps were folded in", "fork-taste-r3-failure: on 2026-10-04 the user selected override and cook now after the r3 findings (failed-cell spend, reused-row output, baseline drift comparison set, OpenCode sidecar scope) were folded in; /press and /age backstop residual test gaps"]
execution_holds: []
gate_applicability:
  disposition: red-required
  work_class: behavior
  ui_surface: non-browser
landing:
  shape: single
  layers: []
  per_layer_green: required
  review_fixes: fold
verification: cd benchmark && python3 -m pytest tests exits 0, including the new test_baselines.py and test_analyze.py and the extended test_run_hardening.py
---

## Parent
- Spec: benchmark-gepa-overhaul
- Goals: G-4, G-5
- Depends on: none
- Frozen decisions: F-6

## Problem

Parent goal: Grow the benchmark with FeatureBench, the Gin render-context task, and a few cherry-picked tasks, in a format GEPA can optimize against once-run stored baselines, with a model judge for structural-tool applicability.

This curd ships the substrate for G-4 and G-5. Today every run rewrites a fresh timestamped JSONL with no run key (`benchmark/run.py:1144`), so stock baselines are re-bought on every A/B. The row truncates tool arguments and drops tool outputs (`benchmark/run.py:291-319`), so a research model has nothing to reason over. `--max-cells` caps calls, not spend (`benchmark/run.py:906-911`). Runs use a Claude subscription, where a stray `ANTHROPIC_API_KEY` silently switches billing and usage limits end runs midway.

## Contract

The result substrate (parent curd c1) changes only the benchmark harness.

- **Row fields.** Every row from `benchmark/run.py` gains `run_key`, `harness_digest`, `task_digest`, `env_fingerprint`, `cli_version`, `timeout_s`, `trajectory_path`, `cost_source`, and `reused`.
- **Run key.** It hashes:
  - model ID, agent CLI version, effort, timeout, arm, and repetition;
  - the harness digest (`SYSTEM_PROMPT`, tool allowlist, strict-file-tools, bare, per-cell `--max-budget-usd`, MCP config shape);
  - the task digest (prompt, ground truth, test command, fixture files, repo commit);
  - the env fingerprint (toolchain versions plus lockfile hash);
  - for tilth arms, the candidate `git_sha` and `binary_sha256`.
- **Trajectory sidecar.** For claude and codex cells it holds every tool call's input and output, untruncated, derived from the stream the runner already tees (`run.py:656`). OpenCode cells are not teed today and are not a stock arm; their rows record `trajectory_path: null`.
- **Result store.**
  - It keeps every row, but only a completed row is reusable. A completed row has no `error`, no `infra:quota`, and no timeout.
  - A cell whose key matches a completed row is answered from the store without a model call, and the stored row is written to the run's output JSONL like a fresh row, with `reused: true`, so `analyze.py` and the research model see it. Frozen baselines are reused this way, and an interrupted run resumes this way.
  - Stored error, quota, and timeout rows are re-run.
- **Paid run.** A paid run is a `run.py` invocation where at least one planned cell has no reusable stored row, so it would spawn a model runner. A run fully answered by the store is not paid.
- **CLI pinning and baseline drift.** Runner subprocesses set `DISABLE_AUTOUPDATER=1`. A baseline row is a row whose arm is mode `baseline`. A paid run that plans a baseline cell with no completed row under its current key, while the store holds a completed baseline row for the same task, model, effort, timeout, and repetition under a different key (for example a new agent CLI version or env fingerprint), refuses to start unless `--refreeze-baselines` is passed, and names the changed key inputs. A baseline cell with no stored row under any key runs normally, and after a refreeze the new rows match, so later runs start.
- **Spend ceiling.** Paid runs require `--max-usd`.
  - Spend is summed from native `total_cost_usd` when the stream's result event contains that field, else the `pricing.yaml` computation. Parsing distinguishes an absent field from a reported 0.0, and the row records `cost_source: native | pricing`.
  - Failed cells count too. An errored, timed-out, or budget-capped cell (`error_max_budget_usd`) adds its native cost when the teed stream's result event carries one, else the estimate taken before it ran, and its error row records the amount with `cost_source: native | estimate`.
  - The run stops before a cell whose estimate would cross the ceiling. The estimate is the mean stored cost for that task and arm, else the run's maximum cell cost so far, else `--cell-estimate-usd`.
- **Auth guard.** A Claude cell refuses to start when `ANTHROPIC_API_KEY` is set, because it silently overrides `CLAUDE_CODE_OAUTH_TOKEN`. The guard lives in a shared helper that later judge and proposer callers reuse.
- **Quota stop.** A usage-limit rejection marks the cell `infra:quota` and stops the run.
- **Stream fixtures.** The work starts by committing two synthetic result streams in the existing stream-json shape, one whose result event carries `total_cost_usd` and one without it, so both cost branches are tested without a live model call. Recording a real OAuth stream is out of scope here (see Non-goals). The quota classifier is tested against a synthetic stream built from the documented `rate_limit_event` shape (`status: rejected`) and the documented "You've hit your … limit" result text [speculating until a real rejection is captured].
- **Power gate.** `analyze.py` labels the power readout informational and drops the task-growth verdict. `PHASE4_PLAN.md` gains a superseded marker naming the parent spec.

## Grounding

| Probe | Outcome | Evidence |
| --- | --- | --- |
| wiki | hit | `.hallouminate/wiki/benchmark-harness-gotchas.md`: per-cell Claude config copies auth only, revoked tokens invalidated run `20260928_201559`, `--bare` refuses OAuth |
| explorer | hit | Explorer digest at HEAD `4cafeea`: row built at `benchmark/run.py:803-857`, streams teed at `run.py:656-692,1145`, runner env built at `run.py:185-215` (already passes `CLAUDE_CODE_OAUTH_TOKEN`), no run key or result reuse, `tool_sequence` truncates at `run.py:291-319`, per-cell `--max-budget-usd` at `run.py:953` is Claude's own cap, power readout at `analyze.py:1058-1147` |

## Approach

1. **Fixtures first.** Commit two synthetic result streams for the cost parser, one with `total_cost_usd` in the result event and one without it. Also commit a synthetic quota-rejection stream built from the documented `rate_limit_event` shape for the quota classifier (G-4, F-6).
2. **Keys and sidecar.**
   - A new `benchmark/baselines.py` computes `harness_digest`, `task_digest`, `env_fingerprint`, and `run_key`.
   - The row builder writes those fields, `cli_version`, `timeout_s`, and `trajectory_path`.
   - The sidecar is derived from the teed claude or codex stream, with every tool call's input and output untruncated (G-4).
3. **Result store.**
   - `baselines.lookup` and `baselines.store` keep every row keyed by `run_key`.
   - The cell loop answers a cell from a matching completed row before spawning a runner and writes that row to the output JSONL with `reused: true`. It re-runs stored error, quota, and timeout rows (G-5).
   - Runner subprocesses set `DISABLE_AUTOUPDATER=1`. A paid run whose baseline cells drifted from stored baseline rows under a different key refuses to start without `--refreeze-baselines` (G-5).
4. **Spend and auth.**
   - `--max-usd` is required on paid runs (runs with at least one cell the store cannot answer).
   - A spend ledger sums native `total_cost_usd` when the field is present, else the `pricing.yaml` computation, and records `cost_source`. Failed cells add their native cost or their pre-run estimate. It stops before a cell whose estimate would cross the ceiling (F-6).
   - The shared runner-env helper refuses a Claude cell when `ANTHROPIC_API_KEY` is set.
   - A usage-limit rejection marks the cell `infra:quota` and stops the run (F-6).
5. **Retire the power gate.** `analyze._power_readout` is labelled informational, and `PHASE4_PLAN.md` gains a superseded marker (F-6).

## Interface sketches

```text
slice:            benchmark (existing harness)
spine step:       workflow (run.py cell loop) + infra (result store)
public interface: baselines.run_key(cell) -> str; baselines.lookup(key) -> Row | None; baselines.store(row)  (G-5)
public interface: run.py --max-usd FLOAT --cell-estimate-usd FLOAT --refreeze-baselines  (F-6)
private:          completed-row reuse rule, cost_source fallback, harness digest, task digest, env fingerprint, trajectory sidecar format, spend ledger, quota classifier, auth guard helper
crust delta:      new row fields (run_key, harness_digest, task_digest, env_fingerprint, cli_version, timeout_s, trajectory_path, cost_source, reused); new run.py flags; new benchmark/baselines.py; PHASE4_PLAN.md superseded; analyze power readout informational
arrows:           run.py -> baselines; analyze.py unchanged dependencies
```

## Acceptance
- AC-1: WHEN a claude or codex cell completes THE SYSTEM SHALL write an untruncated trajectory sidecar with every tool call's input and output, and every row SHALL carry `run_key`, `harness_digest`, `task_digest`, `env_fingerprint`, `cli_version`, `timeout_s`, `trajectory_path`, `cost_source`, `reused`, and, for tilth arms, the candidate `git_sha` and `binary_sha256`.  (G-4)
- AC-2: WHEN any cell is requested and the result store holds a completed row (no `error`, no `infra:quota`, no timeout) with the same run key THE SYSTEM SHALL reuse that row without a model call and write it to the run's output JSONL with `reused: true`, and WHEN the matching stored row is an error, quota, or timeout row THE SYSTEM SHALL re-run the cell; WHEN any harness digest, task digest, env fingerprint, or candidate identity input changes THE SYSTEM SHALL compute a different run key; WHEN no row matches THE SYSTEM SHALL run the cell and store it.  (G-5)
- AC-3: WHEN a paid run plans a baseline cell with no completed row under its current key while a completed baseline row for the same task, model, effort, timeout, and repetition exists under a different key THE SYSTEM SHALL refuse to start unless `--refreeze-baselines` is passed and name the changed key inputs, WHEN no baseline row exists under any key THE SYSTEM SHALL run the cell, and every runner subprocess SHALL run with `DISABLE_AUTOUPDATER=1`.  (G-5)
- AC-4: WHEN a run with at least one cell the store cannot answer starts without `--max-usd` THE SYSTEM SHALL exit before any model call; WHEN a stream's result event lacks `total_cost_usd` THE SYSTEM SHALL cost the cell from `pricing.yaml` and record `cost_source: pricing`; WHEN a cell errors, times out, or hits its budget cap THE SYSTEM SHALL add its native cost, else its pre-run estimate, to cumulative spend; and WHEN cumulative cost plus the next cell's estimate exceeds the ceiling THE SYSTEM SHALL stop before that cell.  (F-6)
- AC-5: WHEN a Claude cell starts with `ANTHROPIC_API_KEY` set THE SYSTEM SHALL exit before the model call; WHEN a cell's stream reports a usage-limit rejection THE SYSTEM SHALL record the cell as `infra:quota`, start no further cell, and leave completed rows reusable.  (F-6, G-5)
- AC-6: WHEN `analyze.py` reports a run THE SYSTEM SHALL label the power readout informational and SHALL NOT emit a task-growth gate verdict, and `PHASE4_PLAN.md` SHALL carry a superseded marker naming `benchmark-gepa-overhaul`.  (F-6)

## Test Contracts

| Acceptance ID | Interface referent | Outermost stable seam | Expected failure | Mode | Interface version | Matrix rows |
| --- | --- | --- | --- | --- | --- | --- |
| AC-1 | `baselines.run_key`, trajectory sidecar writer | `run._run_single_in_repo` with a canned stream | `test_run_hardening.py::test_row_has_run_key_and_sidecar` fails: row lacks `run_key` and no sidecar file exists; `test_run_hardening.py::test_sidecar_is_untruncated` fails: the canned stream holds a tool input and a tool output each longer than 200 characters, and the sidecar lacks either one byte-for-byte | tracer | | |
| AC-2 | `baselines.lookup` / `baselines.store` | `run.main` with a pre-seeded result store holding one completed row, one error row, and one `infra:quota` row with no `error` field | `test_baselines.py::test_completed_row_reused_error_row_rerun` fails: runner invoked for the completed row's cell, not invoked for the error row's cell or the quota row's cell, or the run's output JSONL lacks the completed row with `reused: true`; `test_baselines.py::test_run_key_changes_per_input` fails: changing any one of the harness digest inputs, task digest inputs, env fingerprint inputs, `git_sha`, or `binary_sha256` leaves `run_key` unchanged | tracer | | |
| AC-3 | `run.main` baseline drift check, `run.build_runner_env` | `run.main` with a pre-seeded result store and stubbed CLI version and env fingerprint; `run.build_runner_env` return value | `test_baselines.py::test_baseline_drift_requires_refreeze` fails: with a stored baseline row under an older CLI version or env fingerprint, the run starts without the flag; `test_baselines.py::test_refrozen_baselines_do_not_block` fails: after a run with `--refreeze-baselines`, the next run with the same CLI and fingerprint refuses to start; `test_baselines.py::test_first_baseline_runs` fails: an empty store refuses a baseline cell; `test_run_hardening.py::test_runner_env_disables_autoupdater` fails: the env returned for a claude and a codex cell lacks `DISABLE_AUTOUPDATER=1` | tracer | | |
| AC-4 | `run.py --max-usd` spend ledger (F-6) | `run.main` argument parsing and cell loop; `parse` on a fixture stream without `total_cost_usd` | `test_run_hardening.py::test_paid_run_requires_max_usd` fails: run starts without the flag; `test_parse.py::test_missing_native_cost_uses_pricing` fails: cost is 0.0 with no `cost_source: pricing`; `test_parse.py::test_present_native_cost_is_used` fails: a result event with `total_cost_usd` yields a different cost or no `cost_source: native`; `test_run_hardening.py::test_ceiling_stops_before_crossing_cell` fails: with `--max-usd 1.0`, stored mean cost 0.6 for the next task-arm, and 0.5 already spent, the next runner is still invoked; `test_run_hardening.py::test_cell_estimate_tiers` fails: the estimate does not fall from stored mean to run maximum to `--cell-estimate-usd`; `test_run_hardening.py::test_failed_cells_count_toward_ceiling` fails: with `--max-usd 1.0`, `--cell-estimate-usd 0.4`, and a stubbed runner whose every cell ends `error_max_budget_usd` without a result cost, a third runner is invoked | tracer | | |
| AC-5 | `run.build_runner_env` auth guard and quota classifier (F-6) | `run.main` with a stubbed runner whose first cell replays the synthetic quota-rejection stream; `run.main` with `ANTHROPIC_API_KEY` set in the environment | `test_run_hardening.py::test_quota_rejection_stops_run` fails: the second planned cell's runner is invoked after a rejected `rate_limit_event`, or the first cell's stored row is not marked `infra:quota`; `test_run_hardening.py::test_api_key_refuses_claude_cell` fails: with `ANTHROPIC_API_KEY` set, the stubbed claude runner is invoked or the run exits 0 | tracer | | |
| AC-6 | `analyze._power_readout` (F-6) | `analyze.main` on a fixture JSONL | `test_analyze.py::test_power_readout_is_informational` fails: output contains `grow TASK pool` | tracer | | |

## Non-goals
- External task adapters, panels, the model judge, and the evolution loop (parent curds c2 to c5).
- Changes to `src/**`, `prompts/**`, or any tilth binary behavior.
- Concurrency in the cell loop; cells stay serial and Codex keeps using the host `CODEX_HOME`, as today.
- Changing the existing per-cell `--max-budget-usd` semantics.
- Bumping the crate or npm version (fork law).
- Recording a real `claude -p` OAuth stream; this cloud container holds no `CLAUDE_CODE_OAUTH_TOKEN`, so the maintainer records and commits it locally as follow-up `bench-oauth-fixture`, which also settles whether OAuth streams carry `total_cost_usd`.

## Provenance (tier 2 only)
- briesearch: `claude setup-token` gives a one-year static OAuth token, `ANTHROPIC_API_KEY` overrides it under `-p`, `--bare` refuses OAuth, `total_cost_usd` is a client-side estimate, and quota surfaces as `rate_limit_event` and "hit your … limit" results; artifact: /root/.local/share/cheese/paulnsorensen-tilth/research/benchmark-subscription-harness-auth/benchmark-subscription-harness-auth.md
