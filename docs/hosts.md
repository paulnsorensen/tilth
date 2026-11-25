# Hosts

This page lists the hosts that `tilth install` supports and the files it writes.
The source of truth is `src/install.rs`.

## MCP config

```bash
tilth install <host>
```

The command keeps your other config entries. It adds or replaces one `tilth` server entry.

| Host | Config file |
|---|---|
| `claude-code` | `~/.claude.json` |
| `cursor` | `~/.cursor/mcp.json` |
| `windsurf` | `~/.codeium/windsurf/mcp_config.json` |
| `vscode` | `.vscode/mcp.json` (project) |
| `claude-desktop` | the Claude Desktop config file for your OS |
| `opencode` | `~/.config/opencode/opencode.json` |
| `gemini` | `~/.gemini/settings.json` |
| `codex` | `~/.codex/config.toml` |
| `amp` | `~/.config/amp/settings.json` |
| `droid` | `~/.factory/mcp.json` |
| `antigravity` | `~/.gemini/antigravity/mcp_config.json` |
| `zed` | `~/.config/zed/settings.json` |
| `copilot-cli` | `~/.copilot/mcp-config.json` |
| `augment` | `~/.augment/settings.json` |
| `kiro` | `~/.kiro/settings/mcp.json` |
| `kilo-code` | VS Code global storage of the extension |
| `cline` | VS Code global storage of the extension |
| `roo-code` | VS Code global storage of the extension |
| `trae` | `.trae/mcp.json` (project) |
| `qwen-code` | `~/.qwen/settings.json` |
| `crush` | `~/.config/crush/crush.json` |
| `pi` | `~/.pi/agent/mcp.json` |

The entry runs the absolute path of the `tilth` binary that ran the install, with `--mcp`.
When that binary is inside `node_modules`, the entry is `npx <package> --mcp` instead, where `<package>` is the npm package that holds the binary. From the nightly package, the entry is `npx @paulnsorensen/tilth-nightly --mcp`. The `--hook` entry uses the same `npx` form. See [Install](../README.md#install).

The installer accepts `--edit` for compatibility and ignores it. The server always serves `tilth_write`.

## Shell guard hook

```bash
tilth install <host> --hook
```

`--hook` writes the MCP config, then adds one entry for `tilth hook` to the host's hook file. A second run replaces the tilth entry and keeps other hooks.

| Host | Hook file | Event shape (`--host`) |
|---|---|---|
| `claude-code` | `~/.claude/settings.json` | `claude` |
| `codex` | `~/.codex/hooks.json` | `claude` |
| `droid` | `~/.factory/hooks.json` if it exists, else the `hooks` key of `~/.factory/settings.json` | `claude` |
| `crush` | `~/.config/crush/crush.json` | `claude` |
| `augment` | `~/.augment/settings.json` | `claude` |
| `cursor` | `~/.cursor/hooks.json` | `cursor` |
| `windsurf` | `~/.codeium/windsurf/hooks.json` | `windsurf` |

Other hosts refuse `--hook`.

### Command form

- `claude-code` gets an exec form when the entry runs the binary by its absolute path: the path goes in `command` and `hook --host <family>` goes in `args`. No shell reads the path. An install from npm runs `npx <package>`, so it gets the shell form.
- Every other host gets a shell string. A path with characters other than letters, digits, and `_./:-` is put in single quotes.
- On Windows, `windsurf` runs the string through PowerShell.
- In shell form, the install refuses a binary path that contains `'`, `"`, `$`, a backtick, `%`, or a newline.

### When the guard cannot run

- `claude-code` gets `onFailure: "block"`. A missing binary then blocks every Bash call.
- The other hosts get no fail-closed field. A missing binary then disables the guard. Keep the binary path stable.

### Codex trust

Codex does not run a new hook until you trust it. After the install, run `/hooks` in Codex and approve the tilth entry.

### Remove the guard

`tilth install` has no uninstall step.
To remove the guard, delete the tilth entry from the hook file. Then restart the host.
In the shell form, the entry's `command` contains `hook --host <family>`; a `--hint` value can follow it.
In the exec form (`claude-code` with an absolute binary path), `command` is the binary path, and `args` holds `hook --host <family>`.

### `tilth hook` flags

```bash
tilth hook --host <claude|copilot|cursor|windsurf> --hint <tilth|native> [--allow-capture]
```

- `--host` sets the event shape. The default is `claude`. The install host names `claude-code`, `codex`, `droid`, `crush`, and `augment` also select `claude`.
- `--hint` sets which tool names the deny message cites. The default is `tilth`.
- The guard exits 2 to deny.

### Output capture

The hook blocks file capture by default. `--allow-capture` lets a tool save its own output, for example `just check > check.log 2>&1`.
It also permits a pipe from a tool to `tee`, curl downloads with `-o` or `-O`, and `tail -f`.
Readers can use permitted literal paths under `/proc` or `/sys`, or `/dev/null`. The hook still blocks recursive repository searches.
Output to a stream device is permitted. Authored text, including text from a generator or a here-document, does not qualify for file capture.
This option routes tool use. It does not provide a security sandbox. See [the policy and its limits](../.hallouminate/wiki/adr/tilth-hook-strict-evolve.md).

Run `tilth hook --help` before enabling capture. An older binary rejects the unknown option with exit 2, which the host reads as a denial.
Add `--allow-capture` to the existing tilth hook's `args` array or to its shell command. Do not pass it to `tilth install`.
Reinstalling the hook preserves the existing opt-in. A fresh install stays strict.
