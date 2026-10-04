---
slug: benchmark-gepa-overhaul
status: draft
source: mold-handshake
created: 2026-10-03
confidence: medium
leverage: ["contract", "irreversible-effects", "new-slice", "auth"]
gates_overridden: ["fork-taste-third-failure: on 2026-10-04 the user selected the umbrella restructure (option C) and authorized a fresh parent coherence round on the restructured draft", "fork-taste-fresh-sequence-exhausted: on 2026-10-04 the user accepted the umbrella with override after its fresh sequence (r3 to r5) failed on narrowing findings, all folded in with no fork reopened; each child spec carries its slice of the invariants through its own review"]
execution_holds: ["children-pending: child specs for c2 to c5 are not minted yet; each child owns its own taste round and Cook selection"]
agent_introduced_scope: ["optimize_anything", "harness digest", "result store", "prepare hook", "grade_details hook", "coding-agent proposer", "delta report", "recorded OAuth fixture", "evaluator cascade", "run key", "task digest", "env fingerprint", "trajectory sidecar", "max-usd", "dev/test split", "applicability label", "trajectory critique", "src_patch component", "applier-owned byte lock", "contamination flag", "infra:quota", "masked-state reconstruction"]
goal_coverage: {"G-1": "covered", "G-2": "covered", "G-3": "covered", "G-4": "covered", "G-5": "covered", "G-6": "covered"}
entity_referent_bindings: [{"noun": "cell", "verdict": "bound", "referent": "benchmark/run.py planned_cell_count", "citation": "benchmark/run.py:895-903", "note": "task x model x mode x repetition"}, {"noun": "arm", "verdict": "bound", "referent": "benchmark/config.py MODES and variants.py experiment arms", "citation": "benchmark/config.py:142-165", "note": "stock baseline arms are no_tilth (claude) and the codex runner without MCP"}, {"noun": "task", "verdict": "bound", "referent": "benchmark/tasks/base.py Task ABC and TASKS registry", "citation": "benchmark/tasks/base.py:58-179", "note": "external tasks subclass Task and override check_correctness, as gin_edit_render_context does"}, {"noun": "candidate", "verdict": "NEW ENTITY", "referent": "git commit plus binary_sha256 recorded in row.variant", "citation": "benchmark/run.py:803-857", "note": "reuses existing variant.git_sha and binary_sha256 fields"}, {"noun": "panel", "verdict": "NEW ENTITY", "referent": "benchmark/panels/<name>.json", "citation": "none", "note": "pre-registered task list with dev/test split"}, {"noun": "run key", "verdict": "NEW ENTITY", "referent": "benchmark/baselines.py run_key", "citation": "none", "note": "hash of model id, agent CLI version, task digest, env fingerprint, effort, timeout, arm, repetition"}, {"noun": "grader", "verdict": "NEW ENTITY", "referent": "benchmark/external/grade.py", "citation": "none", "note": "native replay of the instance FAIL_TO_PASS and PASS_TO_PASS tests in a clean copy, following the gin_edit_render_context held-out-test pattern (benchmark/tasks/gin_render_context_tasks.py:116-167)"}]
agent_resolution: []
gate_applicability:
  disposition: red-required
  work_class: behavior
  ui_surface: non-browser
landing:
  shape: diamond_stack
  layers: [["c1-result-substrate"], ["c2-external-task-adapter", "c4-model-judge"], ["c3-panel-manifest"], ["c5-evolution-loop"]]
  per_layer_green: required
  review_fixes: fold
---

# Benchmark overhaul: FeatureBench, Gin, and a GEPA evolution loop against frozen baselines

## Problem

Grow the benchmark with FeatureBench, the Gin render-context task, and a few cherry-picked tasks, in a format GEPA can optimize against once-run stored baselines, with a model judge for structural-tool applicability.

Today the harness cannot do any of that. Every run rewrites a fresh timestamped JSONL with no run key, so stock baselines are re-bought on every A/B (`benchmark/run.py:1144`). The row truncates arguments to 60–80 characters and drops `queries` and `edits` (`benchmark/run.py:291-319`); full traces exist only as unindexed stream files for the claude and codex runners (`benchmark/run.py:1145-1226`), so a reflection model has nothing structured to reason over. There is no external-task grading, no judge, and no spend ceiling (`--max-cells` caps calls only, `benchmark/run.py:906-911`). The MCP instruction text is compiled in (`src/mcp/mod.rs:76`), so any optimizer must produce builds, not text swaps. The maintainer runs on a Claude and Codex subscription, not an API key, and wants no container infrastructure. The maintainer feels this: every experiment costs a full re-run and yields only pass/fail.

This parent is an umbrella. It fixes the goals, the six forks, the curd graph, and the invariants that span curds. Each curd's child spec is the contract for its slice and carries that slice's detailed acceptance criteria and test contracts.

## Goals

- G-1: FeatureBench tasks run in our harness.
- G-2: The Gin render-context task is in the panel.
- G-3: A couple of other cherry-picked tasks are in the panel: the three local hard tasks plus one Go and one Rust SWE-bench Multilingual instance.
- G-4: The research model reasons over raw result JSONL.
- G-5: Stock baselines run once, are stored, and are reused.
- G-6: A model judge decides where structural tools apply.

## Non-goals

- A structural write op in `tilth_write`; tracked as issue #310 (user-filed in the prior session).
- Docker, Harbor, or native `fb infer`/`fb eval` execution (F-1 picked fully native).
- FeatureBench tasks that need a GPU, Level 2 tasks, and tasks whose environment does not build in a local `uv` venv or whose masked state cannot be reconstructed natively [AGENT-INTRODUCED].
- Matching published leaderboard numbers; published scores stay sanity checks.
- Auto-merging any optimizer winner (F-4: winners land as draft PRs).
- Bumping the crate or npm version (fork law).

## Deferred follow-ups

- **bench-gpu-host** — rent a GPU host to admit the GPU-flagged FeatureBench Lite tasks.
  - Destination: local_draft
  - State: prepared
  - Reference: this spec, Non-goals
- **bench-heavy-python-envs** — admit pandas, astropy, transformers, Lightning, and mlflow Pareto-12 tasks once a reproducible native environment recipe exists.
  - Destination: local_draft
  - State: prepared
  - Reference: this spec, Non-goals
- **bench-hgm-parent-selection** — swap GEPA parent selection for clade-metaproductivity (HGM) if frontier noise dominates.
  - Destination: local_draft
  - State: prepared
  - Reference: arXiv 2510.21614
- **fix-tilth-deps-route** — `prompts/mcp.md:16` and `AGENTS.md:18` still route imports to the retired `tilth_deps`.
  - Destination: local_draft
  - State: prepared
  - Reference: commit 4cafeea
- **bench-oauth-fixture** — the maintainer records a scrubbed real `claude -p` OAuth stream locally and commits it as a parser fixture; the cloud container holds no `CLAUDE_CODE_OAUTH_TOKEN`.
  - Destination: local_draft
  - State: prepared
  - Reference: child `bench-result-substrate`, Non-goals

## Grounding

| Probe | Outcome | Evidence |
| --- | --- | --- |
| wiki | hit | `hallouminate ground` on `repo:tilth:wiki`: `benchmark-harness-gotchas.md` (attach-or-abort guard, per-cell Claude config copies auth only, revoked tokens invalidated run `20260928_201559`, `--bare` refuses OAuth) |
| explorer | hit | Explorer digest at HEAD `4cafeea`: no run key or result reuse, `tool_sequence` drops `queries`/`edits` (`run.py:291-319`), instructions and tool descriptions compiled in with byte-lock and 2KB tests (`src/mcp/mod.rs:76,771,1249`), no judge code, power gate at `PHASE4_PLAN.md:3-25` with no exception process, `gin_edit_render_context` grades by held-out test replay (`gin_render_context_tasks.py:116-167`), local tasks carry only `required_strings`/`forbidden_strings` ground truth (`benchmark/tasks/base.py:21-31`); research sub-agents: `research/benchmark-subscription-harness-auth/benchmark-subscription-harness-auth.md`: subscription auth for `claude -p` (`setup-token`, API key precedence, `--bare`), Codex `auth.json` rotation, quota and `rate_limit_event`, FeatureBench dataset fields, SWE-bench Multilingual grading and candidate instances |

## Approach

Build a result substrate first, then two independent adapters, then the panel, then the loop. Each step names the child spec that owns its mechanics; the parent holds only what must stay true across them.

1. **Result substrate — c1, child `bench-result-substrate` (G-4, G-5, F-6).** Every row gets a run key over model, agent CLI version, harness digest, task digest, env fingerprint, effort, timeout, arm, repetition, and candidate identity, plus an untruncated trajectory sidecar. A result store answers any cell whose key matches a completed row, so baselines are bought once. `--max-usd` is required on paid runs, a set `ANTHROPIC_API_KEY` refuses the run, a usage-limit rejection stops it, runner CLIs are pinned, and the Phase 4 power gate is retired (F-6).
2. **External task adapter — c2 (F-1, G-1, G-3).** FeatureBench Lite Level 1 and SWE-bench Multilingual instances run fully native: a checkout without upstream history, a local `uv` venv or Go/Rust toolchain, and grading by native replay of FAIL_TO_PASS and PASS_TO_PASS tests on a clean copy, as `gin_edit_render_context` does. Held-out tests stay in the harness data directory. A task is admitted only after a native round trip (gold resolves, empty and tampered patches do not), and trajectories that fetch upstream source, read the harness data directory, or read the tilth `benchmark/` tree (where the Gin held-out tests live, `benchmark/tasks/gin_render_context_tasks.py:17`) mark the row contaminated; the scan runs on every panel cell, local tasks included. No Docker (F-1).
3. **Model judge — c4 (F-5, G-6).** A pinned judge labels each panel task for structural-tool applicability — external tasks from problem statement and gold patch, local tasks from prompt, ground truth, and any trusted reference — and critiques paid-tier rollouts from the same stripped record reflection receives, with no ground truth, gold patch, or held-out test in the critique call. Labels come from a fixed categorical set, so a label carries no task text. Labels and critiques are used only while the judge meets its calibration threshold. Labels slice reports and enter reflection; critiques enter reflection only; neither enters a score (F-5).
4. **Panel manifest — c3 (G-1, G-2, G-3).** A pre-registered panel lists the admitted FeatureBench tasks, `gin_edit_render_context`, `rg_search_dispatch`, `rg_trait_implementors`, `gin_servehttp_flow`, `gin-gonic__gin-3741`, and `sharkdp__bat-2650`. The three local hard tasks form the dev-only cheap tier; the rest get a stratified dev/test split that locks at the first recorded result.
5. **Evolution loop — c5 (F-2, F-3, F-4, F-5).** GEPA `optimize_anything` evolves a candidate that is a commit (F-2): text components under `prompts/**` from GEPA's reflection model, and a `src_patch` over `src/**` from a coding-agent proposer that works in an isolated worktree holding no benchmark files (F-5). Each candidate runs apply, `just check`, the cheap tier, then the paid tier; the score is mean grader correctness over dev tasks; frontier candidates are re-run before acceptance and get a delta report against the frozen baselines (F-3). The best frontier candidates are scored once on the test split, and the winner opens as a draft PR (F-4).

**Cross-curd invariants.** These are the parent contract, verified in `benchmark/tests/test_invariants.py` by the curd that closes each one:

- The only score is grader correctness. Labels and critiques never move it, and a contaminated rollout counts as incorrect (F-5, F-1).
- No optimizer-side model reads grader inputs. Reflective records and GEPA side info carry the grader verdict and counts (`correct`, `pass_rate`, F2P/P2P counts) and never grader-side material: `correctness_reason`, the task's ground-truth definition (`required_strings`, `forbidden_strings`), gold patches, or held-out test source or output. Text the agent itself produced in its trajectory or final answer stays in the record even when it matches a ground-truth string, since a correct answer to a local task must name those identifiers (`benchmark/tasks/ripgrep_tasks.py:26`); today `correctness_reason` names missing ground-truth strings (`benchmark/tasks/base.py:178-180`) and holds held-out test output (`benchmark/tasks/gin_render_context_tasks.py:152-153`). The proposer sees no `benchmark/` file, no panel file, and no harness data path. Critique inputs are that same stripped record. Contaminated rollouts contribute no reflective record or critique. Test-split tasks contribute no reflective record before finish (F-5, F-2).
- Every paid model call shares one `--max-usd` ceiling and one OAuth auth guard: agent cells, judge calls, reflection calls, and proposer calls (F-6).
- Baselines are reused, not re-bought, across runs and candidates. A baseline key that drifted (agent CLI version or env fingerprint) refuses the run until an explicit `--refreeze-baselines` buys them once under the new key (G-5, F-3).
- Nothing runs in a container (F-1).
- A winner only ever lands as an unmerged draft PR whose source changes stay inside `src/**` and `prompts/**` (F-4).

## Decisions

- F-1: External tasks run fully native — local checkout without upstream history, local `uv` venv or Go/Rust toolchain, and our replay of the instance's FAIL_TO_PASS/PASS_TO_PASS tests; no Docker — the user runs on a subscription on their own machine and wants no container infrastructure; host preflight replaces image fidelity.
- F-2: One evolutionary loop mutates code and text; a candidate is a commit — instructions are compiled in, and `variant.git_sha` / `binary_sha256` already prove what was served.
- F-3: The loop engine is GEPA `optimize_anything` with repeat runs on frontier candidates — trajectory reflection matches G-4 and it has the best measured budget (150–600 metric calls).
- F-4: The loop may mutate only `src/**` and `prompts/**`; winners land as human-reviewed draft PRs — user chose the widest search space; `just check` in the cascade is the backstop.
- F-5: The judge labels per-task applicability and critiques each rollout for reflection only; grader correctness is the only objective — the score comes only from the grader, which the F-4 allowlist puts outside the loop's reach, and the proposer isolation in AC-5 keeps grader inputs out of the proposer's view, so the loop cannot steer the score through labels, critiques, or leaked tests.
- _Minor decisions:_ the three existing hard tasks are cherry-picks and form the dev-only cheap tier; SWE-bench Multilingual picks are Go `gin-gonic__gin-3741` (fallback `prometheus__prometheus-14861`) and Rust `sharkdp__bat-2650` (fallback `tokio-rs__tokio-6724`) at HF revision `846e647b`; FeatureBench dataset v1.1, CPU-only Level 1; the judge model is pinned and its label cache key (task digest, judge model, prompt hash) is separate from the run key; Claude cells, the judge, and the reflection and proposer agents authenticate through `claude -p` with `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`; cells run one at a time and Codex uses the host `CODEX_HOME`; text components come from GEPA's reflection model and the `src_patch` from a coding agent; applier mechanics (`AGENTS.md` regeneration, byte-lock literal rewrite, `#[cfg(test)]` guard) and spend-estimate tiers live in the c5 and c1 child specs.
- F-6: The Phase 4 power gate is retired; every paid run requires an explicit spend ceiling — user retired the gate; `--max-usd` replaces it as the spend control.

## Acceptance

- AC-1: WHEN a paid run loads the pre-registered panel THE SYSTEM SHALL refuse it unless the panel holds at least one admitted FeatureBench Lite Level 1 task, `gin_edit_render_context`, `rg_search_dispatch`, `rg_trait_implementors`, `gin_servehttp_flow`, one admitted Go and one admitted Rust SWE-bench Multilingual instance, with every external task admitted by a native round trip on this host and no task run in a container.  (G-1, G-2, G-3, F-1; closed by c3)
- AC-2: WHEN the optimizer builds a reflective record for a dev-split rollout THE SYSTEM SHALL build it from the stored JSONL row and its untruncated trajectory sidecar, including every tool call's full input and output, with `correctness_reason` and every grader output removed and only `correct`, `pass_rate`, and F2P/P2P counts kept from grading, and SHALL build no record for a contaminated rollout; WHEN the judge critiques a rollout THE SYSTEM SHALL give it only that stripped record, with no ground truth, gold patch, or held-out test.  (G-4, F-5; closed by c5 on c1 and c4)
- AC-3: WHEN a `run.py` or `evolve.py` invocation requests a baseline-arm cell whose run key matches a completed stored row THE SYSTEM SHALL answer it from the store without a model call, so a second evolve run over an unchanged panel, agent CLI, and env fingerprint makes no baseline-arm model call; and WHEN a planned baseline cell has no completed row under its current key while one exists under a different key THE SYSTEM SHALL refuse to start unless `--refreeze-baselines` is passed.  (G-5, F-3; closed by c5 on c1)
- AC-4: WHEN the optimizer scores a candidate THE SYSTEM SHALL use mean grader correctness over paid-tier dev tasks as the only score, counting a contaminated rollout as incorrect and unchanged by any applicability label or critique; WHEN a panel run is analyzed or an evolve run starts THE SYSTEM SHALL hold a cached applicability label for every panel task and report the judge's measured agreement against the calibration set; WHEN the judge is calibrated THE SYSTEM SHALL slice analysis reports by label and pass labels and critiques into reflection, and WHEN it is uncalibrated THE SYSTEM SHALL pass neither and mark the report uncalibrated.  (G-6, F-5; closed by c5 on c4)
- AC-5: WHEN the optimizer proposes a candidate THE SYSTEM SHALL run the `src_patch` proposer in a worktree that holds no `benchmark/` file, panel file, or harness data path, reject the candidate when the proposer trajectory references any of them, and keep test-split tasks out of every reflective record until the finish step.  (F-5, F-2; closed by c5)
- AC-6: WHEN the optimizer run ends THE SYSTEM SHALL score the best frontier candidates once on the test split and open the winner as a draft PR, never merged by the system, whose commit passed `just check` and whose proposed changes touch only `src/**` and `prompts/**` plus the applier-regenerated `AGENTS.md`.  (F-4, F-2; closed by c5)
- AC-7: WHEN any paid model call starts — an agent cell, judge call, reflection call, or proposer call — THE SYSTEM SHALL refuse it without a run-wide `--max-usd` ceiling or with `ANTHROPIC_API_KEY` set, count its cost against that one ceiling, and stop before a call whose estimate would cross it; and `analyze.py` SHALL emit no task-growth gate verdict.  (F-6; closed by c5 on c1 and c4)
- AC-8: WHEN a candidate is accepted onto the GEPA frontier THE SYSTEM SHALL have re-run it the configured number of times and written a delta report (paired accuracy and cost per correct) against frozen baseline rows without re-running baseline arms; after an agent CLI or env fingerprint change those rows are the baselines refrozen once under `--refreeze-baselines`.  (F-3, G-5; closed by c5 on c1)

## Test Contracts

| Acceptance ID | Interface referent | Outermost stable seam | Expected failure | Mode | Interface version | Matrix rows |
| --- | --- | --- | --- | --- | --- | --- |
| AC-1 | `panels.load_panel`, `external.preflight.admit` (F-1) | `run.main --panel` with fixture panels and a subprocess recorder | `test_invariants.py::test_complete_panel_reaches_runner` fails: a fixture panel holding every required member does not start a paid run that invokes the stubbed runner for each member; `test_invariants.py::test_paid_panel_requires_every_task_family` fails: parametrized over each required member (an admitted FeatureBench task, `gin_edit_render_context`, `rg_search_dispatch`, `rg_trait_implementors`, `gin_servehttp_flow`, an admitted Go and an admitted Rust SWE-bench Multilingual instance), a panel missing that one member starts a paid run; `test_invariants.py::test_external_cell_never_invokes_container` fails: a recorded subprocess argv names `docker` or `podman` | tracer | | |
| AC-2 | `evolve.evaluate` reflective record builder over `baselines` rows, `judge.critique` input (F-3, F-5) | `evolve.evaluate` with a pre-seeded store, a canned sidecar, and a stub judge that echoes its whole input as the critique | `test_invariants.py::test_reflective_record_has_untruncated_tool_io` fails: the record lacks a tool output longer than 80 characters present in the sidecar; `test_invariants.py::test_reflective_record_strips_grader_text` fails: with `SECRET_GT` seeded in a row's `correctness_reason` and in the task ground-truth definition but absent from the canned sidecar and final answer, any reflective record, critique, or GEPA side info contains `SECRET_GT`; `test_invariants.py::test_agent_output_kept_verbatim` fails: an agent final answer and tool output containing a required string `AGENT_GT` are missing that text from the record; `test_invariants.py::test_contaminated_rollout_unreflected` fails: a rollout marked contaminated yields a reflective record or a critique | tracer | | |
| AC-3 | `baselines.lookup` from `evolve.main` (F-3) | `evolve.main` run twice over a fixture panel with a stubbed runner | `test_invariants.py::test_baselines_bought_once_then_reused` fails: on an empty store the first evolve run does not make exactly one baseline-arm runner call per baseline cell, or the unchanged second run refuses to start, makes any baseline-arm call, or lacks those rows with `reused: true` in its output JSONL; `test_invariants.py::test_env_drift_refuses_without_refreeze` fails: after the stubbed env fingerprint changes, the second evolve run starts without `--refreeze-baselines` | tracer | | |
| AC-4 | `evolve.cascade` score, `judge.applicability`, `judge.calibrate` (F-5) | `evolve.cascade` with stubbed stages and two label sets; `evolve.main` preflight with a stubbed judge | `test_invariants.py::test_score_ignores_labels_and_critiques` fails: the same rollouts score differently under different labels or critiques; `test_invariants.py::test_contaminated_rollout_scores_incorrect` fails: a rollout graded correct but marked contaminated raises the candidate score; `test_invariants.py::test_uncalibrated_judge_withholds_labels` fails: a reflective record carries a label below threshold; `test_invariants.py::test_calibrated_judge_labels_flow` fails: with a stub judge above threshold, a panel task lacks a label, the run log lacks the agreement value, the analysis report lacks per-label slices, or a reflective record lacks its label; `test_invariants.py::test_label_is_categorical` fails: a stub judge answering free text outside the label set yields a cached label | tracer | | |
| AC-5 | `evolve.propose_src_patch` isolation (F-5, F-2) | `evolve.propose_src_patch` with a stubbed agent runner | `test_invariants.py::test_proposer_cannot_see_grader_inputs` fails: the proposer worktree contains `benchmark/`, a trajectory reading a panel file yields a committed candidate, or a test-split task appears in a reflective record before finish | tracer | | |
| AC-6 | `evolve.finish`, `evolve.materialize` (F-4, F-2) | `evolve.finish` against a temp git repo with a stubbed PR client | `test_invariants.py::test_winner_is_unmerged_allowlisted_draft` fails: a merge call is recorded, the PR is not draft, or the winning commit changes a path outside `src/**`, `prompts/**`, and `AGENTS.md` | tracer | | |
| AC-7 | shared spend ledger and auth guard across `run`, `judge`, `evolve` (F-6) | `evolve.main` with stubbed cell, judge, reflection, and proposer clients | `test_invariants.py::test_one_ceiling_spans_all_paid_calls` fails: a judge, reflection, or proposer call runs after cumulative spend plus its estimate crosses `--max-usd`, or any of them starts with `ANTHROPIC_API_KEY` set | tracer | | |
| AC-8 | `evolve.cascade` frontier acceptance and delta report (F-3) | `evolve.cascade` with a pre-seeded result store | `test_invariants.py::test_frontier_needs_reruns_and_frozen_delta` fails: a candidate joins the frontier after one rollout, or the delta report triggers a baseline-arm runner call | tracer | | |

Each child spec carries the detailed per-slice rows; these rows test only the invariants above.

## Interface sketches

```text
slice:            benchmark (existing harness) + NEW SLICE benchmark/external/, benchmark/judge/, benchmark/evolve/
spine step:       workflow (run.py cell loop) + infra (native checkout and env builder, result store)
public interface: baselines.run_key(cell) -> str; baselines.lookup(key) -> Row | None; baselines.store(row)  (c1, F-3)
public interface: run.py --panel PATH --max-usd FLOAT --cell-estimate-usd FLOAT --refreeze-baselines --candidate-sha SHA  (c1/c3, F-6, F-2)
public interface: Task.prepare(workdir) and Task.grade_details() optional hooks, called by run.py when present  (c2, F-1)
public interface: external.ExternalTask(Task); external.featurebench.load / external.swebench_ml.load -> ExternalTask; external.preflight.admit(instance_id) -> PreflightVerdict; external.contamination.scan(sidecar_path, task) -> bool  (c2, F-1)
public interface: panels.load_panel(path) -> Panel(cheap, dev, test)  (c3, F-1)
public interface: judge.applicability(task) -> Label; judge.critique(row, trajectory) -> str; judge.calibrate(labels) -> Agreement  (c4, F-5)
public interface: evolve.main(--panel, --max-usd, --max-metric-calls, --seed-sha); evolve.propose_src_patch(worktree, records) -> diff; evolve.materialize(candidate) -> sha; evolve.cascade(sha) -> (score, side_info); evolve.finish(frontier)  (c5, F-2, F-3, F-4)
public interface: prompts/tools/search.md and prompts/tools/write.md via include_str! in src/mcp/tools/definitions.rs  (c5, F-2)
shared:           one spend ledger and one OAuth auth guard reused by run, judge, and evolve  (c1, F-6)
private:          each child owns its private mechanics (env builders, test-output parsers, masked-state reconstruction, sidecar format, digests, judge prompts, GEPA config, applier steps)
crust delta:      new run.py flags; new row fields (run_key, harness_digest, task_digest, env_fingerprint, cli_version, timeout_s, trajectory_path, cost_source, reused, pass_rate, contaminated); new Task hooks; new packages external/judge/evolve; two new prompt files; PHASE4_PLAN.md superseded
arrows:           run.py -> external, baselines, panels; evolve -> run.py, baselines, judge, panels; judge -> baselines (row and sidecar read); no arrow from tilth src/ into benchmark/
```

## Risks

- Masked-state reconstruction by reverse-applying the gold patch is [speculating]; the native admission round trip is the only proof, and tasks that fail it drop out. If no FeatureBench task survives, AC-1 refuses the panel and G-1 returns to the user rather than passing vacuously.
- About six of Pareto-12 are expected to build in a local `uv` venv (sympy, metaflow, seaborn ×2, xarray, pydantic) [speculating]; the FeatureBench share of the panel may be small.
- Native execution has no network or filesystem isolation for agent cells; tilth trusts absolute paths and Bash can read anywhere, so an agent can fetch the upstream fix or read the harness data directory. The contamination scan detects those known paths, not every leak.
- Whether OAuth streams carry `total_cost_usd` and `rate_limit_event` is untested [speculating]; cost falls back to `pricing.yaml` when the field is absent, and follow-up `bench-oauth-fixture` settles it.
- Host toolchain or agent CLI drift changes results; the run key keeps drifted rows apart, and any baseline drift needs an explicit one-time `--refreeze-baselines`, so a toolchain update between evolve runs costs one deliberate baseline re-buy.
- Anthropic's Consumer Terms bar automated access except via an API key "or where we otherwise explicitly permit it", while Anthropic documents `setup-token` for scripts; the exposure falls on the maintainer's account.
- Subscription quota (5-hour and weekly windows) bounds throughput; long runs will stop and resume from the store.
- Per-instance Pareto selection chases noise on expensive rollouts; frontier re-runs cost extra budget.
- Mutating `src/` invites overfitting to a small dev split; `just check` and the held-out test split are the guards.
- Retiring the power gate removes the statistical stop; `--max-usd` limits effort but not false-positive conclusions.

## Open questions

- [TBD] Judge agreement threshold and calibration-set size, owned by the c4 child spec (default proposal: every panel task for applicability plus 20 trajectories, Cohen's kappa ≥ 0.6).
- [TBD] Frontier re-run count and plateau window, owned by the c5 child spec (default proposal: 3 re-runs; stop after 5 rounds without dev improvement).

## Quality gates

- `just check`: exits 0 (fmt, clippy, cargo test, `tests/mcp_v2`, `tests/scripts`, bash-guard self-test).
- `cd benchmark && python3 -m pytest tests`: exits 0, including `test_invariants.py` and each child's new test modules.
- `python3 benchmark/external/preflight.py --panel benchmark/panels/<name>.json`: every admitted task reports gold resolved, empty and tampered unresolved, and at least one FeatureBench task is admitted.

## Curds

- **c1-result-substrate** — run key, trajectory sidecar, result store, `--max-usd` spend ledger, auth guard, quota stop, CLI pin, power gate retired. Covers G-4, G-5. Parent ACs: AC-2, AC-3, AC-7, AC-8 (substrate half). Depends on: none. Child: `bench-result-substrate` (`/root/.local/share/cheese/paulnsorensen-tilth/specs/bench-result-substrate.md`); route: Cook here in isolation, pending its coherence verdict. State: draft.
- **c2-external-task-adapter** — `prepare` and `grade_details` hooks, native checkout, masked-state reconstruction, `uv` and Go/Rust environments, native F2P/P2P grading, contamination flag, preflight. Covers G-1, G-3. Parent ACs: AC-1 (admission half). Depends on: c1. Child: `bench-external-tasks` (to mint from r3-pre-umbrella AC-1, AC-2, AC-3, AC-17). State: proposed.
- **c4-model-judge** — applicability labels and analysis slicing, per-rollout critiques, calibration gate. Covers G-6. Parent ACs: AC-4 (label half), AC-7 (judge calls). Depends on: c1. Child: `bench-model-judge` (to mint from r3-pre-umbrella AC-10, AC-11). State: proposed.
- **c3-panel-manifest** — pre-registered panel, task families, split lock. Covers G-1, G-2, G-3. Parent ACs: AC-1. Depends on: c2. Child: `bench-panel-manifest` (to mint from r3-pre-umbrella AC-4). State: proposed.
- **c5-evolution-loop** — tool-description move to prompt files, multi-component candidate with isolated `src_patch` proposer, applier, cascade, GEPA engine, delta reports, frontier re-runs, held-out scoring, draft PR. Covers G-4, G-5. Parent ACs: AC-2 to AC-8 (loop half). Depends on: c1, c3, c4. Child: `bench-evolution-loop` (to mint from r3-pre-umbrella AC-12 to AC-16, AC-18, AC-19). State: proposed.

## References

- Subscription and native-harness research: `/root/.local/share/cheese/paulnsorensen-tilth/research/benchmark-subscription-harness-auth/benchmark-subscription-harness-auth.md` (accessed 2026-10-03)
- FeatureBench dataset fields: https://huggingface.co/datasets/LiberCoders/FeatureBench (accessed 2026-10-03)
- COSPA Pareto-12: https://raw.githubusercontent.com/shisa-ai/cospa/main/configs/featurebench_lite_pareto12_v1.json (accessed 2026-10-03)
- SWE-bench Multilingual: https://www.swebench.com/multilingual.html (accessed 2026-10-03)
- Claude Code authentication: https://code.claude.com/docs/en/authentication (accessed 2026-10-03)
- GEPA `optimize_anything`: https://gepa-ai.github.io/gepa/blog/2026/02/18/introducing-optimize-anything/ (accessed 2026-10-03)
- Huxley-Gödel Machine: https://arxiv.org/html/2510.21614v2 (accessed 2026-10-03)
