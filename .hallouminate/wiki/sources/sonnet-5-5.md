# Introducing Claude Sonnet 5.5

Introducing Claude Sonnet 5.5 is Anthropic's model announcement, published 2026-09-28 and verified 2026-09-29.
The announcement supports the Claude Sonnet 5.5 price scenario, not a measured benchmark cost.
Canonical source: [Introducing Claude Sonnet 5.5](https://www.anthropic.com/claude-sonnet-5-5).

## Claude Sonnet 5.5 pricing evidence

Claude Sonnet 5.5 listed prices are $2 per million input tokens and $10 per million output tokens.
Cache reads cost $0.20 per million tokens.
Include cache-write and other billable categories when estimating a run.
These prices do not determine how many tokens a difficult coding task consumes.

## Project relevance

[Benchmark cost planning](../benchmark-cost-planning.md) uses these rates only as a dated model-identity scenario.
The local `sonnet` and `sonnet5` aliases do not select Sonnet 5.5 at the inspected revision.
No Sonnet high benchmark execution supports the session's per-attempt budget assumptions.

_Source: [Anthropic announcement](https://www.anthropic.com/claude-sonnet-5-5) · Updated: 2026-09-29_
