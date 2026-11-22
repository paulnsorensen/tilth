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

## Decision: tool runners and stdin REPLs (2026-11-22, user-approved guard change)

A review of PR #459 found two gaps. `mise exec -- cat f` and `gdb -batch -ex "shell cat f"` passed. Codex `write_stdin` can type into a running process, so a program that reads its commands from stdin is an unguarded shell.

- Tool runners unwrap to the command they run, and the normal rules check that command. `RUNNERS` in `src/hook/rules.rs` lists them: `mise exec|x`, `uv run`, `uvx`, `npx`, `npm exec|x`, `poetry run`, `bundle exec`, `pnpm exec|dlx`, `pnpx`, `yarn exec|dlx`, `bunx`, `bun x`, `direnv exec DIR`, `nix develop|shell -c`, `nix-shell --run|--command`, `asdf exec`, `pixi run`, `conda|mamba|micromamba run`.
  - The command is the words after `--`, or the first operand after the subcommand. `mise exec` takes tool specs before `--`, so only the words after `--` are its command.
  - A shell-string option (`mise exec -c`, `npx -c`, `nix-shell --run`) and pnpm shell mode reparse their text, like `bash -c`.
  - `uv run -m MODULE` uses the `python -m` module rules, and `uv run -` denies as a stdin script.
  - `nix develop`, `nix shell`, and `nix-shell` without a command start an interactive shell and deny as `shell stdin`.
  - Unknown runner options count as flags. A false deny costs benchmark accuracy. The cost of this choice: the value of an unlisted valued option reads as the command.
- gdb and lldb deny without `-batch` (rule `stdin repl`). An `-ex`/`-o` value that starts with `shell`, `!`, or `platform shell` reparses the shell part. A value that starts with `pipe`, `|`, `python`/`py`/`pi`, `guile`, or `script` denies (rule `debugger script`).
- `REPLS` is one table of programs that read commands from stdin when no operand or option names a program: `irb`, `pry`, `lua`, `luajit`, `sqlite3` (needs DB and SQL), `make`/`gmake` with `-f -`, `R` without `-f`/`-e`/`CMD`, `tclsh`, `guile`, `ghci` without `-e`, `jshell`. They deny as `stdin repl`.
- The rules only close the stdin vector. Inline code that does not read stdin (`lua -e`, `R -e`, `ghci -e`, `guile -c`) stays allowed, like `Rscript -e`.
- Each new rule has a `must_deny` row in `benchmark/hook_corpus.json`, because `ac1_every_rule_entry_has_a_must_deny_row` requires one.

### Review fixes (PR #466, 2026-11-22, user-approved guard change)

An independent review of PR #466 found nine bypasses. The fixes:

- A short option with `=` drops the `=` from its value (`npx -c='cmd'`, `mise exec -c='cmd'`, `gdb -x=-`), because clap, nopt, and gdb do the same.
- An unlisted `--long` runner option gives the next word two readings: the command, or the option's value. The rules check both, and a deny in either one denies. Only the first reading denies a missing command, so `npx --yes prettier` stays allowed. Before the subcommand, the word after an unlisted option can be its value, so `uv --new-opt v run cat f` finds `run`. `UV_VALUED` follows `uv run --help` and `uvx --help` (uv 0.13.9). `NPM_VALUED` lists common npm config keys, and `-C` is `--prefix`.
- Options before the runner subcommand count: `pnpm -c exec CMD` and `npm -c CMD exec` reparse CMD.
- `npx`, `npm exec`, and `npm x` with no command and no `-c` start a shell that reads stdin (`stdin_shell`), so they deny as `shell stdin`.
- gdb and lldb: the rules check the program and its arguments like any other command. gdb uses the words after `--args`, or else the first operand (or `-e`/`-exec`). lldb uses `-f`, every operand, and the words after `--`. `run ARGS`, `start ARGS`, `set args`, `set inferior-tty`, `tty`, `process launch ARGS`, and the lldb `target.run-args` and `target.input-path` settings deny, because gdb passes `run` arguments through a shell. Pass arguments with `--args` or after `--` instead.
- Debugger meta commands deny: `eval`, `alias`, `with`, `make`, `define`, `define-prefix`, `document`, `source`, `if`, `while`, `commands`, `compile`, `interpreter-exec`, lldb `command`, and any `$_shell(...)` call. This is a deny list, not an allow list: the deny list is simpler and keeps every plain inspection command (`bt`, `info`, `frame`, `print`) allowed. `thread apply`, `frame apply`, `taas`, `faas`, and `tfaas` check the command they run, so `thread apply all bt` stays allowed.
- sqlite3 SQL operands and `-cmd` values deny (rule `sqlite3 io`) when they call `readfile`, `writefile`, `fsdir`, `zipfile`, `load_extension`, or `edit(`, or when a line starts with a file or shell dot-command (`.shell`, `.system`, `.read`, `.import`, `.output`, `.once`, `.load`, `.log`, `.save`, `.backup`, `.restore`, `.open`, `.excel`, `.clone`, `.archive`, `.iotrace`, `.trace`, and their abbreviations). `-A` denies too. The `stdin repl` hint now says to pass plain SQL.
- make `--eval`/`-E` (and `--ev` abbreviations) deny outright (rule `make --eval`), because a recipe in the text (`all: ; cat f`) also runs. A command-line variable with `!=`, or with `$(shell`, `$(file`, `$(eval`, or `$(guile` in it, denies too.

Open gaps after this change: `python -i`, `node -i`, `perl -d`, `php -a`; `docker run`; `cabal repl`, `stack ghci`, `ghc --interactive`; debugger expression calls into a live inferior (`call system()`, `print func()`, lldb `expr`); gdb `list` and lldb `source list` print source lines; `MAKEFLAGS=--eval=...` in the environment; a make variable that the Makefile itself passes to a shell (`make test PYTEST='cat f #'`); `npm --script-shell` with a non-shell program; SQL text hidden in a variable (`sqlite3 db "$Q"`).

## Deferred

- vscode, copilot-cli, antigravity, gemini, qwen-code, and kiro install support waits until each host's docs and event shape are verified.
- opencode, pi, and amp need plugin shims.
- Retiring `scripts/tilth-bash-guard`.
- The wozcode strict arm gets native-tool deny hints it cannot use.
- Policy gaps outside the named rules: top-level `$c f`, `docker run` (tool runners closed 2026-11-22, see above), `git blame`, `busybox`, `find -fprint`, `parallel` and other shell-string runners, `awk` programs that build file names or commands at run time, `sed` scripts read with `-f`.
