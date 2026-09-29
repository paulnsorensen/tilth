# FeatureBench integration

Use an existing FeatureBench grader. Add a thin Tilth experiment adapter instead of rebuilding the benchmark.

**Status: researched, not implemented or executed.**
The session finds native FeatureBench and Harbor harnesses.
Neither path is a verified Tilth integration.
This page proposes work; it does not authorize a paid run or replace the existing Phase 4 gate.[^1]

## Available harnesses

The [source register](sources/benchmark-research.md#featurebench-harnesses) records the native configuration, CLI, Harbor adapter, and Claude Code implementation sources.

| Route | Existing support | Work we still need |
| --- | --- | --- |
| Native `fb infer` / `fb eval` | Claude Code, Codex, OpenHands, Gemini CLI, mini-swe-agent; Docker images; patch export and grading | Inject pinned Tilth into the container agent; enforce arm policies; normalize telemetry and failures |
| Harbor FeatureBench adapter | Converted tasks, agent abstraction, CPU/GPU configs, oracle path | Verify dataset revision and grader parity; install Tilth; configure isolated MCP; validate fresh-verifier behavior |
| Direct import into our `Task` registry | Existing local model/mode reporting | Build container lifecycle and an external grader boundary; do not use response strings or candidate tests as the benchmark oracle |

<speculative> Start by qualifying Harbor's CPU route, because its Claude Code agent exposes MCP configuration and budget controls.
Keep native FeatureBench as the reference implementation for dataset and grading parity.
Prefer native execution if the adapter cannot preserve the selected dataset revision or grading contract.

Harbor's Claude Code implementation accepts an effort setting and `max_budget_usd`.
It supports stdio MCP registration through an isolated Claude configuration.
That capability is source evidence, not proof that Tilth works in a FeatureBench container.[^2]

## Dataset and hardware

FeatureBench publishes full 200, fast 100, and Lite 30 splits.
Dataset v1.1 corrects prompt inconsistencies and omissions.
Pin the resolved dataset revision, not only a mutable default.[^3]

Harbor's adapter divides Lite into 23 CPU and seven GPU tasks.
Its full configuration divides 200 tasks into 156 CPU and 44 GPU tasks.
Some Liger tasks require Ampere-or-newer GPUs.
Missing GPU resources can produce zero reward, which must not become a false model-failure claim.[^4]

For a CPU-only pilot, name the result “FeatureBench Lite CPU-23.”
A Pareto-12 run must use the exact COSPA manifest and retain that subset name.
Neither result is the full Lite score.

Required infrastructure:

- Docker access and a compatible Linux execution host.
- A pinned FeatureBench or Harbor environment, preferably installed through a locked `uv` environment.
- Container-compatible Tilth binaries for both compared revisions, with checksums and source SHAs.
- Image storage, memory, CPU, and disk quotas measured during reference qualification.
- Task-specific GPU resources only when the chosen manifest requires them.
- A model credential exposed only to the inference process that needs it.
- Separate inference and grading output locations, with per-task unique run IDs.

During this session, `uv` resolves on PATH.
The checks do not find `docker`, `fb`, or `harbor` on PATH.
No container, image download, GPU check, or capacity measurement runs.
This is a PATH observation, not proof that the machine has no container service.

## Amendments to our harness

The merged runner creates host worktrees or copied directories in `_agent_repo`.
`run_single` resolves a local registered task and calls its grader.
The base `Task.check_correctness` is not a FeatureBench adapter.[^5]
The new Codex isolation and strict-tool features do not supply container grading by themselves.

<speculative> Add only these integration responsibilities:

| Component | Required contract | Acceptance evidence |
| --- | --- | --- |
| Task manifest | Explicit IDs, split, level, revision, image digest, resource class | Frozen manifest; every ID resolves exactly once |
| External runner adapter | Launch pinned upstream harness and collect patch/trace artifacts | One no-model reference task completes end to end |
| Agent setup | Install Tilth inside the task container; isolate MCP and agent settings | Actual initialization lists the intended server and tools |
| Arm configuration | Same model, effort, permissions, budgets, prompts, and resources except the declared tool treatment | Saved config diff and tool-availability check |
| Tool enforcement | Define additive versus replacement mode; preserve common build/test access | No native-file fallback in a replacement arm; no Tilth in baseline |
| Trusted evaluation | Rebuild clean task state; replay permitted patch; inject trusted tests | Gold passes; empty, incomplete, and test-tampering patches fail |
| Result ingestion | Translate upstream outputs without changing success semantics | Golden-result fixtures cover success, failure, timeout, missing patch, and infrastructure error |
| Analysis | Keep the planned denominator and task pairing | Missing-row assertion and task-clustered paired report |
| Cost control | Per-attempt limit, global spend limit, bounded concurrency, explicit retry policy | Stop-path test; report actual billed usage and possible final-request overshoot |

Avoid a generic benchmark framework before a second concrete integration requires one.
Do not change exported task interfaces until callers and the analysis schema are inspected.
PR #277's JSON task loading and skill-plugin work remains a separate, open change at the research date.
This proposal does not assume that branch is merged.

## Protect the experiment and grader

The agent must not see reference patches, held-out tests, grading scripts, host credentials, or solution-bearing Git history.
Do not mount the host checkout, Docker socket, or home directory into the candidate environment.
Run Tilth inside the candidate boundary, with the correct container repository as `cwd`.

Harbor's default verification can share the agent environment.
Its separate-verifier feature is opt-in and needs explicit artifact transfer.[^6]
Inspect the actual FeatureBench task configuration.
Require patch-only replay into fresh state when the default does not meet the trusted-grader contract.

Keep network policy equal between arms.
Provision dependencies before inference where practical.
Restrict inference egress and run grading offline when the qualified task supports it.
Do not claim that all native or Harbor tasks already satisfy this policy.

A missing MCP attachment invalidates the treatment.
Record initialization, actual calls, allowed tools, fallback attempts, and agent termination.
Do not rely on `tilth --version` alone; this fork keeps a fixed package version.
See [historical harness failures](benchmark-harness-gotchas.md).

## Native FeatureBench pitfalls

The native CLI documentation establishes these constraints.[^7]

- Specify `--split lite`; inference otherwise defaults to full.
- Pin `--data-version` consistently for inference and evaluation.
- Distinguish agent `--version` from dataset version.
- Keep the shared download cache off unless its visibility is explicitly safe.
- Do not pass `--api-key` on the command line; the CLI can save it in run metadata.
- Keep configuration environment injection minimal; never publish secret-bearing metadata.
- Native `--cost-limit` applies to mini-swe-agent, not Claude Code.
- Avoid `--white` and `--without`; they change the task presentation.
- Avoid `--force-timeout`; inference status is not task correctness.
- Preserve failed inference attempts in the denominator; evaluation otherwise skips them by default.
- Use `--include-failed` when evaluating available failed-attempt patches.
- Do not treat absent patches as missing tasks or successful evaluations.
- Do not report default best-of-attempt summaries as pass@1.
- Distinguish test-pass fraction from fully resolved task rate.
- Native gold evaluation is documented for Level 1; qualify Level 2 separately.

Illustrative native commands, **not executed**:

```bash
fb infer --config-path config.toml --data-version v1.1 \
  --agent claude_code --model MODEL_ID --split lite \
  --task-id TASK_ID --n-attempts 1 --n-concurrent 1 --timeout 3600

fb eval -p RUN_OUTPUT_JSONL --data-version v1.1 \
  --split lite --task-id TASK_ID --include-failed
```

Resolve v1.1 to a recorded immutable revision before running.
Confirm the command schema against the pinned installation.
These commands do not install Tilth or enforce a Claude Code dollar cap.

Harbor provides `adapters/featurebench/featurebench_lite_docker_cpu.yaml`.
Its adapter README does not establish the exact v1.1 mapping.
Resolve that mapping before comparing native and Harbor results.[^4]
Explicit task IDs define a subset. Concurrency flags do not define subset size.

## Result record

<speculative> Preserve the upstream raw artifact and add a normalized row containing:

- Run ID, task ID, repository, level, subset, dataset revision, and image digest.
- Agent version, model ID, reasoning effort, tool policy, and Tilth source/binary identity.
- Repetition, attempt, seed where supported, resource limits, and elapsed time.
- Patch checksum, inference status, evaluation status, resolved flag, and test counts.
- Error class, termination reason, retry linkage, available tools, and actual tool counts.
- Input/output/cache token categories, provider-reported spend, and reconstructed spend.

Store secrets outside this record.
Record infrastructure errors separately, but never silently remove planned rows.

## Staged acceptance and remaining decisions

1. Confirm the Phase 4 gate or record an explicit bounded exception.
2. Choose native or Harbor, CPU-23 or Pareto-12, and the exact model and arm policy.
3. Install and pin the runtime without starting model calls.
4. Run gold, empty, incomplete, and tampered-test controls on one CPU task.
5. Verify native/adapter grading parity and fresh-state artifact transfer.
6. Run one authorized paired task with bounded spending and capture actual MCP use.
7. Audit billing, timeouts, missing rows, and success semantics.
8. Freeze the panel and analysis before a broader paid pilot.

Do not expand when a qualification control fails.
A 12-task pilot tests plumbing and large effects; it is not confirmatory proof.
See [cost planning](benchmark-cost-planning.md) and [statistical limits](benchmark-selection-and-rigor.md#what-a-small-panel-can-tell-us).

[^1]: `benchmark/PHASE4_PLAN.md:1-76`.
[^2]: [Harbor Claude Code agent](https://github.com/harbor-framework/harbor/blob/main/src/harbor/agents/installed/claude_code.py).
[^3]: [FeatureBench](https://github.com/LiberCoders/FeatureBench); [dataset](https://huggingface.co/datasets/LiberCoders/FeatureBench).
[^4]: [Harbor FeatureBench adapter](https://github.com/harbor-framework/harbor/blob/main/adapters/featurebench/README.md).
[^5]: `benchmark/run.py:_agent_repo`, `benchmark/run.py:run_single`, and `benchmark/tasks/base.py:143-186`, at `9fa37b51fb655294f6718ff4da1a00ba52a68a53`.
[^6]: [Harbor separate verifier](https://docs.harborframework.com/core-concepts/tasks/separate-verifier).
[^7]: Native [configuration](https://github.com/LiberCoders/FeatureBench/blob/main/docs/config.md), [inference CLI](https://github.com/LiberCoders/FeatureBench/blob/main/docs/infer_cli_arg.md), and [evaluation CLI](https://github.com/LiberCoders/FeatureBench/blob/main/docs/harness_cli_arg.md).

_Source: Upstream harness documentation and source inspection; local runner inspection · Updated: 2026-09-29 · Supersedes: Any assumption that FeatureBench is already runnable through our task registry._
