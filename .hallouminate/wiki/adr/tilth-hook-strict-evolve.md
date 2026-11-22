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

Open gaps after this change: `python -i`, `node -i`, `perl -d`, `php -a`; `docker run`; `cabal repl`, `stack ghci`, `ghc --interactive`; debugger expression calls into a live inferior (`call system()`, `print func()`, lldb `expr`); gdb `list` prints source lines (lldb `source list` denies, because `source` is on the debugger deny list); `MAKEFLAGS=--eval=...` in the environment; a make variable that the Makefile itself passes to a shell (`make test PYTEST='cat f #'`); `npm --script-shell` with a non-shell program; SQL text hidden in a variable (`sqlite3 db "$Q"`).

### Review fixes, round 2 (PR #466, 2026-11-22, user-approved guard change)

A second independent review of PR #466 found eleven issues. Real gdb 17.1, sqlite3 3.54.1, bun 1.3.14, uv 0.13.9, mise, and pnpm 10.13.1 confirmed each bypass and each fix. The fixes:

- gdb reads options with `getopt_long_only`. A unique prefix of a long name works (`-eval`, `--init-e`, `-ar` for `--args`), and `NAME=VALUE` works. `GDB` in `src/hook/rules.rs` lists the whole gdb 17 `long_options` table, so the rules resolve a prefix as gdb does. An unknown option, an ambiguous prefix (`-bat`), or a flag with a value denies as `unresolved`, because gdb exits on it. `-early-init-eval-command`/`-eiex` and `-early-init-command`/`-eix` count as command options. `-tty FILE` gives the program its input, so it denies as `debugger script`.
- lldb reads exact names: a long name takes one or two dashes (`-one-line`), and a one-letter name takes one dash. lldb ignores other options. `--repl` and `-r` start a REPL that reads stdin and deny as `stdin repl`. `lldb -batch` now counts as batch mode; before, the rules read it as `-b` with the value `atch`.
- `thread apply` skips `$var` thread IDs (`thread apply $_thread shell cat f`).
- sqlite3 SQL denies when an identifier names `edit`, `readfile`, `writefile`, `fsdir`, `zipfile`, `load_extension`, `sqlar_compress`, `sqlar_uncompress`, `sha1_query`, `sha3_query`, `sqlite_dbpage`, or `writable_schema`. A small SQL tokenizer finds the identifiers. It skips string literals and comments, and it reads `"quoted"`, `[bracketed]`, and backtick names. Thus `edit/**/(` denies and `where action = 'edit'` stays allowed.
  - The rule does not try to follow strings that SQL joins at run time (`'ed'||'it'`). Instead it denies every way that the CLI can run SQL text built at run time: `sha1_query`, `sha3_query`, the `.selftest` dot-command, and schema rewrites through `writable_schema`, `sqlite_dbpage`, or `.dbconfig`. A name in a string then never runs.
  - In text with a dot-command, any whole word counts, because a dot-command quotes its arguments like a shell and `.parameter set` runs a value as SQL.
  - The dot-commands `.www`, `.session`, `.selftest`, and `.dbconfig` deny too.
  - The cost: a column or parameter named `edit` denies.
- sqlite3 reads `--name` as `-name`, so the rules strip one dash before they check options. `--Ac` and `--interactive` now deny. `-init FILE` denies unless FILE is `/dev/null`, because sqlite3 runs FILE and a parse error prints its text.
- make: a `MAKE_CALLS` function in an environment assignment denies (`CFLAGS='$(shell cat f)' make`), because make reads its environment as variables.
- Runners:
  - `bun run`, `uv tool run`, `pipenv run`, `pdm run`, `hatch run`, `rye run`, `devbox run`, and `pixi exec` unwrap to the command they run.
  - `pixi run`, `devbox run`, and `hatch run` run the command words as shell source, so the rules rejoin the words and reparse them. hatch also skips `+NAME=VALUES` matrix words and strips an `ENV:` prefix from the command (`hatch run dev:cat f`).
  - `mise en`, and `shell` for poetry, pipenv, hatch, rye, devbox, and pixi, start a shell that reads stdin and deny as `shell stdin`.
  - A runner command of `-` reads a script from stdin and denies.
- pnpm reads options with nopt. A prefix of `--shell-mode` (`--shell-m`, `--shell`), `--shell-mode=VALUE` other than `false`, a `true` or `false` word after the flag, and a one-letter cluster with `c` (`-rc`) all turn on shell mode. pnpm also runs a first operand that is not a pnpm command as a script, or else as a command (`pnpm cat f`), so the rules check that operand as a command. `PNPM_BUILTINS` lists the pnpm commands, and `recursive`, `m`, and `multi` are group words before `exec`.
- The ADR no longer lists lldb `source list` as an open gap.

Open gaps added by this round: `sqlite3 -append FILE` writes a database into an existing file; `ATTACH` and `VACUUM INTO` create database files; the rejoined words of `pixi run`, `devbox run`, and `hatch run` lose their quotes, so a quoted argument with a shell operator can deny.

### Review fixes, round 3 (PR #466, 2026-11-22, user-approved guard change)

A third review of PR #466 found nine bypasses. Real sqlite3 3.54.1, bun, yarn, npm, and hatch (through `uvx`) confirmed each bypass for those tools. gdb, lldb, and nix are not installed on the test machine. Their fixes follow the gdb and lldb source and the nix manual. The fixes:

- sqlite3 `-lookaside SIZE N` and `-pagecache SIZE N` take two values. `-nonce`, `-cmdline-edit`, `-screenwidth`, `-threadsafe`, and `-sorterref` take one value. The rules skip these values, so `sqlite3 -lookaside 64 8 app.db` denies as `stdin repl`. The SQL check skips them too, so it never reads the database operand as SQL.
- `bun repl` starts a REPL that reads stdin, so it denies as `shell stdin`.
- `yarn node ARGS` runs `node ARGS`. The runner field `program_subs` unwraps it, and the interpreter rules check `node`.
- `npm explore PKG [--] CMD` runs CMD as shell source. Without CMD, it starts a shell that reads stdin. The runner field `shell_subs` reparses CMD, and a missing CMD denies as `shell stdin`. `scan` now skips the operands that a runner still needs after `--`, so `npm explore -- PKG CMD` denies too.
- gdb `edit` runs `$EDITOR` through `/bin/sh`, so it denies as `debugger script`.
- gdb aliases of denied commands deny: `gr` and `guile-repl` start a Guile REPL, and `expression` is an alias of `compile`. Only gdb denies `expression`, because lldb `expression` prints values. The alias list comes from `gdb/python/python.c`, `gdb/guile/guile.c`, `gdb/compile/compile.c`, and `gdb/cli/cli-cmds.c`.
- lldb `platform file`, `platform get-file`, and `platform put-file` read and write files, so they deny as `debugger script`. The rules check `platform process launch ARGS` as `process launch ARGS`. The `debugger script` hint now says that the command can read or write a file.
- hatch: `env` is a group word, so `hatch env run` unwraps like `hatch run`. `-i`, `-x`, and `-f` take values for `env run`. The hatch root has no short options with these letters.
- `nix run` follows the rule below.

The `nix run` rule:

- `nix run INSTALLABLE ARGS` runs the main program of INSTALLABLE with ARGS. The hook cannot evaluate Nix, so it takes the program name from the last attribute of INSTALLABLE (`nixpkgs#hello` runs `hello`).
- The rules remove a `_VERSION` suffix (`nodejs_22`). `NIX_PROGRAMS` maps common packages whose program has another name (`ripgrep` runs `rg`, `bashInteractive` runs `bash`).
- The rules check the command line with `nix run INSTALLABLE` replaced by the program name. Thus `nix run nixpkgs#bash` denies as `shell stdin`, and `nix run nixpkgs#bat -- f` denies as `bat`.
- ARGS are the operands after INSTALLABLE and all words after `--`. With `-f FILE` or `--expr`, INSTALLABLE is an attribute path.
- Without an attribute (`nix run`, `nix run .`, a store path, a variable), the hook cannot name the program, so the command denies as `unresolved`.
- A word after an unlisted `--long` option has two readings, as in the other runners: INSTALLABLE, or the value of the option.
- This is the simplest rule that allows `nix run nixpkgs#hello` and denies a shell. Its limit: the rules do not check a package whose program name is not its attribute and not in `NIX_PROGRAMS`. A flake app can also run any program under any name.

Open gaps added by this round:

- From the review (not fixed): `make -p -f FILE` prints the make database, which holds the text of FILE. `uv run --env-file FILE printenv` prints the variables in FILE. gdb `search` and `forward-search` print matching source lines. `bundle console` starts a Ruby REPL that reads stdin.
- Found during the round-3 work (not fixed): `deno` and `deno repl` start a REPL that reads stdin. `nix repl` reads stdin, and `nix eval --expr 'builtins.readFile ./f'` prints a file. lldb `repl` and `expression --repl` start a REPL that reads stdin.

## Deferred

- vscode, copilot-cli, antigravity, gemini, qwen-code, and kiro install support waits until each host's docs and event shape are verified.
- opencode, pi, and amp need plugin shims.
- Retiring `scripts/tilth-bash-guard`.
- The wozcode strict arm gets native-tool deny hints it cannot use.
- Policy gaps outside the named rules: top-level `$c f`, `docker run` (tool runners closed 2026-11-22, see above), `git blame`, `busybox`, `find -fprint`, `parallel` and other shell-string runners, `awk` programs that build file names or commands at run time, `sed` scripts read with `-f`.
