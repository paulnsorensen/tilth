# Benchmark harness gotchas (Sonnet 5 investigation, 2026-08-08)

Four independent ways the benchmark harness silently produced invalid or
misleading numbers, found while investigating PR #196
(`jahala/tilth#196`). Full forensics in `.cheese/notes/tilth-pr196-sonnet5-audit.md`
and `.cheese/notes/tilth-sonnet5-cost-attribution.md`.

## `--safe-mode` strips ALL MCP servers, including `--mcp-config` ones

The 2026-08-07 sonnet5 run (`benchmark_20260807_075044_sonnet5.jsonl`) compared
three arms that were supposed to differ by which tilth MCP server was attached.
`--safe-mode` disabled every MCP server — including ones passed via
`--mcp-config` — so all three arms ran native-tools-only. Init messages showed
`tools=[5 native], mcp_servers=[]` in every cell; first-turn prompt size was
identical (~8,124 tokens) across arms. Every accuracy/cost delta in that run
was sampling noise (fork-upstream +$0.21 total, bootstrap 95% CI
`[-0.66, +1.13]`). The PR #196 failure-audit and benchmark comments posted
against this run (comments `5223809583`, `5216075559`) rest on the invalid
comparison.

Fix: `--setting-sources ""` for harness isolation instead of `--safe-mode`.

## Guards added on PR #168 (commit `d4d41b5`)

- Per-cell `available_tools`, `mcp_servers`, and `model_usage` recorded in
  every result row.
- `McpUnavailableError` — the harness now aborts fast if a pinned arm's MCP
  server fails to attach, instead of silently degrading to native tools.
- `check_experiment` validates tilth-availability per pinned cell (fails the
  run if a tilth arm didn't actually get tilth attached).
- Per-arm and per-task tool-usage reporting in `analyze.py`.

Verified by a 6-cell haiku smoke run
(`benchmark_20260808_021626_haiku.jsonl`): tilth attached and was called in
both pinned arms.

## `pricing.yaml` drift vs Claude Code's native billing

Sonnet 5 was initially priced in `benchmark/pricing.yaml` using the wrong
rate family, understating computed cost by **-33.7%** vs Claude Code's native
billing. Corrected to the introductory Sonnet-5 rates ($2/M input, $10/M
output, $2.50/M 5m cache writes, $4/M 1h cache writes, $0.20/M cache reads,
`as_of: 2026-08-07`) — residual dropped to -0.50% (remainder attributed to a
haiku sidecar call, decomposable via the new per-row `model_usage` field).
Reports now show a `Δnative` residual line so a future pricing drift is
visible instead of silent.

## Stale installed binary served pre-#151 instructions for weeks

`~/.local/bin/tilth` was a stale build that predated PR #151 (the 2KB
instruction-cap shortening) — it served an 8,688-char instructions blob for
weeks after #151 landed. It was mistaken for current HEAD behavior during an
early measurement pass (the "24,917-char fork surface" reading). Root cause:
version is pinned at `0.8.4` across the whole fork (see CLAUDE.md fork law),
so `tilth --version` cannot distinguish a stale binary from current HEAD.

**Check byte sizes (or rebuild from source), not `--version`, when a fork
binary's prompt surface is in question.**

## Codex headless isolation

Use `--runner codex` to restrict scheduling to Codex models.
The default model alias is `gpt5`; the default arms are `baseline,tilth`.
The runner rejects other providers and `tilth_forced` before model calls.[^codex-runner]

Codex runs ignore user configuration and rules.
The runner disables discovered user, project, admin, and system skills through explicit `skills.config` entries.
It also disables skill search, plugins, hooks, apps, and multi-agent support.
The `skip_host_skill_discovery` flag alone does not exclude personal skills in CLI 0.154.0.[^codex-isolation]
The selected tilth MCP server uses `required=true` to fail on initialization errors.
The tilth arm receives explicit tilth-first developer instructions; native tools remain available.
A cell without a successful tilth call, or with a detected skill-access attempt, invalidates the run.[^codex-isolation]

Codex JSONL does not supply the Claude init inventory used by this harness.
An empty `available_tools` field does not establish that tilth is absent.
Use recorded MCP calls or an explicit MCP probe to confirm tool access.[^codex-parser]
Read batching does not prove write access: the first corrected smoke rejects `tilth_write` under the default approval policy.
After explicit user approval, the runner sets `mcp_servers.tilth.tools.tilth_write.approval_mode="approve"` only for its invocation.
A live edit smoke then repairs three files in one successful MCP write and passes the task tests.[^codex-write-probe]
This does not change global settings or confine MCP absolute paths; benchmark tasks target disposable copies.
Codex cost fields contain token-based estimates, not native billing or a spending limit.[^codex-parser]

[^codex-runner]: benchmark/run.py:675-693; benchmark/config.py:23-45
[^codex-isolation]: benchmark/run.py:163-228,369-416,573-576; .context/codex-disabled-skills-probe.jsonl; https://learn.chatgpt.com/docs/build-skills
[^codex-parser]: benchmark/parse.py:193-356; benchmark/README.md:5-27
[^codex-write-probe]: benchmark/results/streams/20260928_130342/02_gin_edit_render_runtime_tilth_luna56_rep0.jsonl (approval denied); benchmark/results/streams/20260928_131147/01_gin_edit_render_runtime_tilth_luna56_rep0.jsonl (three-file write succeeds); benchmark/run.py:408-415; https://learn.chatgpt.com/docs/extend/mcp

_Source: Codex-only benchmark setup and live probes · Updated: 2026-09-28_

## Edit-only batching selection

The Luna 5.6 diagnostic uses three repetitions of two cross-file edit tasks.
`gin_edit_render_cascade` tests shared-helper consistency; `gin_edit_render_runtime` tests three independent renderer repairs.
Both use test-based grading, not answer keywords.[^batch-selection]
This Gin-only selection tests a narrow batching hypothesis, not general coding performance.
Check actual tool arguments for batched file access; an available MCP server does not prove batching.

Exclude `express_diff_multi_mutation` until its supposed harmless rename is repaired.
It renames the `app` declaration without renaming later uses, so preserving that change preserves a bug.[^express-mutation]
Mutation preflight detects broken behavior but does not prove that a task specification is valid.

[^batch-selection]: benchmark/tasks/gin_render_cascade_tasks.py:34-83; benchmark/tasks/gin_render_runtime_tasks.py:34-76; benchmark/README.md:32-46
[^express-mutation]: benchmark/tasks/express_diff_tasks.py:35-39; Express fixture commit 1140301f6a0ed5a05bc1ef38d48294f75a49580c, lib/response.js:161

## Larger forward-edit benchmark

The two repair tasks each need only three restored source lines.
The shared-helper task can revert two files instead of migrating all callers.[^small-repair]
Use a required new API to prevent this shortcut when testing larger edits.

`gin_edit_render_context` changes 20 reference files across 90 edit sites, with 165 added and 72 removed lines.
It migrates renderers, context dispatch, SSE compatibility, and original test callers.[^large-reference]
The grader restores pinned regression assertions and injects held-out tests after inference.
It accepts new local Go helper/test files so valid implementations are not tied to reference file placement.
It also tests the candidate suite, preventing unmigrated callers from passing through restored tests alone.

Grade full renderer and binding suites plus root Context, Middleware, and held-out tests under default and `nomsgpack` builds.
Compile all packages to detect missed cross-package callers.
Do not include unrelated network readiness tests in per-cell correctness.
The pinned `TestUnixSocket` waits only five milliseconds before dialing and fails on a correct reference.[^socket-race]
Preflight rejects unchanged code, incomplete migration, omitted cancellation, premature header changes, and lost context propagation.
It also checks that weakened candidate assertions do not rescue broken source.[^large-reference]

Do not restrict migration grading to root/render: `binding/json_test.go` also calls `PureJSON.Render`.
An initial larger-task attempt exposes this missed caller and is invalidated before comparison.[^binding-gap]
Forward an explicit temporary `GOCACHE` to both arms to avoid unwritable global cache failures.
The runner allowlist supports this key without widening sandbox permissions.[^binding-gap]

[^binding-gap]: Gin fixture d7776de7d444935ea4385999711bd6331a98fecb, binding/json_test.go:59; .context/luna56-render-context-invalid-attempt.md; benchmark/run.py:101-114

[^small-repair]: benchmark/tasks/gin_render_cascade_tasks.py:34-60; benchmark/tasks/gin_render_runtime_tasks.py:34-53
[^large-reference]: benchmark/tasks/gin_render_context_fixtures/evidence.md; benchmark/tasks/gin_render_context_tasks.py:110-161; benchmark/tasks/gin_render_context_fixtures/preflight.py
[^socket-race]: Gin fixture d7776de7d444935ea4385999711bd6331a98fecb, gin_integration_test.go:248-275; .cheese/cook/luna56-render-context.md

## Sonnet 5 and WOZCODE three-way runs

Use explicit `--reasoning-effort high`, `--max-budget-usd`, and `--arm-order-seed` for the Claude comparison.
`--wozcode-plugin-dir` enables the opt-in WOZCODE arm and records its version and Git commit.[^three-way-runner]
The comparison disables Agent and Task tools. It does not test WOZCODE's normal Haiku exploration delegation.

Claude's `--strict-mcp-config` excludes the plugin's automatic MCP registration, even when `--plugin-dir` loads the plugin.
Configure the server explicitly as `plugin_woz_code` so WOZCODE hooks recognize its tool names.
Check both the tool inventory and a successful edit before scored inference.[^woz-probes]

An explicit `CLAUDE_CONFIG_DIR` activates fresh per-cell configuration.
Copy only WOZCODE authentication, not settings, saved sessions, or memory.
Preserve refreshed authentication between sequential cells.
A new Claude configuration directory does not inherit the existing macOS OAuth login.
Supply OAuth through the child environment without logging or saving the token.
Without an explicit seed, legacy Claude runs retain their original authentication path.[^three-way-auth]

A Claude result must report successful completion and `is_error: false` before grading.
Budget exits and missing result events are failures, not successful cells.
The task, trusted grader, and tilth binary remain unchanged for the September three-way run.[^three-way-runner]

Use `--strict-file-tools` to match the two MCP arms: Bash plus their own MCP tools, without native file tools.
The native control retains Read, Edit, Write, Grep, Glob, and Bash.
All arms use the same fail-closed Go test/build/vet/formatting Bash hook.
An available MCP server alone does not establish exclusive use: the stopped hybrid tilth cell uses 47 native Edit calls.[^strict-tools]
The hook is a tool-use control, not a security sandbox.

Do not assume a copied interactive OAuth access token lasts for the whole schedule.
One strict attempt passes its first cell, then records 14 authentication failures after the token is revoked.
Keep authentication failures separate from task failures. Stop further calls when authentication fails.
Prefer a dedicated `claude setup-token` credential for unattended runs.
The retry verifies at least four hours of credential validity and aborts on authentication errors.[^strict-auth-failure]

[^strict-tools]: benchmark/run.py, _strict_bash_settings and _audit_strict_claude; benchmark/claude_bash_guard.py; .context/sonnet5-hybrid-stopped-results.md; .context/sonnet5-strict-plan.md
[^strict-auth-failure]: .context/sonnet5-strict-auth-failure.md; https://code.claude.com/docs/en/authentication#generate-a-long-lived-token

[^three-way-runner]: benchmark/run.py, wozcode_mode, claude_mcp_config, _run_single_in_repo, main; benchmark/README.md, Sonnet 5 three-way invocation; .context/sonnet5-three-way-plan.md
[^woz-probes]: .context/sonnet5-woz-smoke.jsonl (tools absent); .context/sonnet5-woz-smoke2.jsonl (search permission denied); .context/sonnet5-woz-edit-smoke.jsonl (edit succeeds)
[^three-way-auth]: benchmark/run.py, _cell_claude_config and build_runner_env; benchmark/tests/test_run_hardening.py; .context/sonnet5-native-smoke.jsonl (isolated auth absent), .context/sonnet5-native-smoke2.jsonl (native login succeeds)

## Related

- `.cheese/notes/tilth-pr196-sonnet5-audit.md`
- `.cheese/notes/tilth-sonnet5-cost-attribution.md`
- PR #168 (paulnsorensen/tilth): harness + reporting fixes, commits `d4d41b5`, `f8a92c9`
