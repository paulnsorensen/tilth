# ADR: tilth harness preamble launcher

Status: implemented (2026-10-29) on branch `t3/confirm-preamble-editor` (PR #417). Review follow-ups on `t3/preamble-launcher-lows`. `tilth preamble` on `t3/preamble-command`. Spec: `~/.local/share/cheese/mold/specs/tilth-harness-preamble-launcher.md`. Research: `~/.local/share/cheese/briesearch/research/harness-preamble-injection-claude-codex/`.

tilth takes over the preamble role of the dotfiles `ap` tool for Claude Code and Codex. It adds two launcher subcommands, `tilth claude` and `tilth codex`. Each one execs the harness with a bundled preamble that replaces the built-in system prompt. A third subcommand, `tilth preamble`, prints the preamble for other harnesses.

## Decisions (user-settled)

- F-1: Claude gets the preamble through a `tilth claude [ARGS]...` launcher that execs `claude --system-prompt-file <F>`. Only a launch flag gives a true system prompt.
- F-2: The preamble replaces the built-in prompt on both harnesses. This matches `ap` and avoids duplicate tool guidance.
- F-3: The preamble text is bundled in the binary (`prompts/preamble.md`, `include_str!`). Users cannot edit it, and no user config file exists.
- F-4: Codex gets the preamble through a `tilth codex [ARGS]...` launcher that passes `-c model_instructions_file="<F>"`. tilth never writes `~/.codex/config.toml`.
- F-5: The preamble covers tilth tool routing and the harness basics that a replaced prompt drops. Personal wiki, delegation, and agent-selection policy moves to the global AGENTS.md and CLAUDE.md in dotfiles.
- F-6: Other harnesses get the preamble through `tilth preamble`, which prints the bundled text. `tilth preamble --path` writes the launcher's cache file and prints its path, for harnesses that take a prompt file. tilth adds no launcher per extra harness.

## Rejected directions

- SessionStart hook `additionalContext`: it arrives as a system reminder in the conversation. It cannot replace the prompt and is capped at 10,000 chars.
- Output style (including a plugin with `force-for-plugin`): it also arrives as a reminder attachment, at lower authority (dotfiles `harnesses/claude.md:30-36`, verified on Claude Code 2.1.302).
- `tilth install codex --preamble` writing a root `model_instructions_file`: it competes with the dotfiles key and needs an uninstall path.
- User-editable `~/.config/tilth/preamble.md`: rejected in favor of one versioned bundled source.

## Gotchas

- Claude Code has no `settings.json` key that sets or appends to the system prompt. A plain `claude` launch never sees the preamble.
- The Codex `model_instructions_file` key must be a TOML root key, written before any table (dotfiles bug #262). The launcher avoids this because it uses `-c`.
- A replaced prompt drops vendor safety and tool guidance, so `prompts/preamble.md` must restate it, including the defensive-security and no-guessed-URL lines.
- Management subcommands (`claude mcp …`) through the launcher are a non-goal. The prepended flag can break them.
- The benchmark does not use `ap`. It builds its own argv (`benchmark/run.py:1324-1490`). A preamble arm is a follow-up.
- Clap drops a leading `--` in a `trailing_var_arg` positional. `src/main.rs` `launch_from_argv` therefore dispatches before clap parses argv when argv[1] is `claude` or `codex`, and passes argv[2..] unchanged. The clap `Claude`/`Codex` variants are unit variants for `--help` only. Clap reaches them only when another argument comes first, and that is a usage error (exit 2). `args_conflicts_with_subcommands` does not work here: clap then reads `claude` as the positional query and runs a search.
- `tilth -- claude` still searches for the word `claude`; a bare `tilth claude` starts the launcher. The same holds for `tilth preamble`.
- `tilth preamble` is an ordinary clap subcommand. It writes the text with one `write_all` and a flush, and treats a closed pipe as success.
- The caller-override scan (`caller_overrides`) stops at the first `--`. Text after it, such as `tilth claude -- --system-prompt`, is a prompt, not a flag.
- On Unix the cache dir `<cache>/tilth` is created with mode 0700. The launcher refuses it when it is a symlink, another user owns it, or all users can write it (mode bit 0o002). With a shared `XDG_CACHE_HOME`, another user could otherwise swap the preamble before the harness reads it.
- Group write on the cache dir is allowed. With umask 002 (the Debian and Ubuntu default), `~/.cache/tilth` is 0775 and its group is the user's private group. Other tilth caches (`~/.cache/tilth/deps`) share the dir. A group-write refusal broke a real `~/.cache/tilth` during development.
- Each preamble revision has its own cache file. A launch that finds its file refreshes the file mtime. A launch that writes a new file removes other `preamble-*.md` files older than 30 days, so a running session of another tilth version keeps its file.
- Codex's session record (`~/.codex/sessions/.../rollout-*.jsonl`, `session_meta.payload.base_instructions`) shows which base instructions a launch used. Use it to verify delivery; a model asked to "quote your instructions" may quote `~/.codex/AGENTS.md` instead.
- On Windows the launcher needs a native `claude.exe`/`codex.exe`; Rust `Command` does not run npm `.cmd` shims.
