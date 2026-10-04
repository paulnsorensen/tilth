# Running FeatureBench and SWE-bench Multilingual on subscription auth, one eval per attempt

Accessed 2026-10-03. Raw notes: `raw/auth-terms.md`, `raw/harness-options.md`, `raw/swe-multilingual.md`.

## Synthesis

Our own runner can run FeatureBench and SWE-bench Multilingual on subscription login for both Claude Code and Codex, with one agent run and one test-only grading pass per attempt. Native `fb infer` hard-requires `OPENAI_API_KEY` for Codex. Harbor supports both logins but grades inside the agent's container and leaves the gold patch in `/tmp`. Both upstream graders (`fb eval`, `swebench` `run_evaluation`) take a predictions JSONL and apply each patch in a fresh container, which costs about a minute of tests, not a second agent run. Native `fb infer` never grades either, so "agent container plus grading container" is the upstream design, not a duplicate eval. For Claude, a `claude setup-token` token (one year, static, set as `CLAUDE_CODE_OAUTH_TOKEN`) avoids refresh-token rotation in containers; `ANTHROPIC_API_KEY` must be absent and `--bare` must not be used. Codex ChatGPT login rotates `auth.json` and its docs forbid sharing that file across concurrent jobs. Under subscription, `total_cost_usd` is a notional price-table estimate, and the binding constraint is the 5-hour and weekly quota. Anthropic's Consumer Terms bar automated access except by API key "or where we otherwise explicitly permit it", while Anthropic's own docs describe `setup-token` for scripts; the two texts are not reconciled.

## Claim evidence

| # | Claim | Source | Confidence |
| --- | --- | --- | --- |
| 1 | `run.py` already passes `CLAUDE_CODE_OAUTH_TOKEN`/`CLAUDE_CONFIG_DIR` and `CODEX_HOME`, and avoids `--bare` because it refuses OAuth | `benchmark/run.py:196`, `:207-208`, `:613-619` | certain |
| 2 | `claude setup-token` prints a one-year OAuth token for "CI pipelines, scripts", never saved; it can only make model requests | code.claude.com/docs/en/authentication | certain |
| 3 | `ANTHROPIC_API_KEY` beats the OAuth token under `-p`; a stray key silently switches billing | code.claude.com/docs/en/authentication | certain |
| 4 | `/login` credentials refresh and rotate; parallel copies of `.credentials.json` likely break, `setup-token` does not rotate | code.claude.com/docs/en/errors (inferred) | speculating |
| 5 | Codex `auth.json` refreshes about every 8 days; "Do not share the same file across concurrent jobs"; docs recommend an API key for automation | developers.openai.com/codex/auth/ci-cd-auth | certain |
| 6 | Anthropic Consumer Terms bar access "through automated or non-human means" except via API key or explicit permission | anthropic.com/legal/consumer-terms (eff. 2025-10-08) | certain (text), don't know (application) |
| 7 | Pro/Max limits "assume ordinary, individual usage"; `-p` draws from subscription limits; a plan to move it off them is paused | code.claude.com/docs/en/legal-and-compliance; support.claude.com/en/articles/15036540 | certain |
| 8 | Claude limits are a 5-hour window plus a weekly cap; `rate_limit_event` carries status, `resetsAt`, utilization | support.claude.com/en/articles/11049741; Agent SDK types | certain (types), speculating (`-p` stream emits it) |
| 9 | `total_cost_usd` is a client-side estimate from a bundled price table | Agent SDK cost-tracking docs | speculating (under OAuth, untested) |
| 10 | `codex exec --json` reports token usage but no cost or quota fields | codex-rs/exec/src/exec_events.rs | certain (fields) |
| 11 | `fb infer` does not grade; `fb eval` grades each patch in a fresh container; gold eval averages 57.2 s | FeatureBench@8d4e347 run_evaluation.py:569; README:24 | certain |
| 12 | Native FB Claude agent passes extra env keys (OAuth token works without code change); Codex agent requires `OPENAI_API_KEY` and overwrites `auth.json` | FB claude_code.py:225-238; codex.py:194-196, 313-332 | certain |
| 13 | Native FB has no MCP hook; the run command and `--allowedTools` are fixed; the proxy allows only api.anthropic.com/api.openai.com | FB claude_code.py:103-110; network.py:85-135 | certain |
| 14 | Harbor supports `CLAUDE_FORCE_OAUTH` + token, Codex `auth.json` upload, and MCP via task.toml or `--mcp-config` | harbor claude_code.py:1770-1812; codex.py:1470-1556; models/task/config.py:450 | certain |
| 15 | Harbor's FB verifier shares the agent container, its image keeps `/tmp/setup_patch.diff` (gold) readable, and network defaults to public | harbor adapters/featurebench template | certain |
| 16 | Agent-on-host works for SWE-bench on Max with no API key; it loses the image's test environment unless tests run via `docker exec` into the post-setup container | jimmc414/claudecode_gemini_and_codex_swebench (README); FB runtime.py | medium |
| 17 | SWE-bench Multilingual grades `{instance_id, model_patch}` in a fresh container per instance; test command and image come from dataset rows | swebench harness run_evaluation.py, utils.py:make_test_spec @02e7a74 | certain |
| 18 | Multilingual images are prebuilt on Docker Hub with digests; Go 262 MB–1.1 GB, Rust 568 MB–2.5 GB; most precompile test binaries | Docker Hub tags API; swe-bench-multilingual-tasks Dockerfiles | certain |
| 19 | Fast candidates: Go gin-4003, gin-2121, caddy-5870 (smoke), gin-3741, prometheus-14861 (discriminating); Rust tokio-4898, axum-691, bat-2393 (smoke), bat-2650, bat-2201 (discriminating) | SWE-bench/experiments per_instance_details (14 mini-SWE-agent runs) | medium |

## Open questions (alternatives, not decisions)

- Agent placement: inside the pinned image (claude/codex binary plus tilth mounted, `setup-token` env) versus on the host with a `docker exec` test tool into the post-setup container.
- Codex concurrency: run Codex cells serially with one writable `auth.json`, or use an API key for the Codex arm only.
- Whether the Consumer Terms' "explicitly permit" clause covers `setup-token` benchmark runs; only Anthropic can answer.
- Network isolation parity with FB's 2026-09-03 proxy when the agent runs under our runner.
- Quota handling: pause and resume on `rate_limit_event` or a limit message, versus marking the cell failed.

## Gaps

- Untested: `total_cost_usd` and `rate_limit_event` under OAuth in `claude -p` stream-json; exit code on a usage limit.
- Untested: whether OAuth inference reaches any host besides api.anthropic.com.
- No measured eval times for Multilingual instances; gold patches not validated on the 2026-08 `latest` images.
- No primary current Opus/Sonnet weekly hour figures.

## Confidence

speculating — the harness and grader claims are certain from source, but quota behavior and stream fields under OAuth are untested and the terms question is unresolved.

## agent_resolution

| Work | Resolved type | Isolation | Fallback used |
| --- | --- | --- | --- |
| Auth and terms fetch | general-purpose | read-only, fresh context | yes (no `researcher` type) |
| Harness options fetch | general-purpose | read-only, fresh context | yes |
| SWE-bench Multilingual fetch | general-purpose | read-only, fresh context | yes |
