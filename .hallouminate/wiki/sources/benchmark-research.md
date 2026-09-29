# Coding benchmark source register

This register preserves the primary evidence for the 2026-09-29 benchmark research.
It supports [benchmark selection](../benchmark-selection-and-rigor.md), [cost planning](../benchmark-cost-planning.md), and [FeatureBench integration](../featurebench-integration.md).

All entries are last verified on **2026-09-29**, unless a publication date is stated.
Repository links to `main` are moving sources, not immutable experiment pins.
Published claims below are not local reproductions.

## FeatureBench

The [FeatureBench paper source entry](featurebench.md) preserves the exact title, authorship, and solution-scale evidence.

LiberCoders' official [FeatureBench repository](https://github.com/LiberCoders/FeatureBench) documents the implementation and release history.
The [FeatureBench dataset](https://huggingface.co/datasets/LiberCoders/FeatureBench) identifies full 200 and Lite 30; the repository adds fast 100.
Level 1 restores feature implementations; Level 2 develops from scaffolding.
The paper's Level-1 averages are 790.2 solution lines, 15.7 files, and 29.2 functions.
These are not Lite-specific averages.
Dataset v1.1, released 2026-08-24, corrects task-statement inconsistencies and omissions.
The displayed leaderboard uses v1.0, so it does not establish current-model performance on v1.1.

## FeatureBench harnesses

LiberCoders' configuration and CLI reference documents define native inference and evaluation.
Canonical sources: [config.md](https://github.com/LiberCoders/FeatureBench/blob/main/docs/config.md), [infer_cli_arg.md](https://github.com/LiberCoders/FeatureBench/blob/main/docs/infer_cli_arg.md), and [harness_cli_arg.md](https://github.com/LiberCoders/FeatureBench/blob/main/docs/harness_cli_arg.md).

Their contribution is operational: version pinning, agent options, environment injection, attempt handling, and grading semantics.
Important constraints include mini-swe-only cost limiting, secret-bearing run metadata, default failed-attempt exclusion, and best-attempt summaries.
Use the [integration checklist](../featurebench-integration.md#native-featurebench-pitfalls) before constructing a run.

Harbor's **FeatureBench** adapter README documents conversion, execution configs, resource splits, and parity experiments.
Canonical source: [Harbor FeatureBench adapter](https://github.com/harbor-framework/harbor/blob/main/adapters/featurebench/README.md).
It lists Lite CPU-23/GPU-7 and full CPU-156/GPU-44.
It reports limited parity experiments; these do not validate Tilth, Sonnet, or an unpinned dataset revision.
Its README does not establish v1.1 mapping.

Harbor's agent implementation documents usable extension points through code.
Canonical source: [Claude Code agent](https://github.com/harbor-framework/harbor/blob/main/src/harbor/agents/installed/claude_code.py).
It supports effort, a dollar budget parameter, tool controls, and MCP registration.
These features still require a container-level Tilth test.

## Harbor verifier boundary

Harbor's **Separate Verifier** documentation explains opt-in isolated grading.
Canonical source: [Separate Verifier](https://docs.harborframework.com/core-concepts/tasks/separate-verifier).
The default can share the agent environment; separate verification requires configuration and explicit artifact transfer.
Do not infer fresh-state grading merely because a task runs through Harbor.

Harbor's [Custom Agents](https://docs.harborframework.com/core-concepts/agents/custom-agents) documentation describes installed and external agent extensions.
This supports a thin adapter design, not an assertion that this repository already has one.

## SWE-bench Pro V2

Scale AI's **SWE-bench Pro V2** repository documentation describes the 642-task V2 set and HARD-51.
Canonical source: [SWE-bench Pro V2](https://github.com/scaleapi/SWE-bench_Pro-os/blob/main/v2/README.md).

The release provides Harbor tasks, public container images, sanitized history, restricted inference networking, and fresh-sandbox patch replay.
It reports reference passes and empty-patch failures for all 642 tasks.
HARD-51 selects failures across multiple model families and excludes three ambiguous cases.
Its published task timeout is 50 minutes.
The original V1 public set has 731 tasks; that is not V2.
Example concurrency values do not select an equally sized task subset.
Our Tilth integration with this harness remains untested.

## SWE-bench Pro Verified

OpenCompass' **SWE-bench Pro Verified** guide describes a different 731-task refinement, including 102 refined instances.
Canonical source: [SWE-bench Pro Verified guide](https://agent-compass.mintlify.app/en/user_guide/modules/benchmarks/swebench_pro_verified).
Related artifacts: [AgentCompass](https://github.com/open-compass/AgentCompass), [dataset](https://huggingface.co/datasets/opencompass/SWEBench-Pro-Verified), and [paper](https://arxiv.org/html/2609.08149v2).

The guide separates inference from clean-image patch evaluation.
It describes metadata filtering, hidden-artifact removal, and code-host blocking.
The documented blacklist support applies to Docker, not every available execution backend.
The guide allocates four CPUs and 8 GiB per task and documents a 3,600-second evaluation timeout.
Its mini-swe-agent defaults include 250 steps and a $3 cost limit.
Those defaults do not price a Sonnet attempt or apply to native FeatureBench Claude Code.

## Original Pro audit

OpenAI's **Separating signal from noise in coding evaluations** is an evaluation audit, published July 2026.
Canonical source: [Separating signal from noise in coding evaluations](https://openai.com/index/separating-signal-from-noise-coding-evaluations/).
It reports substantial defects, near 30%, in original public Pro.
Its contribution is the need to qualify tasks and graders.
Do not transfer that defect rate to V2 or Pro Verified.

## COSPA

Shisa AI's **COSPA** repository and **Panel Construction** document describe curated low-cost evaluation panels.
Canonical sources: [COSPA](https://github.com/shisa-ai/cospa) and [Panel Construction](https://github.com/shisa-ai/cospa/blob/main/docs/PANEL-CONSTRUCTION.md).

FeatureBench Pareto-12 contains 12 Level-1 tasks from 11 repositories, drawn from Lite.
Selection combines stable pilot cases with fast reference verification and repository coverage.
It is not an official new FeatureBench split.
Polybench balanced64 contains 16 tasks per language across Java, JavaScript, Python, and TypeScript.
The maintainers report three gold passes and three null failures per selected task.
They describe isolated, no-network grading.
These checks are reported upstream, not rerun here.
The available agent adapters do not make Tilth a drop-in configuration.

## Other small and multilingual candidates

Princeton PLI's **HAL Harness** repository supplies SWE-bench Verified Mini, a 50-task sampled panel.
Canonical source: [HAL Harness](https://github.com/princeton-pli/hal-harness).
The repository is archived as of 2026-07-01.
Do not confuse its task list with unrelated optimized 50-task subsets.

The SWE-bench project's **SWE-bench Multilingual** benchmark page describes 300 curated tasks across 42 repositories and nine non-Python languages.
Canonical source: [SWE-bench Multilingual](https://www.swebench.com/multilingual.html).
Its gold patches have a median size of ten lines and a 95th percentile of 110 lines.
This supports language breadth, but not a claim of consistently large feature work.
The [official harness](https://github.com/SWE-bench/SWE-bench) provides patch grading.
Use unique run IDs; cached evaluation keyed by task and run must not be reused for a changed patch.

## Retrieval and fresh issue candidates

EuniAI's **ContextBench** repository describes 1,136 tasks, 66 repositories, and eight languages with annotated relevant context.
Canonical source: [ContextBench](https://github.com/EuniAI/ContextBench).
The [agent documentation](https://github.com/EuniAI/ContextBench/blob/main/docs/agents.md) explains trace extraction into files and spans.
A Tilth-aware extractor remains integration work.
Context recall, precision, and efficiency complement correctness; they do not replace executable grading.

The SWE-rebench authors' [V2 paper](https://arxiv.org/abs/2602.23866) describes 32,079 tasks across 3,617 repositories and 20 languages.
That training collection is not an unseen evaluation set.
Canonical evaluation artifacts: [SWE-rebench harness](https://github.com/SWE-rebench/SWE-bench-fork) and [leaderboard dataset](https://huggingface.co/datasets/nebius/SWE-rebench-leaderboard).
Freeze a dated release and inspect task-quality flags.

Microsoft's **SWE-bench Live** repository documents fresh issue collections and Python, multilingual, and Windows variants.
Canonical source: [SWE-bench Live](https://github.com/microsoft/SWE-bench-Live/blob/main/README.md).
Its August multilingual release reports 1,077 tasks across 431 repositories and eight languages.
Frozen subsets differ from growing collections.
Hints, test patches, and fail/pass metadata must not leak into inference.

## Broader task coverage

Terminal-Bench's **Terminal-Bench 4.0** release note describes the 2026-08-28 quality/resource revision.
Canonical source: [Terminal-Bench 4.0](https://www.tbench.ai/news/terminal-bench-4-0).
It reports eight task removals, 19 fixes, and an eight-hour agent timeout.
This supports broad terminal work, not a cheap coding-only comparison.

The **SWE-bench++** paper describes 11,133 tasks across 3,971 repositories and 11 languages.
Canonical source: [SWE-bench++ paper](https://arxiv.org/abs/2512.17419).
It includes feature and bug work.
The session does not qualify its runnable artifacts.

The **CCbench** project describes about 180 tasks from small private-origin codebases.
Canonical source: [CCbench](https://ccbench.org/).
It provides feature-development coverage, not a tiny suite or independent proof against contamination.

## Introducing Claude Sonnet 5.5

The [Introducing Claude Sonnet 5.5 source entry](sonnet-5-5.md) records Anthropic's 2026-09-28 announcement and price categories.
See [benchmark cost planning](../benchmark-cost-planning.md) for model-alias limits and scenario arithmetic.

_Source: Primary documents linked in each section · Updated: 2026-09-29 · Supersedes: No local run evidence._
