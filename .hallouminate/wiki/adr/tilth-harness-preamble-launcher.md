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
- F-7 (2026-11-22, issue #473): `tilth preamble --append <FILE>` gives the bundled text, one empty line, and the bytes of `<FILE>` unchanged. With `--path`, tilth writes the joined text to its own cache file and prints the path. The option takes one file and does not repeat. A file that tilth cannot read gives exit 1, a message on stderr, no output, and no cache write. Line 1 of the result is always `# tilth agent preamble`; `tests/launch_preamble.rs` pins that line as the marker that callers test. `tilth claude` and `tilth codex` do not use the option.

## Why F-7 fits F-3, F-5, and F-6

- F-3 holds. The bundled text stays whole and comes first. tilth reads `<FILE>` on each call and keeps no config file and no record of the path. The only copy of the caller text is the joined cache file.
- F-5 holds. The default output has no personal policy. A caller that appends policy does so with an explicit flag on each call.
- F-6 holds. The option serves the same harnesses as `--path`: the ones that take one prompt file, such as Codex (`model_instructions_file`) and OpenCode (`instructions`).
- tilth owns the join because the join needs the cache rules that tilth already has: the private directory, the content check before reuse, the content hash in the name, and the 30-day removal. The dotfiles shell join (`dots_effective_preamble`, dotfiles PR #1649) had to copy all four rules.

## Rejected directions

- SessionStart hook `additionalContext`: it arrives as a system reminder in the conversation. It cannot replace the prompt and is capped at 10,000 chars.
- Output style (including a plugin with `force-for-plugin`): it also arrives as a reminder attachment, at lower authority (dotfiles `harnesses/claude.md:30-36`, verified on Claude Code 2.1.302).
- `tilth install codex --preamble` writing a root `model_instructions_file`: it competes with the dotfiles key and needs an uninstall path.
- User-editable `~/.config/tilth/preamble.md`: rejected in favor of one versioned bundled source. F-7 does not reopen this: it adds no persistent user file and cannot change the bundled text.

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
- A joined text (F-7) uses the same cache code path as the plain text (`ensure_preamble_file` in `src/launch.rs`). Its file name is `preamble-<hash>.md` with the hash of the joined bytes, so the stale-file removal covers it and a changed `<FILE>` gives a new name. The name does not hold the path of `<FILE>`.
- On Unix the launcher stages preamble files with owner-only permissions. It replaces unsafe cache entries instead of updating their targets.
- Pruning excludes the active append source, even when the caller supplies a relative path or a symlink to that source.
- The join adds exactly one `\n` after the bundled text. That gives one empty line only because `prompts/preamble.md` ends with one newline; `tests/launch_preamble.rs` pins that too. The result is byte-identical to the former dotfiles shell join.
- A tilth build without `--append` exits 2 (clap usage error). A build with it exits 1 for an unreadable file. A caller can therefore tell an old build from a bad file. The experimental version identifies the build, not its supported options. Probe for `--append` instead of comparing versions.
- Codex's session record (`~/.codex/sessions/.../rollout-*.jsonl`, `session_meta.payload.base_instructions`) shows which base instructions a launch used. Use it to verify delivery; a model asked to "quote your instructions" may quote `~/.codex/AGENTS.md` instead.
- On Windows the launcher needs a native `claude.exe`/`codex.exe`; Rust `Command` does not run npm `.cmd` shims.
