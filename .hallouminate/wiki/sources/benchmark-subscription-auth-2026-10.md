# Running FeatureBench and SWE-bench Multilingual on subscription auth, one eval per attempt

The tilth benchmark-overhaul session's research note, accessed 2026-10-03, supports running external benchmark tasks on a Claude and Codex subscription with one agent run and one test-only grading pass per attempt.
Canonical source: [Running FeatureBench and SWE-bench Multilingual on subscription auth, one eval per attempt](https://github.com/paulnsorensen/tilth/blob/9e84659b2d27715a0d995b30a760213cefd01096/.cheese/archive/benchmark-gepa-overhaul/corpus/research/benchmark-subscription-harness-auth/benchmark-subscription-harness-auth.md)
The link points at the PR #312 head commit. The session archive leaves the tree after that PR, so the primary sources below are the durable evidence.

## Supported claims

This research note backs the benchmark decisions recorded in [ADR: benchmark GEPA overhaul](../adr/benchmark-gepa-overhaul.md).

- `claude setup-token` prints a one-year OAuth token for "CI pipelines, scripts". You set it as `CLAUDE_CODE_OAUTH_TOKEN`. It can only make model requests, and locally configured MCP servers still work. Source: code.claude.com/docs/en/authentication.
- Under `-p`, `ANTHROPIC_AUTH_TOKEN` and then `ANTHROPIC_API_KEY` take precedence over `CLAUDE_CODE_OAUTH_TOKEN`. A stray key silently switches billing to the API. Source: code.claude.com/docs/en/authentication.
- `--bare` never reads OAuth credentials, so a subscription harness must not pass it. Source: code.claude.com/docs/en/headless.
- `total_cost_usd` is a client-side estimate from a bundled price table. Under subscription it is a notional API-equivalent price, not a bill. Source: code.claude.com/docs/en/agent-sdk/cost-tracking.
- The binding constraint under subscription is the 5-hour session window plus the weekly cap. Both are shared across models. Source: code.claude.com/docs/en/errors; support.claude.com/en/articles/11049741.
- Codex ChatGPT login rotates `auth.json` about every 8 days. Its docs say "Do not share the same file across concurrent jobs". Source: developers.openai.com/codex/auth/ci-cd-auth.
- Native `fb infer` requires `OPENAI_API_KEY` for Codex and has no MCP hook. Harbor's FeatureBench verifier shares the agent container and leaves the gold patch readable. Source: FeatureBench and Harbor source at the commits the note cites.
- Upstream graders (`fb eval`, SWE-bench `run_evaluation`) apply each patch in a fresh container. That costs about one minute of tests, not a second agent run.

## Limitations and open questions

The note's own confidence is "speculating" for quota and stream behavior under OAuth.

- Untested: whether `claude -p` stream-json under OAuth carries `total_cost_usd` and `rate_limit_event`, and the exit code on a usage limit. The follow-up `bench-oauth-fixture` settles this.
- Unresolved: Anthropic's Consumer Terms bar automated access except by API key "or where we otherwise explicitly permit it" (anthropic.com/legal/consumer-terms, effective 2025-10-08). Anthropic's own docs describe `setup-token` for scripts. Only Anthropic can reconcile the two texts, and the exposure falls on the maintainer's account.
- Native execution gives no network isolation parity with FeatureBench's proxy.

_Source: PR #312 session archive, research note accessed 2026-10-03 · Updated: 2026-10-08_
