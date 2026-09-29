# Benchmark cost planning

Budget from measured attempts, not from the benchmark name or “Sonnet high.”
The numbers below are planning scenarios, not observed benchmark costs.

## Model identity correction

The session's price scenario uses Sonnet 5.5.
Anthropic's “Introducing Claude Sonnet 5.5” lists $2 per million input tokens and $10 per million output tokens.
Cache reads cost $0.20 per million tokens.[^1]
Cache writes and other billable categories need their own rates.

The local `sonnet` alias maps to `claude-sonnet-4-6`.
The `sonnet5` alias maps to `claude-sonnet-5`, not Sonnet 5.5.[^2]
Do not apply the 5.5 scenario to either alias without resolving the actual model.
Pin an exact provider model ID and record the resolved ID.

“High” changes reasoning effort and can change token use.
It is not a fixed per-task price or flat surcharge.
This session does not measure Sonnet high on Pro Verified or FeatureBench.

The [Sonnet 5.5 source entry](sources/sonnet-5-5.md) records the dated pricing source.
The [historical MCP cost analysis](mcp-cost-model-sonnet5.md) concerns a different measured experiment.
Its small-task cost and 97.5% baseline accuracy do not forecast these larger tasks.

## Full Pro Verified budget scenarios

Pro Verified contains 731 tasks.
For one attempt per task:

| Assumed mean spend per attempt | One arm | Two paired arms |
| --- | ---: | ---: |
| $3 | $2,193 | $4,386 |
| $5 | $3,655 | $7,310 |
| $10 | $7,310 | $14,620 |

These totals exclude infrastructure, failed setup, retries, and additional repetitions.
Three repetitions multiply the model-spend scenario by three.
The $3 figure is a budget assumption, not a measured expected price.

AgentCompass documents a $3 default cost limit for its mini-swe-agent configuration.
That is not a universal Sonnet cost or a native FeatureBench Claude Code cap.[^3]
A final request can exceed a soft remaining-budget check.
Verify the actual runner's enforcement and reserve headroom.

## Small paired panels

Formula: `tasks × arms × repetitions × assumed mean attempt spend`.
This table uses two arms and one repetition.

| Panel | Tasks | At $3/attempt | At $5/attempt | At $10/attempt |
| --- | ---: | ---: | ---: | ---: |
| COSPA FeatureBench Pareto-12 | 12 | $72 | $120 | $240 |
| Custom qualification panel | 20 | $120 | $200 | $400 |
| FeatureBench Lite CPU subset | 23 | $138 | $230 | $460 |
| Custom 25-task panel | 25 | $150 | $250 | $500 |
| FeatureBench Lite | 30 | $180 | $300 | $600 |
| HAL Mini | 50 | $300 | $500 | $1,000 |
| Pro V2 HARD-51 | 51 | $306 | $510 | $1,020 |
| COSPA Polybench balanced64 | 64 | $384 | $640 | $1,280 |

Do not infer equal attempt cost across panels.
Feature implementations, difficult issues, and GPU tasks can consume very different resources.
At $3 per attempt, three paired repetitions of Lite cost $540 before infrastructure.
Repeated attempts do not create new independent tasks.

## What to measure before approving volume

<speculative> Use a bounded pilot to measure:

- Provider-reported model spend, split by input, output, cache writes, and cache reads.
- Warm and cold setup time, evaluation time, peak memory, and image storage.
- GPU time when required, including idle allocation.
- Infrastructure failure rate, retry count, and timeout frequency.
- MCP prefix overhead, actual tool use, and fallback behavior.
- Fully resolved tasks, not only passing test fractions.

Set an attempt limit, total experiment limit, and concurrency limit.
Stop new launches before the global reserve is exhausted.
Account for in-flight requests and teardown.
Report all planned attempts and their costs, including unsuccessful ones.

Use [FeatureBench integration](featurebench-integration.md) to choose the runner-specific controls.
The native FeatureBench `--cost-limit` switch does not cap Claude Code.
Harbor exposes a Claude Code budget parameter, but its behavior still needs a stop-path test.

[^1]: [Anthropic, Introducing Claude Sonnet 5.5](https://www.anthropic.com/claude-sonnet-5-5), published 2026-09-28, checked 2026-09-29.
[^2]: `benchmark/config.py:23-35`, at `9fa37b51fb655294f6718ff4da1a00ba52a68a53`.
[^3]: [AgentCompass SWE-bench Pro Verified guide](https://agent-compass.mintlify.app/en/user_guide/modules/benchmarks/swebench_pro_verified).

_Source: Dated provider pricing; local model aliases; computed budget arithmetic · Updated: 2026-09-29 · Supersedes: Any interpretation of the session's $3–$10 scenarios as measured Sonnet costs._
