# Benchmark selection and rigor

Keep local tool regressions. Add a separately reported external coding benchmark only after qualification and the execution gate.

## Recommended evaluation layers

<speculative> Use these layers instead of treating one benchmark as a complete coding assessment.

1. Use the [local inventory](benchmark-task-inventory.md) for fast tool regression checks.
2. Qualify one FeatureBench task without a model before attempting a paid comparison.
3. Use a fixed small FeatureBench panel for integration and large-effect screening.
4. Use a larger, independently frozen panel for confirmation when the pilot supports it.
5. Use HARD-51 as a stress test, not as a representative coding-work distribution.

The [FeatureBench paper](sources/featurebench.md) supports feature-development coverage.
[SWE-bench Pro V2](sources/benchmark-research.md#swe-bench-pro-v2) supplies HARD-51 and a stronger published replay protocol.
Neither source establishes Tilth compatibility or predicts the result of our comparison.

## Candidate benchmarks

Counts and implementation claims below come from primary sources checked on 2026-09-29.
The [source register](sources/benchmark-research.md) records sources and limitations.

| Candidate | Size / scope | Useful signal | Main limitation |
| --- | --- | --- | --- |
| FeatureBench | Full 200; fast 100; Lite 30 | Multi-file feature implementation | Lite includes GPU tasks; large implementations can make each attempt costly |
| COSPA FeatureBench Pareto-12 | 12 Level-1 tasks, 11 repositories | Cheap feature-task smoke panel | Selected partly for verifier cost; not a random or independent benchmark |
| Harbor FeatureBench Lite CPU panel | 23 of Lite's 30 tasks | CPU-only feature pilot | Custom subset; never label its score as full Lite |
| SWE-bench Pro V2 HARD-51 | 51 difficult cases from V2 | Long-horizon failure and tool stress | Selected through model failures; not representative |
| SWE-bench Pro V2 | 642 tasks, 11 repositories | Larger rigorous issue-resolution evaluation | Too expensive for the first integration check |
| SWE-bench Pro Verified | 731 tasks, including 102 refined cases | Audited Pro variant with fresh-image patch grading | Different dataset and harness from V2/HARD-51 |
| COSPA Polybench balanced64 | 64; 16 each Java, JavaScript, Python, TypeScript | Language and change-type coverage | Curated panel; Tilth integration remains untested |
| HAL SWE-bench Verified Mini | 50 sampled Verified tasks | Familiar small issue-resolution panel | HAL harness is archived; do not confuse it with other 50-task subsets |
| SWE-bench Multilingual | 300 tasks, 42 repositories, nine non-Python languages | Independent language breadth | Many small fixes; not a direct substitute for feature-development tasks |
| ContextBench | 1,136 tasks, 66 repositories, eight languages | Retrieval precision, recall, and efficiency | Diagnostic context metrics do not establish patch correctness |
| SWE-rebench leaderboard | Dated evaluation releases | Fresher issue holdouts | Freeze a release and quality filters; training data is not an evaluation holdout |
| SWE-bench Live | Frozen and growing, language-specific sets | Freshness and language breadth | Pin the exact release and prevent metadata leakage |
| Terminal-Bench 4.0 | Broad terminal tasks | Build, environment, and operational coverage | Not all coding; long timeouts can dominate cost |
| SWE-bench++ | Paper reports 11,133 tasks, 3,971 repositories, 11 languages | Features plus bug fixes at scale | Artifacts were not qualified as ready to run here |
| CCbench | About 180 tasks from small private-origin codebases | Feature construction | Neither a tiny suite nor independent proof against contamination |

COSPA reports three gold passes and three null failures for every selected Pareto-12 and balanced64 task.
That is useful qualification evidence, not a local reproduction.
Its Pareto-12 selection uses repository coverage and gold-verifier speed, not model ranking.
That still creates selection bias.[^1]

## Do not conflate the Pro variants

- Original public Pro has 731 tasks.
- Pro V2 has 642 tasks; HARD-51 is its difficulty-selected subset.
- Pro Verified has 731 tasks, with 102 refined instances.
- The reported defect rate near 30% concerns an audit of original Pro, not either newer variant.[^2]

V2 reports reference-patch success and empty-patch failure for all 642 tasks.
Its protocol captures the agent patch and grades it in a fresh sandbox.
It restricts network access and removes useful solution history.
HARD-51 selects tasks failed by at least two of five model families, after excluding three ambiguous cases.[^3]
These are maintainer-reported checks, not evidence that our local runner already provides those controls.

## Required rigor

<speculative> Require the following evidence before trusting a tool-effect comparison.

- Freeze task IDs, dataset revision, image digests, agent version, model ID, effort, prompts, and tool policy.
- Require a passing reference, failing unchanged source, and failing plausible incomplete patches.
- Protect hidden tests, reference patches, grading code, and solution-bearing history from the agent.
- Replay only permitted candidate changes in a fresh grading environment.
- Match time, token, dollar, CPU, RAM, GPU, network, and retry policies between arms.
- Record actual available tools, MCP attachment, tool calls, model usage, and termination reason.
- Abort or classify a missing Tilth server as infrastructure failure; never silently run a native-only “Tilth” arm.
- Preserve all planned task-attempt rows, including infrastructure failures, timeouts, and absent patches.
- Report fully resolved tasks separately from test-pass fractions.
- Predeclare the primary endpoint, retries, exclusions, stopping rule, and uncertainty method.
- Report correctness, total spend, wall time, and cost per correct result together.

The existing [harness gotchas](benchmark-harness-gotchas.md) explain why attachment and billing checks are mandatory.
Freshness reduces one contamination risk. It does not prove that training data excludes a task.

## What a small panel can tell us

A 12-task panel can expose integration defects and very large effects.
It cannot reliably establish modest general improvements.
One result changes accuracy by 8.33 percentage points at 12 tasks, versus two points at 50 tasks.

For one binary outcome per independent task pair, exact two-sided McNemar examples are:

| Tilth-only successes | Baseline-only successes | Exact p-value |
| ---: | ---: | ---: |
| 5 | 0 | 0.0625 |
| 6 | 0 | 0.03125 |
| 8 | 2 | 0.109375 |
| 10 | 0 | 0.001953125 |

These examples illustrate discordant-pair sensitivity; they are not benchmark results or a prospective power calculation.
Three repetitions reduce within-task variation. They do not triple the number of independent tasks.
Use task-clustered paired uncertainty, and report repository concentration.
Do not repeatedly inspect results and stop at significance without a prespecified sequential method.

## Existing plan and decision still needed

`benchmark/PHASE4_PLAN.md:1-76` accepts a power-gated task-growth plan.
It favors importing ContextBench tasks into this harness and requires a tool-set-swap spike for an external harness.
It also describes repeated significance checks; a confirmatory design needs an explicit stopping-rule correction.

<speculative> FeatureBench is a proposed additional feature-development layer, not an approved replacement for that plan.
Before implementation or paid execution, record whether the gate opens or the user explicitly authorizes a bounded exception.
Then choose the task manifest, native or Harbor route, arm policy, exact model, and budget.
See [integration requirements](featurebench-integration.md) and [cost planning](benchmark-cost-planning.md).

[^1]: [COSPA panel construction](https://github.com/shisa-ai/cospa/blob/main/docs/PANEL-CONSTRUCTION.md).
[^2]: [OpenAI, Separating signal from noise in coding evaluations](https://openai.com/index/separating-signal-from-noise-coding-evaluations/).
[^3]: [SWE-bench Pro V2 README](https://github.com/scaleapi/SWE-bench_Pro-os/blob/main/v2/README.md).

_Source: Primary-source research in the source register; local Phase 4 plan · Updated: 2026-09-29 · Supersedes: No accepted execution decision._
