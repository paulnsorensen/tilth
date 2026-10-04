status: gated: 1 open gating entry: q-8e5e7ae9bea3
next: mold
artifact: 
Benchmark overhaul is paused in /mold with a validated parent spec on hold and the c1 result-substrate child awaiting its last coherence round before an approved /cook.
The parent hold needs a user call; the c1 child resumes with a round-2 taste verdict, then approve, finalize, and cook.

work_id: benchmark-gepa-overhaul
revision_id: rev-a550cdacbc50
record_digest: sha256:001aaa1588b6f23bcbb55d0d373fc82fde90f71580c02368158b5d36a592ba2c
projection_digest: sha256:d16a9dfabe657ecff82c50b465e31f01ff87d1cc98bc1e79379a55b71d5ab310
durability: repo-snapshot
schema_version: 4

# Wheypoint benchmark-gepa-overhaul @ rev-a550cdacbc50

## Gates

- q-8e5e7ae9bea3 — Should a fresh fork-coherence round run on the parent spec now that F-5 was reopened by the third failed verdict and the AC-19 proposer-isolation fix is folded in, or should F-5 itself be revisited first?

## Open entries

- q-14f2553225d4 (question) — Does the c1 child bench-result-substrate pass its round-2 coherence verdict, the last correction round, with the r1 test-contract gaps now folded in?
- q-a53bc10901e1 (question) — Who records the scrubbed real claude -p OAuth stream fixture that c1 needs, given this cloud container may not hold an authenticated subscription login?
- q-e4d71f639897 (question) — Does Anthropic's explicitly-permit clause in the Consumer Terms cover setup-token benchmark runs on a subscription?

## Decisions

- d-e270714317ea (decision) — Store stock Claude Code and stock Codex baseline runs once, keyed by model ID, agent CLI version, dataset revision, effort, and timeout; rerun a baseline only when its key changes.
  rationale: Published leaderboards have no per-instance rows for current stock agents, so a frozen local baseline is the only comparable control that avoids rerunning the A/B.
- d-e9b99bf42ae6 (decision) — Filed the structural write gap as issue #310 (a rewrite op in tilth_write).
  rationale: Gin render-context is essentially one structural rewrite, and tilth only has structural search today.
- d-7779503a215c (decision) — The panel is FeatureBench (a small Lite subset such as Pareto-12) plus Gin render-context plus the three existing hard tasks plus one Go and one fast Rust SWE-bench Multilingual instance.
  rationale: The user chose both cherry-pick options; external Go/Rust tasks cover languages FeatureBench lacks, and a single fast Rust task bounds build and test wait time.
- d-3833000bc764 (decision) — F-1: FeatureBench and SWE-bench Multilingual tasks run fully native with a local checkout without upstream history, a local uv venv or Go/Rust toolchain, and our replay of FAIL_TO_PASS/PASS_TO_PASS tests; no Docker.
  rationale: The user picked option A after research showed grading fidelity comes from host preflight, not images, and the harness already runs containerless.
- d-9f95009c6cad (decision) — Claude cells, judge, and proposer authenticate through claude -p with CLAUDE_CODE_OAUTH_TOKEN from claude setup-token, refuse to start when ANTHROPIC_API_KEY is set, stop on usage-limit rejection as infra:quota, and run serially with Codex on the host CODEX_HOME.
  rationale: Follows the subscription directive and the research findings on API key precedence, --bare, and Codex auth.json rotation.
- d-3c324cdca365 (decision) — Split the result substrate (parent curd c1) into the early child spec bench-result-substrate; the user selected Cook here and replied cook it.
  rationale: c1 does not depend on the reopened F-5; the recorded approval is bound to an earlier byte version and must be re-bound after the child spec settles.

## Directives

- v-6429b394c5eb (directive) — Grow the benchmark: run FeatureBench and our Gin render-context task, and maybe cherry-pick a couple of other tasks.
  > we want to beef up the benchmark, and potentially run feature bench AND our gin  (and maybe cherry pick a couple of others
- v-c95480513135 (directive) — Change the benchmark format so it can be optimized with GEPA, and run the models once so later iterations compare against those stored runs.
  > we want to change the benchmark format such that it can be GEPA'd and run the models ONCE so we iterate against them
- v-a51fba57822c (directive) — The auto-research model must read the actual result JSONL and generate its own hypotheses; Harbor may not fit that.
  > i want the auto research model to be able to run against the actual JSONL and figure out and think of items, so I'm not sure harbor makes sense for this
- v-d732d0f12e7d (directive) — Prefer a model judge over manual task tagging for deciding where structural (ast-grep) tools apply.
  > tagging vs just having a model jduge, im leaing towarad a model judge
- v-96baa04fff6a (directive) — Cherry-pick both the existing hard tasks (rg_search_dispatch, rg_trait_implementors, gin_servehttp_flow) and two external Go/Rust SWE-bench Multilingual instances; keep the Rust side minimal to limit waiting time.
  > probably A and B. keep it minimal on the rust as to minimize waiting time though
- v-769ca89e994f (directive) — Run the benchmark on our own machine with our Claude and Codex subscriptions, not an API key, and without running multiple evals per attempt.
  > Ultimately we want to run this on our machien with our subscription, not with an API key if we don't have to. /briesearch how we can achieve this without running multiple evals
- v-e83198be51f8 (directive) — Prefer a pragmatic approach without containers.
  > Is there a more pragmatic way we can do this without containers?
- v-9c9ed533c0c6 (directive) — Drop a wheypoint and pause if the c1 child coherence round failed again.
  > let's drop a /wheypoint if it fails again and pause

## Notes

\## Where this stands

No repository code changed and nothing was committed; all artifacts live in the durable corpus under /root/.local/share/cheese/paulnsorensen-tilth/.
The parent spec benchmark-gepa-overhaul validates with validate-spec --strict and carries execution_holds taste-gate-exhausted.
Its round-0, round-1, and round-2 fork-coherence verdicts failed; round 2 tripped the third-failure stop and reopened F-5, and its four findings (CLI auto-update, local-task labels, proposer isolation as AC-19, at least one FeatureBench task) are already folded in.
The c1 child bench-result-substrate validates strict; its round-0 and round-1 verdicts failed and the round-1 gaps (ceiling-stop test, run-key-changes-per-input test) are folded into its Test Contracts.

\## Resume steps for c1

1. Dispatch a fresh-context reviewer for bench-result-substrate.verdict-r2.json and record it with taste-test --correction-round 2.
2. On pass, set the child status to approved, run mold.pyz approve with --kind scope --curd-id bench-result-substrate --response "cook it", then finalize --mode light with --taste-result, --ledger, and a coverage artifact.
3. Dispatch /cook on branch claude/beautiful-mccarthy-uprx32 only from a ready pointer.

\## Tool gotchas

- mold.pyz and wheypoint.pyz need python3.12; python3 is 3.11 here.
- Verdict JSON reflected_in must use bare lowercase section names, and the verdict must carry no extra top-level keys.
- Spec frontmatter values must not contain apostrophes.
- The Grounding probe column accepts only wiki and explorer.
- A route label such as Cook here is not execution consent; only a literal reply like cook it binds.

\## Follow-ups

- prompts/mcp.md:16 and AGENTS.md:18 still route imports to the retired tilth_deps.
- Issue #310 tracks the structural rewrite op for tilth_write.

## Context

- /root/.local/share/cheese/paulnsorensen-tilth/specs/benchmark-gepa-overhaul.md
- /root/.local/share/cheese/paulnsorensen-tilth/specs/benchmark-gepa-overhaul.ledger.json
- /root/.local/share/cheese/paulnsorensen-tilth/specs/benchmark-gepa-overhaul.verdict-r2.json
- /root/.local/share/cheese/paulnsorensen-tilth/specs/bench-result-substrate.md
- /root/.local/share/cheese/paulnsorensen-tilth/specs/bench-result-substrate.ledger.json
- /root/.local/share/cheese/paulnsorensen-tilth/specs/bench-result-substrate.verdict-r1.json
- /root/.local/share/cheese/paulnsorensen-tilth/handoffs/bench-result-substrate
- /root/.local/share/cheese/paulnsorensen-tilth/research/benchmark-subscription-harness-auth/benchmark-subscription-harness-auth.md
- benchmark/run.py
- benchmark/parse.py
- benchmark/analyze.py
- benchmark/PHASE4_PLAN.md
- benchmark/tasks/gin_render_context_tasks.py
- src/mcp/mod.rs
- src/mcp/tools/definitions.rs
- prompts/mcp.md
- PR#284
- issue#310

## Artifacts

- benchmark/PHASE4_PLAN.md (digest: sha256:4b18d6d40682a5c30aa171a8cedb2251c1eed6834f7dad8d3c36daeefafcdb15, revision: rev-1a7eade2e07e)
- benchmark/parse.py (digest: sha256:b0765182ad28fc5b40b8a398c7a6c6367a28e7a524f6b4b9da40fb6399e9c1a7, revision: rev-1a7eade2e07e)
- benchmark/tasks/gin_render_context_fixtures/reference.py (digest: sha256:56befd129bd68911782775e6c803e54002c8e18403bf861c175ba8f3d149d0b8, revision: rev-1a7eade2e07e)
- https://github.com/paulnsorensen/tilth/pull/284 (revision: rev-1a7eade2e07e)
- https://github.com/paulnsorensen/tilth/issues/310 (revision: rev-a550cdacbc50)
- https://raw.githubusercontent.com/shisa-ai/cospa/main/configs/featurebench_lite_pareto12_v1.json (revision: rev-1a7eade2e07e)
- https://libercoders.github.io/FeatureBench/leaderboard (revision: rev-1a7eade2e07e)
- xdg:paulnsorensen-tilth/specs/benchmark-gepa-overhaul.md (digest: sha256:e4fbb4299f52a505d63d011a9c5f1b37e6993f274020c1225c33cd1e1b5ae190, revision: rev-a550cdacbc50)
- xdg:paulnsorensen-tilth/specs/bench-result-substrate.md (digest: sha256:8dd86fc05dd2deb070be9c041c02dccdea56301214644bab4142cce6fa80c219, revision: rev-a550cdacbc50)
- xdg:paulnsorensen-tilth/research/benchmark-subscription-harness-auth/benchmark-subscription-harness-auth.md (digest: sha256:72069f457a5d8b34e1764006b58094dac323075857178781bf66f6a081b2745a, revision: rev-a550cdacbc50)

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

### Fork: Parent taste round after F-5 reopened
Prior leaning: Run a fresh taste round on the current draft, since every r2 finding is folded in and F-5 only reopened over the proposer isolation hole that AC-19 closes.
- Option: Fresh taste round
  Evidence: /root/.local/share/cheese/paulnsorensen-tilth/specs/benchmark-gepa-overhaul.verdict-r2.json
  Breaks: Needs explicit user authorization because the stop rule fired.
- Option: Revisit F-5
  Evidence: /root/.local/share/cheese/paulnsorensen-tilth/specs/benchmark-gepa-overhaul.md
  Breaks: May drop the src_patch proposer and narrow GEPA to prompt text only, changing F-2 and F-4 reach.
- Option: Keep shaping children only
  Evidence: /root/.local/share/cheese/paulnsorensen-tilth/specs/bench-result-substrate.md
  Breaks: Parent stays on hold; c2 to c5 cannot be finalized.
