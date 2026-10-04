status: ok
next: mold
artifact: 
Benchmark overhaul: FeatureBench plus Gin render-context plus a few cherry-picked tasks, in a GEPA-able format with frozen single-run baselines and a model judge.
Research is done and no code has changed; the next move is to design the result format and loop.

work_id: benchmark-gepa-overhaul
revision_id: rev-1a7eade2e07e
record_digest: sha256:19fa7cbd81c65c9f9a33c477817c2266515cac729b9fd6e2a5e8b1a80e0ea8d3
projection_digest: sha256:63dcc5a84fd6792c762bd4d40a8759e53aca88305785af38f2a25a4547d7d5f7
durability: repo-snapshot
schema_version: 4

# Wheypoint benchmark-gepa-overhaul @ rev-1a7eade2e07e

## Gates

none

## Open entries

- q-2c37af8298a1 (question) — How should FeatureBench tasks execute: Harbor, a forked native FeatureBench harness with MCP injection, or our own runner calling the upstream grader, given that results must land in our JSONL schema?
- q-3c41b7003341 (question) — Which other tasks should be cherry-picked beyond FeatureBench and Gin (for example SWE-bench Multilingual or Pro V2 HARD for Go, Rust, and TypeScript coverage), and should they be fixed before any results are seen?
- q-d7019a19b70e (question) — What does the model judge score (structural applicability from the gold patch, trajectory quality, or both), which model judges, and how is it calibrated against a small hand-labeled set?
- q-1493c4dba7ee (question) — What does GEPA optimize (prompts/mcp.md, tool descriptions in src/mcp/tools/definitions.rs, or both), and what dev/test split and stopping rule prevent overfitting to the panel?
- q-25a8afaf0837 (question) — Does the user authorize a bounded exception to the Phase 4 power gate in benchmark/PHASE4_PLAN.md before any paid FeatureBench or GEPA runs?
- q-fc542ab6da9b (question) — How should FeatureBench tasks execute: Harbor, a forked native FeatureBench harness with MCP injection, or our own runner calling the upstream grader, given that results must land in our JSONL schema?
- q-ae2bc7f85733 (question) — What does the model judge score (structural applicability from the gold patch, trajectory quality, or both), which model judges, and how is it calibrated against a small hand-labeled set?
- q-93edc394f6ef (question) — What does GEPA optimize (prompts/mcp.md, tool descriptions in src/mcp/tools/definitions.rs, or both), and what dev/test split and stopping rule prevent overfitting to the panel?
- q-2d8757d29932 (question) — Does the user authorize a bounded exception to the Phase 4 power gate in benchmark/PHASE4_PLAN.md before any paid FeatureBench or GEPA runs?

## Decisions

- d-e270714317ea (decision) — Store stock Claude Code and stock Codex baseline runs once, keyed by model ID, agent CLI version, dataset revision, effort, and timeout; rerun a baseline only when its key changes.
  rationale: Published leaderboards have no per-instance rows for current stock agents, so a frozen local baseline is the only comparable control that avoids rerunning the A/B.
- d-e9b99bf42ae6 (decision) — Filed the structural write gap as issue #310 (a rewrite op in tilth_write).
  rationale: Gin render-context is essentially one structural rewrite, and tilth only has structural search today.
- d-7779503a215c (decision) — The panel is FeatureBench (a small Lite subset such as Pareto-12) plus Gin render-context plus the three existing hard tasks plus one Go and one fast Rust SWE-bench Multilingual instance.
  rationale: The user chose both cherry-pick options; external Go/Rust tasks cover languages FeatureBench lacks, and a single fast Rust task bounds build and test wait time.
- d-30de4bf90841 (decision) — Store stock Claude Code and stock Codex baseline runs once, keyed by model ID, agent CLI version, dataset revision, effort, and timeout; rerun a baseline only when its key changes.
  rationale: Published leaderboards have no per-instance rows for current stock agents, so a frozen local baseline is the only comparable control that avoids rerunning the A/B.
- d-ee506b21cac7 (decision) — Filed the structural write gap as issue #310 (a rewrite op in tilth_write).
  rationale: Gin render-context is essentially one structural rewrite, and tilth only has structural search today.

## Directives

- v-6429b394c5eb (directive) — Grow the benchmark: run FeatureBench and our Gin render-context task, and maybe cherry-pick a couple of other tasks.
  > we want to beef up the benchmark, and potentially run feature bench AND our gin  (and maybe cherry pick a couple of others
- v-c95480513135 (directive) — Change the benchmark format so it can be optimized with GEPA, and run the models once so later iterations compare against those stored runs.
  > we want to change the benchmark format such that it can be GEPA'd and run the models ONCE so we iterate against them
- v-a51fba57822c (directive) — The auto-research model must read the actual result JSONL and generate its own hypotheses; Harbor may not fit that.
  > i want the auto research model to be able to run against the actual JSONL and figure out and think of items, so I'm not sure harbor makes sense for this
- v-d732d0f12e7d (directive) — Prefer a model judge over manual task tagging for deciding where structural (ast-grep) tools apply.
  > tagging vs just having a model jduge, im leaing towarad a model judge
- v-cda4256d6700 (directive) — Grow the benchmark: run FeatureBench and our Gin render-context task, and maybe cherry-pick a couple of other tasks.
  > we want to beef up the benchmark, and potentially run feature bench AND our gin  (and maybe cherry pick a couple of others
- v-efbaa558b089 (directive) — Change the benchmark format so it can be optimized with GEPA, and run the models once so later iterations compare against those stored runs.
  > we want to change the benchmark format such that it can be GEPA'd and run the models ONCE so we iterate against them
- v-cfef90601837 (directive) — The auto-research model must read the actual result JSONL and generate its own hypotheses; Harbor may not fit that.
  > i want the auto research model to be able to run against the actual JSONL and figure out and think of items, so I'm not sure harbor makes sense for this
- v-f769bd6025fa (directive) — Prefer a model judge over manual task tagging for deciding where structural (ast-grep) tools apply.
  > tagging vs just having a model jduge, im leaing towarad a model judge
- v-96baa04fff6a (directive) — Cherry-pick both the existing hard tasks (rg_search_dispatch, rg_trait_implementors, gin_servehttp_flow) and two external Go/Rust SWE-bench Multilingual instances; keep the Rust side minimal to limit waiting time.
  > probably A and B. keep it minimal on the rust as to minimize waiting time though

## Notes

\## Where this stands

Research only; no code changed in this session.
The session reviewed PR #284 (FeatureBench integration research, wiki-only, open, merge state dirty) and PR #280 (Codex runner, strict arms, `gin_edit_render_context`, merged as 9fa37b51).

\## Findings a cold reader needs

- Size: Gin render-context touches 20 files (14 production), 90 edit sites, +165/-72 lines.
- FeatureBench Level 1 averages 15.7 files, 790 solution lines, 29.2 functions, 62.7 fail-to-pass tests (full set; no Lite-only stats exist).
- Breadth is similar; FeatureBench is roughly 3x the code and is feature implementation, not mechanical migration.
- FeatureBench is Python-only (200 tasks, 24 repos; Lite 30 = 26 L1 + 4 L2; fast 100); MIT for harness and dataset.
- Gin's trusted reference (`benchmark/tasks/gin_render_context_fixtures/reference.py`) is a regex over `func (r X) Render(w http.ResponseWriter)`, so it is the clearest ast-grep showcase.
- Published scores: FeatureBench v1.0 shows stock Claude Code + Opus 4.5 (Lite 59.1% passed / 20% resolved) and Codex + GPT-5.1-Codex (60.2 / 20); v1.1 Lite and full tables are empty.
- No per-instance FeatureBench results or trajectories are published.
- SWE-bench Pro V2 HARD has current stock Claude Code and Codex rows via a third-party mirror (unverified); per-task files not found.
- COSPA publishes per-task verdicts for its Pareto-12 panel, but only for the Pi agent.
- Conclusion: published numbers are sanity checks, not a control arm.
- Native FeatureBench Claude Code agent has no MCP option; Harbor's Claude Code and Codex agents accept `[[environment.mcp_servers]]` in `task.toml`.
- tilth has structural search (`{pattern, language}`) but no structural write op; filed as #310.
- `prompts/mcp.md:16` and `AGENTS.md` still route imports to retired `tilth_deps`; queued as a separate task card.
- `benchmark/parse.py` counts batch sizes and write op kinds but does not count `pattern` search entries separately from `query` entries.

\## Terms

- Pareto-12: COSPA's 12-task FeatureBench Lite Level-1 panel, chosen for repository coverage and fast gold verification. Not an official split; IDs are in `configs/featurebench_lite_pareto12_v1.json` in shisa-ai/cospa.
- `--max-cells`: our runner's hard cap on benchmark cells, where one cell is one task x model x arm x repetition. It caps calls, not dollars.

\## Tension to resolve in design

GEPA needs fresh rollouts of each candidate prompt, so only the stock baseline arms can be frozen.
The tilth arm must rerun for each candidate, but on a small dev panel; the frozen baseline JSONL halves recurring spend and supplies the comparison.
GEPA's reflection step reads trajectories, which fits the directive that the research model reasons over raw JSONL.

\## Cost reference (#284 assumptions, $3-$10 per attempt)

Pareto-12 with two arms and one repetition costs $72-$240; four arms double it.
`benchmark/PHASE4_PLAN.md` still gates paid task growth on the power readout.

## Context

- benchmark/run.py
- benchmark/parse.py
- benchmark/analyze.py
- benchmark/PHASE4_PLAN.md
- benchmark/tasks/gin_render_context_tasks.py
- benchmark/tasks/gin_render_context_fixtures/reference.py
- benchmark/experiments/upstream-fork.json
- .claude/skills/tilth-benchmark/SKILL.md
- prompts/mcp.md
- src/mcp/tools/definitions.rs
- PR#284
- PR#280
- issue#310

## Artifacts

- benchmark/PHASE4_PLAN.md (digest: sha256:4b18d6d40682a5c30aa171a8cedb2251c1eed6834f7dad8d3c36daeefafcdb15, revision: rev-1a7eade2e07e)
- benchmark/parse.py (digest: sha256:b0765182ad28fc5b40b8a398c7a6c6367a28e7a524f6b4b9da40fb6399e9c1a7, revision: rev-1a7eade2e07e)
- benchmark/tasks/gin_render_context_fixtures/reference.py (digest: sha256:56befd129bd68911782775e6c803e54002c8e18403bf861c175ba8f3d149d0b8, revision: rev-1a7eade2e07e)
- https://github.com/paulnsorensen/tilth/pull/284 (revision: rev-1a7eade2e07e)
- https://github.com/paulnsorensen/tilth/issues/310 (revision: rev-1a7eade2e07e)
- https://raw.githubusercontent.com/shisa-ai/cospa/main/configs/featurebench_lite_pareto12_v1.json (revision: rev-1a7eade2e07e)
- https://libercoders.github.io/FeatureBench/leaderboard (revision: rev-1a7eade2e07e)

## Links

none

## Lineage

none

## Decision dossier

### Fork: FeatureBench execution route
Prior leaning: Our runner owns the agent run and JSONL, and calls the pinned upstream grader in a fresh container; the user doubts Harbor fits a JSONL-reading research loop.
- Option: Harbor
  Evidence: https://github.com/harbor-framework/harbor/blob/main/adapters/featurebench/README.md
  Evidence: Harbor Claude Code and Codex agents read [[environment.mcp_servers]] from task.toml
  Breaks: Results land in Harbor's format, not our JSONL; scoring shares the agent container unless separate verification is configured; parity with native was 53/60.
- Option: Fork native FeatureBench
  Evidence: https://github.com/LiberCoders/FeatureBench
  Evidence: featurebench/infer/agents/claude_code.py has no MCP option
  Breaks: Needs an MCP-injection patch and a JSONL translator; we maintain a fork.
- Option: Our runner plus upstream grader
  Evidence: benchmark/run.py
  Evidence: PR#284 featurebench-integration.md advises against importing into the Task registry for grading
  Breaks: We build container lifecycle; grading fidelity must be proven with gold, empty, and tampered controls.

### Fork: Task applicability: tagging or model judge
Prior leaning: Model judge, optionally seeded with deterministic diff-shape features as judge input.
- Option: Model judge
  Evidence: user directive in this session
  Breaks: Needs calibration and adds judge cost and variance.
- Option: Deterministic tagging from gold-patch shape
  Evidence: benchmark/tasks/gin_render_context_fixtures/reference.py
  Breaks: Misses applicability that is not visible as repeated diff shape.
