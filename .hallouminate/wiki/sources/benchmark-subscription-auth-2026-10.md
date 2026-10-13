# Running FeatureBench and SWE-bench Multilingual on subscription auth, one eval per attempt

The tilth benchmark-overhaul session's research note, accessed 2026-10-03, supports running external benchmark tasks on a Claude and Codex subscription with one agent run and one test-only grading pass per attempt.
Canonical source: [Running FeatureBench and SWE-bench Multilingual on subscription auth, one eval per attempt](https://github.com/paulnsorensen/tilth/blob/9e84659b2d27715a0d995b30a760213cefd01096/.cheese/archive/benchmark-gepa-overhaul/corpus/research/benchmark-subscription-harness-auth/benchmark-subscription-harness-auth.md)
The link points at the PR #312 head commit `9e84659`, which stays reachable through `refs/pull/312/head`. PR #312 is the all-in-one provenance PR; it closes unmerged, and the work lands as the stacked PRs #313-#319. The session archive leaves the tree after that PR, so the primary sources below are the durable evidence. The raw notes are `raw/auth-terms.md` and `raw/harness-options.md` in the same archive directory. The SWE-bench Multilingual notes are on [SWE-bench Multilingual candidates](swe-bench-multilingual-candidates-2026-10.md).

## Supported claims

This research note backs the benchmark decisions recorded in [ADR: benchmark GEPA overhaul](../adr/benchmark-gepa-overhaul.md).

- `claude setup-token` prints a one-year OAuth token for "CI pipelines, scripts". You set it as `CLAUDE_CODE_OAUTH_TOKEN`. It can only make model requests, and locally configured MCP servers still work. Source: code.claude.com/docs/en/authentication.
- Credential precedence has seven steps: 1 cloud provider, 2 `ANTHROPIC_AUTH_TOKEN`, 3 `ANTHROPIC_API_KEY`, 4 `apiKeyHelper`, 5 `CLAUDE_CODE_OAUTH_TOKEN`, 6 profiles, 7 subscription OAuth from `/login`. For step 3 the doc says: "In non-interactive mode (-p), the key is always used when present". So under `-p` a stray API key always wins, and billing silently moves to the API. Source: https://code.claude.com/docs/en/authentication.
- `--bare` never reads OAuth credentials, so a subscription harness must not pass it. Source: code.claude.com/docs/en/headless.
- `total_cost_usd` is a client-side estimate from a bundled price table. Under subscription it is a notional API-equivalent price, not a bill. Source: code.claude.com/docs/en/agent-sdk/cost-tracking.
- The binding constraint under subscription is the 5-hour session window plus the weekly cap. Both are shared across models. Source: code.claude.com/docs/en/errors; support.claude.com/en/articles/11049741.
- Codex ChatGPT login rotates `auth.json` about every 8 days. Its docs say "Do not share the same file across concurrent jobs". Source: developers.openai.com/codex/auth/ci-cd-auth.
- Native `fb infer` requires `OPENAI_API_KEY` for Codex and has no MCP hook. Harbor's FeatureBench verifier shares the agent container and leaves the gold patch readable. Details and pins are in the harness section below.
- Upstream graders (`fb eval`, SWE-bench `run_evaluation`) apply each patch in a fresh container. That costs about one minute of tests, not a second agent run.

## Stream and error signals

- A second process can hold the OAuth refresh lock. Under `-p` the failure reads "Failed to refresh OAuth token: another Claude Code process is refreshing it or exited mid-refresh..." with code `server_error`, not `authentication_failed`. The doc says "Another Claude Code process on the same machine held the shared refresh lock". Source: https://code.claude.com/docs/en/errors.
- `rate_limit_event` carries `rate_limit_info.status` with the values `"allowed"`, `"allowed_warning"`, and `"rejected"`. It can carry `errorCode: "credits_required"`. The doc says "When errorCode is credits_required, the rejection is from a claude.ai subscription whose included usage is exhausted" (v2.1.181+). Source: https://code.claude.com/docs/en/agent-sdk/typescript.

## Codex and OpenAI

- Codex CI auth needs one credential file per concurrent stream: "Use one auth.json per runner or per serialized workflow stream. Do not share the same file across concurrent jobs or multiple machines." Source: https://developers.openai.com/codex/auth/ci-cd-auth.
- `codex exec --json` reports token usage on `turn.completed` but has no cost field and no rate-limit field. The `Usage` struct holds only token counts. Sources: https://developers.openai.com/codex/noninteractive; https://raw.githubusercontent.com/openai/codex/main/codex-rs/exec/src/exec_events.rs.

## Harness options (pinned)

The note read FeatureBench at `8d4e347` (v0.2.3) and Harbor `main` at `a2fafbc`, both on 2026-10-03.

- The FeatureBench inference proxy allows only `api.anthropic.com:443` for Claude and `api.openai.com:443` for Codex. It blocks every other destination with "Blocked non-model network destination" (`featurebench/infer/network.py:107-117,315-319`). So a ChatGPT-login Codex cannot reach its auth endpoints. Also, `codex.py:313-332` overwrites `auth.json` with an API key.
- The `fb infer` Claude command is a fixed `claude --verbose -p ... --allowedTools ...` line with no `--mcp-config` and no extra-arguments hook (`featurebench/infer/agents/claude_code.py:103-110`). The `mcp__*` tools are not in its allowlist.
- Harbor uses OAuth only on opt-in: "Opt in via CLAUDE_FORCE_OAUTH=<truthy>; default keeps ANTHROPIC_API_KEY." (`src/harbor/agents/installed/claude_code.py:1770-1812`).
- The Harbor FeatureBench adapter sets no verifier environment, so the verifier runs in the agent container. Its Dockerfile keeps the gold patch: "COPY setup_patch.diff /tmp/setup_patch.diff". The agent can read `/tmp/setup_patch.diff` (`adapters/featurebench/template/environment/Dockerfile`, `adapters/featurebench/template/task.toml`).
- Harbor's FeatureBench parity is "Task-level reward agreement: 53/60 (88%)" over 30 lite tasks with 2 trials each (`adapters/featurebench/README.md:152-158`).
## Limitations and open questions

The note's own confidence is "speculating" for quota and stream behavior under OAuth.

- Untested: whether `claude -p` stream-json under OAuth carries `total_cost_usd` and `rate_limit_event`, and the exit code on a usage limit. The follow-up `bench-oauth-fixture` settles this.
- Unresolved: Anthropic's Consumer Terms bar automated access except by API key "or where we otherwise explicitly permit it" (https://www.anthropic.com/legal/consumer-terms, effective 2025-10-08). The same terms say: "You may not share your Account login information, Anthropic API key, or Account credentials with anyone else. You also may not make your Account available to anyone else." Anthropic's own docs describe `setup-token` for scripts. Only Anthropic can reconcile the two texts, and the exposure falls on the maintainer's account.
- Unresolved, second terms question: the OpenAI Terms of Use bar users from "Automatically or programmatically extract data or Output" (https://openai.com/policies/terms-of-use/). OpenAI's Codex docs describe ChatGPT-auth CI and copying `auth.json` into a container. No text found reconciles the two.
- Forward risk: the headless doc says `--bare` "will become the default for `-p` in a future release". Bare mode never reads OAuth credentials, so that change breaks a subscription harness that does not opt out. Source: https://code.claude.com/docs/en/headless.
- Policy risk: Anthropic announced that "Claude Agent SDK and `claude -p` usage no longer counts toward your Claude plan's usage limits" from 2026-06-15, and then paused it: "For now, nothing has changed". The plan can resume. Source: https://support.claude.com/en/articles/15036540.
- Native execution gives no network isolation parity with FeatureBench's proxy.

_Source: PR #312 session archive (head `9e84659`, `refs/pull/312/head`), research note and raw notes accessed 2026-10-03 · Updated: 2026-10-13_
