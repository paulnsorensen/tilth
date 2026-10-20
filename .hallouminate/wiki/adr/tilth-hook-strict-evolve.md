# ADR: tilth hook and strict-by-default evolve

Status: implemented (2026-10-20) as stacked PRs #359 (hook-core), #360 (hook-install), #361 (bench-strict-hook), #362 (evolve-strict-default). Spec: `~/.local/share/cheese/paulnsorensen-tilth/specs/tilth-hook-strict-evolve.md`.

## Context

A strict benchmark on 2026-10-20 (f4db1e6, sonnet 5.5, 5 reps per arm) compared native and tilth_forced on gin_edit_render_context:

- Cost: tilth_forced ties native (median $0.515 for both, exact Mann-Whitney p = 0.42).
- Speed: tilth_forced is about 14% faster (p = 0.048).
- Tool calls: tilth_forced makes about 65% fewer.

This holds only when Bash cannot read or write files. In the non-strict Oct 18 reference, native bulk-edits with `perl -pi`, `sed -i`, and Python heredocs, and wins about 3x on cost.

The old strict guard, `benchmark/claude_bash_guard.py`, was a Go-only allowlist, and `run_plan` had no strict mode. Evolve therefore could not score candidates under strict conditions.

## Decisions (user-settled)

- F-1: The strict Bash policy is a patched port of the user's dotfiles `tool-reroute` deny-list (`~/Dev/dotfiles/agents/lib/tool-reroute*`). It is released as part of tilth.
  - The dotfiles original fails open, has a 16 KiB small-file exemption, and names only tilth in its deny text.
  - It also lets `perl -pi`, `python -c`, heredoc writes, and most `>` redirects through.
- F-2: The hook is a Rust `tilth hook` subcommand in `src/hook/`. Evolve candidates already edit `src/**` (`benchmark/evolve/materialize.py:15`).
- F-3: The hook denies with a tilth hint. It does not rewrite commands into tilth CLI calls.
- F-4: `tilth install <host> --hook` is opt-in and is meant to cover every harness that has a verified pre-shell hook.
- F-5: The baseline arm runs the seed binary's hook with `--hint native`. Two checks hold every candidate hook to the seed's rules:
  - A frozen corpus, `benchmark/hook_corpus.json`, gates each candidate. It has `must_deny` and `must_allow` rows, checked in both hint modes.
  - After each cell, every Bash call is replayed through the seed hook. A call the seed denies fails the cell as `hook_loosened`.
  - A candidate can tighten rules or reword hints. It cannot allow anything the seed denies.
- F-6: Evolve runs strict by default. `--no-strict-file-tools` turns it off.

## Gotchas found while shaping

- Exit code 2 with the reason on stderr denies on every Claude-compatible host, and also on cursor and windsurf.
  - Antigravity's exit codes are undocumented; it uses a stdout JSON deny.
  - Copilot CLI fails open when a hook times out.
  - Source: `.cheese/research/agent-shell-hook-protocols/agent-shell-hook-protocols.md`.
- Claude-shaped hosts name the shell tool differently: `Bash`, `Execute` (droid), `bash` (crush), `launch-process` (augment).
  - The hook must evaluate any event that carries a command, whatever its `tool_name`, or it fails open.
- The tilth host name in `src/install.rs` is `claude-code`, not `claude`.
- `install.rs` writes only MCP config today (`~/.claude.json`), so hook registration needs a new settings merge.
- Strict cells already form their own baseline slot (`benchmark/baselines.py:33-36`). Turning strict on buys a full new baseline; it does not register as drift.
- The old benchmark guard is unrelated to the repo's own `scripts/tilth-bash-guard`, which CI self-tests.
- Baseline run key: a fingerprint of corpus verdicts would be unsound, because hook behaviour outside the corpus can change without changing it.
  - The baseline key instead uses the git tree hash of `src/hook` plus the SHA-256 of `Cargo.lock` at `hook_sha`.
  - This relies on `src/hook/` importing only `std`, `serde_json`, `tree_sitter`, and `tree_sitter_bash`.
- Copilot CLI's documented `preToolUse` event sends `toolName` and a JSON-string `toolArgs`, not `tool_input.command`, and its exit-2 reason reaches only the user. It is not in install v1.
- Cursor `failClosed: true` counts silent output as a failure, and the hook allows silently, so it would block every command. Cursor's hook is fail-open if the binary is missing.
- Claude Code supports exec form (`command` + `args`, no shell) and `onFailure: "block"` (v2.1.295+). Installs and strict benchmark cells use both for native binaries. Exec form cannot start a Windows `.cmd` shim such as `npx`, so npm installs use shell form.
- A real Claude Code hook denial result starts with `PreToolUse:Bash hook error: [`; a failed command starts with `Exit code N`. The strict audit recognizes a denial only by that prefix.
- Install tests must run with a sandbox HOME. A test that unset HOME made `tilth install` fall back to the passwd home and write the real `~/.claude.json` and `~/.claude/settings.json`.
- Wrapper commands (`env`, `sudo`, `timeout`, `xargs`, ...) leak through ordering variants (`--`, `VAR=value`, positionals) unless one loop consumes all of them; reviews found them one at a time.

## Deferred

- vscode, copilot-cli, antigravity, gemini, qwen-code, and kiro install support waits until each host's docs and event shape are verified.
- opencode, pi, and amp need plugin shims.
- Retiring `scripts/tilth-bash-guard`.
- The wozcode strict arm gets native-tool deny hints it cannot use.
- Policy gaps outside the named rules: top-level `$c f`, `uv run`/`poetry run`/`npx`/`docker run` wrappers, `git blame`, `sed w`, awk `system()`, `busybox`, `find -fprint`.
