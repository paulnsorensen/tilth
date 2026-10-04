# Raw notes: harness options (accessed 2026-10-03)
Source repo clones (read-only, untrusted data): FeatureBench @ 8d4e347 (v0.2.3), harbor main (shallow).

## FeatureBench native fb infer
- featurebench/infer/agents/claude_code.py:201-208 required_vars = {"ANTHROPIC_API_KEY": ..., DISABLE_TELEMETRY, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC...}
- claude_code.py:225-228 "# Add any additional env vars / for key, value in self.env_vars.items(): if key not in required_vars and value: required_vars[key] = value"
  -> any extra key in [infer_config.claude_code] (e.g. CLAUDE_CODE_OAUTH_TOKEN) is exported. ANTHROPIC_API_KEY only exported if non-empty (line 231 `if value:`); not validated as required.
- config.py:162-176 get_agent_env_vars merges [env_vars] + [infer_config.<agent>] (all non-empty keys, stringified).
- claude_code.py:103-110 run cmd: `claude --verbose -p <instr> --allowedTools Bash Edit Write Read Glob Grep LS WebFetch NotebookEdit NotebookRead TodoRead TodoWrite Agent --output-format stream-json | tee /agent-logs/...` -- no --mcp-config, no extra-args hook; mcp__* tools not in allowlist.
- codex.py:194-196 `if not api_key: raise RuntimeError("OPENAI_API_KEY is required for codex agent")`
- codex.py:313-332 writes $HOME/.codex/auth.json = {"OPENAI_API_KEY": key} and config.toml with custom model_provider "featurebench", wire_api="responses" -> overwrites any ChatGPT-login auth.json; forces API-key route.
- network.py (added d4f6efc 2026-09-03 "fix: harden inference against solution leakage"):
  - :85-89 base_url keys claude_code->ANTHROPIC_BASE_URL, codex->OPENAI_BASE_URL
  - :107-117 defaults claude_code->api.anthropic.com:443, codex->api.openai.com:443 (single endpoint)
  - :315-319 `_open_allowed`: endpoint not in allowed -> PermissionError "Blocked non-model network destination" -> 403
  - :409-433 isolate(): writes HTTP(S)_PROXY into /installed-agent/setup-env.sh then disconnects ALL docker networks.
  -> No claude.ai / console.anthropic.com / platform.claude.com, no chatgpt.com / auth.openai.com.
- run_infer.py:882-884 network isolation is unconditional ("Agent network isolation is not initialized" raise); :938-945 isolate after install.
- run_infer.py:873-895 volumes = {cache_dir:/download, proxy socket}; no user-mount config.
- base.py:275-340 pre_run_setup(container, instance, log_file) hook "called AFTER agent installation and BEFORE agent execution" (default no-op) — subclass hook to copy tilth binary/.mcp.json; prepare_run() :252 also.
- runtime.py:121-200 Level-1 init: `rm -rf /testbed/* && cp -r /root/my_repo/* /testbed/ && rm -rf /root/my_repo`, git apply mask.patch, rm -f f2p test, git init.
- runtime.py:398-520 complete_runtime: git add -A; git diff --cached <base>; strips nested .git dirs, removes binary diffs (_remove_binary_diffs :523).
- run_infer.py:~960-1010 infer never runs tests; result.model_patch saved; output.jsonl (output.py:44).

## fb eval
- harness/run_evaluation.py:569 create_container per prediction (fresh), :584 run_instance_level1.
- harness/runtime.py:55-63 "Level 1 workflow: 1. Activate conda 2. Restore project from /root/my_repo/ 3. Apply mask patch, delete F2P 4. env fixes 5. Reinit git 6. Apply agent's patch 7. Delete generated F2P files and restore F2P test 8. Run tests"
- README.md:24 fast split 100 instances, "average evaluation time per instance using gold patches is 57.2 seconds".

## Harbor (harbor-framework/harbor main @ a2fafbc, 2026-10-03)
- src/harbor/agents/installed/claude_code.py:111-126 capabilities mcp_servers=True; MODEL_CONNECTION api_key_envs=("ANTHROPIC_API_KEY","ANTHROPIC_AUTH_TOKEN")
- claude_code.py:1770-1812 `_should_force_oauth`: "Opt in via CLAUDE_FORCE_OAUTH=<truthy>; default keeps ANTHROPIC_API_KEY." ; reads CLAUDE_CODE_OAUTH_TOKEN; error "Run `claude setup-token` to get one"; env passes CLAUDE_CODE_OAUTH_TOKEN.
- claude_code.py:1717-1743 `_build_register_mcp_servers_command` writes {"mcpServers":...} to $CLAUDE_CONFIG_DIR/.claude.json ("User-scoped servers are loaded without a trust dialog"); stdio -> {type, command, args}.
- claude_code.py:98-102 permission_mode default "bypassPermissions"; :1889-1890 IS_SANDBOX=1; :1895 CLAUDE_CONFIG_DIR=<logs>/sessions.
- codex.py:1470-1495 `_resolve_auth_json_path`: "Defaults to None (OPENAI_API_KEY auth). Opt into auth.json auth via: CODEX_AUTH_JSON_PATH=<path> / CODEX_FORCE_AUTH_JSON=<truthy> → use ~/.codex/auth.json"; :1546-1556 uploads auth.json, symlinks into $CODEX_HOME. :470-472 auth.json not supported in ACP mode. :1425-1449 writes [mcp_servers.<name>] into Codex config.toml.
- models/task/config.py:450 `mcp_servers: list["MCPServerConfig"]` in [environment]; :617-640 MCPServerConfig(name, transport sse|stdio|streamable-http, url, command, args).
- models/trial/config.py:150 agent-level mcp_servers; :215 `mounts` "Additional mounts for the agent environment only."; cli/jobs.py:684-688 `--mcp-config` "Claude-style .mcp.json or Harbor MCP config file. Repeatable."
- models/task/config.py:555-600 VerifierEnvironmentMode SHARED|SEPARATE; "When omitted: defaults to 'separate' if a verifier 'environment' is set, otherwise 'shared'." trial.py:885-950 separate verifier gets only uploaded artifacts.
- models/task/config.py:37-42,70 NetworkMode no-network|public|allowlist; default PUBLIC.
- models/trial/paths.py: trial_dir/{config.json, result.json, agent/, verifier/reward.txt|reward.json, artifacts/}; models/job/config.py:403 jobs_dir default Path("jobs").
- adapters/featurebench/template/task.toml: no [verifier] environment -> shared mode (verifier runs in agent container).
- adapters/featurebench/template/environment/Dockerfile: "# Keep the gold patch in image for oracle solve.sh. COPY setup_patch.diff /tmp/setup_patch.diff" ; test_patch.diff copied to /tmp; lv2 backup /tmp/.hb_solution.tar.gz -> remain readable by agent.
- template/tests/test.sh:13-44 guardrail via git status; :48 restores F2P tests via git checkout; :69 runs `{test_cmd} {fail_to_pass_files}`; :78-89 P2P only if F2P passes; writes /logs/verifier/reward.txt (binary).
- adapters/featurebench/README.md:152-158 parity 30 lite tasks x2 trials, "Task-level reward agreement: 53/60 (88%)"; parity_experiment.json: codex@0.106.0 gpt-5-mini, original 13.3% vs harbor 15.0%.

## Prior art (web, accessed 2026-10-03; untrusted)
- https://github.com/jimmc414/claudecode_gemini_and_codex_swebench — README: "No API key required for Max or Pro subscribers"; agent (claude/codex/gemini CLI) runs on HOST against checkout (`python code_swe_agent.py --instance_id ...`), predictions JSONL -> `python swe_bench.py eval --file predictions_*.jsonl` (SWE-bench docker harness).
- https://github.com/nedcut/gm-bench/pull/148 — CLAUDE_CODE_OAUTH_TOKEN handed via stdin of throwaway docker run into 0600 file in home volume; iptables egress firewall; MCP config inline; `--permission-mode dontAsk --allowedTools mcp__gm-bench,Bash,Read,Edit,...`.
- https://github.com/sunj-labs/bassclef-cli/issues/184 — "harness: accept CLAUDE_CODE_OAUTH_TOKEN so Docker harness bills against Claude subscription" (title only).
- Web summary: ANTHROPIC_API_KEY, if set, takes precedence over CLAUDE_CODE_OAUTH_TOKEN (Harbor code comment agrees: claude_code.py:1785 "default keeps the key (the CLI prefers it)").
- gh search code/repos blocked in this session (403 repo-scoped).
