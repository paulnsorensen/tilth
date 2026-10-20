use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{json, Value};

// Supported MCP hosts and their config locations.
//
// Paths verified from official docs (2025):
//   claude-code:    ~/.claude.json                            (user scope)
//   cursor:         ~/.cursor/mcp.json                        (global)
//   windsurf:       ~/.codeium/windsurf/mcp_config.json       (global)
//   vscode:         .vscode/mcp.json                          (project scope)
//   claude-desktop: ~/Library/Application Support/Claude/...  (global)
//   opencode:       ~/.config/opencode/opencode.json          (user scope, local entry)
//   gemini:         ~/.gemini/settings.json                   (user scope)
//   codex:          ~/.codex/config.toml                      (user scope, TOML)
//   amp:            ~/.config/amp/settings.json                (user scope)
//   droid:          ~/.factory/mcp.json                        (user scope)
//   antigravity:    ~/.gemini/antigravity/mcp_config.json      (user scope)
//   zed:            ~/.config/zed/settings.json                (user scope)
//   copilot-cli:    ~/.copilot/mcp-config.json                 (user scope)
//   augment:        ~/.augment/settings.json                   (user scope)
//   kiro:           ~/.kiro/settings/mcp.json                  (user scope)
//   kilo-code:      <globalStorage>/kilocode.kilo-code/...     (user scope)
//   cline:          <globalStorage>/saoudrizwan.claude-dev/... (user scope)
//   roo-code:       <globalStorage>/rooveterinaryinc.roo-cline/... (user scope)
//   trae:           .trae/mcp.json                             (project scope)
//   qwen-code:      ~/.qwen/settings.json                     (user scope)
//   crush:          ~/.config/crush/crush.json                 (user scope)
//   pi:             ~/.pi/agent/mcp.json                       (user scope)
const SUPPORTED_HOSTS: &[&str] = &[
    "claude-code",
    "cursor",
    "windsurf",
    "vscode",
    "claude-desktop",
    "opencode",
    "gemini",
    "codex",
    "amp",
    "droid",
    "antigravity",
    "zed",
    "copilot-cli",
    "augment",
    "kiro",
    "kilo-code",
    "cline",
    "roo-code",
    "trae",
    "qwen-code",
    "crush",
    "pi",
];

/// The tilth server entry as JSON. Format depends on the host's [`ConfigFormat`] variant.
fn tilth_server_entry(format: &ConfigFormat) -> Value {
    let (command, args) = tilth_command_and_args();
    match format {
        ConfigFormat::Json { .. } => json!({
            "command": command,
            "args": args
        }),
        ConfigFormat::JsonLocal { .. } => {
            let mut command_arr = vec![command];
            command_arr.extend(args);
            json!({
                "type": "local",
                "command": command_arr
            })
        }
        ConfigFormat::Toml => unreachable!("tilth_server_entry called for TOML host"),
    }
}

/// Returns a migration warning when a pre-#144 Claude Code hook install is still
/// on disk. tilth no longer ships the `PreToolUse` cwd-injection hook; leaving it
/// wired means an omitted `cwd` gets silently filled with the session root instead
/// of tilth's teaching refusal.
fn stale_hook_warning(script_exists: bool) -> Option<&'static str> {
    script_exists.then_some(
        "tilth: found a stale Claude Code cwd-injection hook. Delete ~/.claude/tilth/inject-cwd.js and remove the mcp__tilth__.* PreToolUse entry from ~/.claude/settings.json — tilth no longer ships this hook, and leaving it wired means an omitted cwd is silently filled with the session root instead of tilth's teaching refusal.",
    )
}

/// Write MCP config for the given host, preserving existing config.
/// The MCP server always serves the edit surface, so the entry carries no
/// mode flag.
pub fn run(host: &str) -> Result<(), String> {
    let host_info = resolve_host(host)?;

    if let Some(parent) = host_info.path.parent() {
        fs::create_dir_all(parent)
            .map_err(|e| format!("failed to create {}: {e}", parent.display()))?;
    }

    match host_info.format {
        ConfigFormat::Json { .. } | ConfigFormat::JsonLocal { .. } => {
            write_json_config(&host_info)?;
        }
        ConfigFormat::Toml => write_toml_config(&host_info)?,
    }

    if host == "claude-code" {
        if let Some(msg) =
            stale_hook_warning(home_dir()?.join(".claude/tilth/inject-cwd.js").exists())
        {
            eprintln!("{msg}");
        }
    }

    eprintln!("✓ tilth added to {}", host_info.path.display());

    if let Some(note) = host_info.note {
        eprintln!("  {note}");
    }

    Ok(())
}

/// Hosts whose pre-tool shell hook `install --hook` can register.
const HOOK_HOSTS: &[&str] = &[
    "claude-code",
    "codex",
    "droid",
    "crush",
    "augment",
    "cursor",
    "windsurf",
];

/// Where and in what shape one host keeps its pre-tool shell hook.
struct HookTarget {
    path: PathBuf,
    /// Value for `tilth hook --host`.
    family: &'static str,
    /// Event key holding the entry array.
    event: &'static str,
    /// The event map sits under a top-level `hooks` key.
    wrap: bool,
    /// The command sits in an inner `hooks: [{type, command}]` array.
    nested: bool,
    matcher: Option<&'static str>,
    /// The file carries a top-level `version: 1`.
    version: bool,
    /// Host field that makes a failed guard block the action, when its docs name one.
    fail_closed: Option<(&'static str, Value)>,
    /// The host documents an exec form (`command` + `args`, no shell).
    exec_form: bool,
    /// The host runs shell-form commands through PowerShell on this platform.
    powershell: bool,
    note: Option<&'static str>,
}

fn hook_target(host: &str, home: &Path) -> Result<HookTarget, String> {
    let claude_like = |path: PathBuf, matcher: &'static str| HookTarget {
        path,
        family: "claude",
        event: "PreToolUse",
        wrap: true,
        nested: true,
        matcher: Some(matcher),
        version: false,
        fail_closed: None,
        exec_form: false,
        powershell: false,
        note: None,
    };
    match host {
        "claude-code" => Ok(HookTarget {
            fail_closed: Some(("onFailure", json!("block"))),
            // raw/claude.md: `args` present means `command` is spawned directly, no shell.
            exec_form: true,
            ..claude_like(home.join(".claude/settings.json"), "Bash")
        }),
        "codex" => Ok(HookTarget {
            note: Some(
                "Codex ignores a new hook until you trust it: run /hooks in Codex and approve the tilth entry.",
            ),
            ..claude_like(home.join(".codex/hooks.json"), "Bash")
        }),
        "droid" => {
            // Droid reads settings.json hooks only while hooks.json is absent.
            let standalone = home.join(".factory/hooks.json");
            if standalone.exists() {
                Ok(HookTarget {
                    wrap: false,
                    ..claude_like(standalone, "Execute")
                })
            } else {
                Ok(claude_like(home.join(".factory/settings.json"), "Execute"))
            }
        }
        "augment" => Ok(claude_like(
            home.join(".augment/settings.json"),
            "launch-process",
        )),
        "crush" => Ok(HookTarget {
            nested: false,
            ..claude_like(home.join(".config/crush/crush.json"), "^bash$")
        }),
        "cursor" => Ok(HookTarget {
            path: home.join(".cursor/hooks.json"),
            family: "cursor",
            event: "beforeShellExecution",
            wrap: true,
            nested: false,
            matcher: None,
            version: true,
            fail_closed: None,
            exec_form: false,
            powershell: false,
            note: None,
        }),
        "windsurf" => Ok(HookTarget {
            path: home.join(".codeium/windsurf/hooks.json"),
            family: "windsurf",
            event: "pre_run_command",
            wrap: true,
            nested: false,
            matcher: None,
            version: false,
            fail_closed: None,
            exec_form: false,
            // raw/windsurf.md: commands run through PowerShell on Windows.
            powershell: cfg!(windows),
            note: None,
        }),
        other => Err(format!(
            "--hook is not supported for `{other}`; supported hosts: {}",
            HOOK_HOSTS.join(", ")
        )),
    }
}

/// Quote `word` for a shell hook command. Plain paths stay bare; others get single quotes,
/// which POSIX sh, Git Bash and PowerShell all treat literally. Characters that a quoted
/// word could still expand or break on are refused.
fn shell_quote(word: &str) -> Result<String, String> {
    if let Some(bad) = word
        .chars()
        .find(|c| matches!(c, '\'' | '"' | '$' | '`' | '%' | '\n' | '\r'))
    {
        return Err(format!(
            "cannot register a hook for a binary path containing {bad:?}: {word}"
        ));
    }
    let plain = |c: char| c.is_ascii_alphanumeric() || "_./:-".contains(c);
    if !word.is_empty() && word.chars().all(plain) {
        Ok(word.to_string())
    } else {
        Ok(format!("'{word}'"))
    }
}

/// How one host runs the guard: exec form (`command` + `args`) or a shell-form string.
struct HookInvocation {
    /// Value of the handler's `command` key.
    command: String,
    /// Value of the handler's `args` key; present only in exec form.
    args: Option<Vec<String>>,
    /// `<binary> hook --host ` for this binary, unquoted, to recognise its stale entries.
    own_prefix: String,
}

impl HookInvocation {
    /// The command line as one string, the way a stale-entry check reads it.
    #[cfg(test)]
    fn line(&self) -> String {
        let mut line = self.command.clone();
        for arg in self.args.iter().flatten() {
            line.push(' ');
            line.push_str(arg);
        }
        line
    }
}

/// Build the guard invocation. Exec form keeps the binary path verbatim. Shell form uses
/// forward slashes and the host shell's quoting; `powershell` adds the `&` call operator
/// that PowerShell needs before a quoted path.
fn build_hook_invocation(
    exec_form: bool,
    powershell: bool,
    family: &str,
    binary: &str,
    extra_args: &[String],
) -> Result<HookInvocation, String> {
    let tail = ["hook", "--host", family];
    let own_prefix = std::iter::once(binary)
        .chain(extra_args.iter().map(String::as_str))
        .chain(["hook", "--host", ""])
        .collect::<Vec<_>>()
        .join(" ");
    if exec_form {
        let args = extra_args
            .iter()
            .map(String::as_str)
            .chain(tail)
            .map(String::from)
            .collect();
        return Ok(HookInvocation {
            command: binary.to_string(),
            args: Some(args),
            own_prefix,
        });
    }
    let path = shell_quote(&binary.replace('\\', "/"))?;
    let mut command = if powershell && path.starts_with('\'') {
        format!("& {path}")
    } else {
        path
    };
    for word in extra_args.iter().map(String::as_str).chain(tail) {
        command.push(' ');
        command.push_str(word);
    }
    Ok(HookInvocation {
        command,
        args: None,
        own_prefix,
    })
}

/// Whether `command` is an absolute path as the given platform reads it.
fn is_absolute_command(command: &str, windows: bool) -> bool {
    if command.starts_with('/') {
        return true;
    }
    let b = command.as_bytes();
    windows
        && (command.starts_with("\\\\")
            || (b.len() > 2
                && b[0].is_ascii_alphabetic()
                && b[1] == b':'
                && matches!(b[2], b'/' | b'\\')))
}

/// Exec form starts the binary directly, with no shell. That works only for an absolute
/// path to a native binary. `npx` and `node` are launchers, and a `.cmd`, `.bat` or `.ps1`
/// shim cannot start without a shell on Windows, so those need shell form.
fn exec_form_applies(host_exec: bool, command: &str, _args: &[String], windows: bool) -> bool {
    if !host_exec || !is_absolute_command(command, windows) {
        return false;
    }
    let name = command.rsplit(['/', '\\']).next().unwrap_or(command);
    let lower = name.to_ascii_lowercase();
    let stem = lower.rsplit_once('.').map_or(lower.as_str(), |(s, _)| s);
    let shim = [".cmd", ".bat", ".ps1"].iter().any(|e| lower.ends_with(e));
    !(shim || matches!(stem, "npx" | "node"))
}

fn hook_invocation(target: &HookTarget) -> Result<HookInvocation, String> {
    let (command, mut args) = tilth_command_and_args();
    args.retain(|a| a != "--mcp");
    build_hook_invocation(
        exec_form_applies(target.exec_form, &command, &args, cfg!(windows)),
        target.powershell,
        target.family,
        &command,
        &args,
    )
}

/// Drop quotes and the PowerShell call operator, and turn `\` into `/`, so entries written
/// in any past quoting style compare equal.
fn canonical_command(command: &str) -> String {
    let command = command.trim_start();
    let command = command.strip_prefix("& ").unwrap_or(command);
    command
        .chars()
        .filter(|c| !matches!(c, '"' | '\''))
        .map(|c| if c == '\\' { '/' } else { c })
        .collect()
}

/// True when `command` runs this binary (`own_prefix`) or any binary named `tilth` as
/// `<binary> hook --host <family>`.
fn is_tilth_hook_command(command: &str, own_prefix: &str) -> bool {
    let command = canonical_command(command);
    if command.starts_with(&canonical_command(own_prefix)) {
        return true;
    }
    let Some((head, _)) = command.split_once(" hook --host ") else {
        return false;
    };
    let name = head.rsplit(['/', ' ']).next().unwrap_or("");
    name == "tilth" || name == "tilth.exe"
}

/// A handler's `command`, followed by its `args` when it uses exec form.
fn handler_line(handler: &Value) -> Option<String> {
    let mut line = handler.get("command")?.as_str()?.to_string();
    for arg in handler
        .get("args")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
    {
        line.push(' ');
        line.push_str(arg);
    }
    Some(line)
}

fn is_tilth_hook_entry(entry: &Value, own_prefix: &str) -> bool {
    let inner = entry
        .get("hooks")
        .and_then(Value::as_array)
        .into_iter()
        .flatten();
    std::iter::once(entry)
        .chain(inner)
        .filter_map(handler_line)
        .any(|c| is_tilth_hook_command(&c, own_prefix))
}

fn hook_entry(target: &HookTarget, invocation: &HookInvocation) -> Value {
    let mut entry = serde_json::Map::new();
    if let Some(matcher) = target.matcher {
        entry.insert("matcher".into(), json!(matcher));
    }
    let mut handler = serde_json::Map::new();
    if target.nested {
        handler.insert("type".into(), json!("command"));
    }
    handler.insert("command".into(), json!(invocation.command));
    if let Some(args) = &invocation.args {
        handler.insert("args".into(), json!(args));
    }
    if let Some((key, value)) = &target.fail_closed {
        handler.insert((*key).into(), value.clone());
    }
    if target.nested {
        entry.insert("hooks".into(), json!([Value::Object(handler)]));
    } else {
        entry.extend(handler);
    }
    Value::Object(entry)
}

/// Matcher-group shapes: replace only tilth handlers inside groups. A tilth handler in a
/// group with the host's exact matcher is replaced in place; otherwise every tilth handler
/// goes and a new group is appended. Other handlers and groups stay as they were.
fn upsert_nested(entries: &mut Vec<Value>, target: &HookTarget, new: Value, own_prefix: &str) {
    let handler = new["hooks"][0].clone();
    let is_tilth =
        |h: &Value| handler_line(h).is_some_and(|c| is_tilth_hook_command(&c, own_prefix));
    let exact = |g: &Value| g.get("matcher").and_then(Value::as_str) == target.matcher;
    let mut placed = false;
    for group in entries.iter_mut() {
        let in_exact = exact(group);
        let Some(hooks) = group.get_mut("hooks").and_then(Value::as_array_mut) else {
            continue;
        };
        let mut kept = Vec::with_capacity(hooks.len());
        let mut removed = false;
        for h in hooks.drain(..) {
            if !is_tilth(&h) {
                kept.push(h);
            } else if in_exact && !placed {
                kept.push(handler.clone());
                placed = true;
                removed = true;
            } else {
                removed = true;
            }
        }
        *hooks = kept;
        if removed && hooks.is_empty() {
            group["hooks"] = Value::Null;
        }
    }
    entries.retain(|g| !g.get("hooks").is_some_and(Value::is_null));
    if !placed {
        entries.push(new);
    }
}

/// Put exactly one tilth entry in the event array. A stale tilth entry is replaced in
/// place; every other entry stays as it was.
fn upsert_hook(
    config: &mut Value,
    target: &HookTarget,
    invocation: &HookInvocation,
) -> Result<(), String> {
    let root = config
        .as_object_mut()
        .ok_or("config root is not a JSON object")?;
    if target.version {
        root.entry("version").or_insert(json!(1));
    }
    let events = if target.wrap {
        root.entry("hooks")
            .or_insert(json!({}))
            .as_object_mut()
            .ok_or("hooks is not a JSON object")?
    } else {
        root
    };
    let entries = events
        .entry(target.event)
        .or_insert(json!([]))
        .as_array_mut()
        .ok_or_else(|| format!("{} is not a JSON array", target.event))?;
    let new = hook_entry(target, invocation);
    let own_prefix = invocation.own_prefix.as_str();
    if target.nested {
        upsert_nested(entries, target, new, own_prefix);
    } else {
        match entries
            .iter()
            .position(|e| is_tilth_hook_entry(e, own_prefix))
        {
            Some(first) => {
                entries[first] = new;
                let mut index = 0;
                entries.retain(|e| {
                    index += 1;
                    index - 1 <= first || !is_tilth_hook_entry(e, own_prefix)
                });
            }
            None => entries.push(new),
        }
    }
    Ok(())
}

/// Load the host's hook file and merge the tilth entry in memory, writing nothing.
fn prepare_hook(host: &str, home: &Path) -> Result<(HookTarget, Value), String> {
    let target = hook_target(host, home)?;
    let mut config: Value = if target.path.exists() {
        let raw = fs::read_to_string(&target.path)
            .map_err(|e| format!("failed to read {}: {e}", target.path.display()))?;
        serde_json::from_str(&raw)
            .map_err(|e| format!("invalid JSON in {}: {e}", target.path.display()))?
    } else {
        json!({})
    };
    upsert_hook(&mut config, &target, &hook_invocation(&target)?)?;
    Ok((target, config))
}

/// Register the shell guard for `host` under `home`. Returns the report to print.
fn install_hook(host: &str, home: &Path) -> Result<String, String> {
    let (target, config) = prepare_hook(host, home)?;
    if let Some(parent) = target.path.parent() {
        fs::create_dir_all(parent)
            .map_err(|e| format!("failed to create {}: {e}", parent.display()))?;
    }
    let json = serde_json::to_string_pretty(&config).map_err(|e| e.to_string())?;
    atomic_write(&target.path, &json)?;
    let mut report = format!("✓ tilth hook added to {}", target.path.display());
    if let Some(note) = target.note {
        report.push_str("\n  ");
        report.push_str(note);
    }
    Ok(report)
}

/// `install <host> --hook`: write the MCP config, then register the shell guard.
/// An unsupported host or an unreadable hook file fails before anything is written.
pub fn run_with_hook(host: &str) -> Result<(), String> {
    let home = home_dir()?;
    install_with_hook(host, &home, run)
}

fn install_with_hook(
    host: &str,
    home: &Path,
    write_mcp: impl FnOnce(&str) -> Result<(), String>,
) -> Result<(), String> {
    prepare_hook(host, home)?;
    write_mcp(host)?;
    eprintln!("{}", install_hook(host, home)?);
    Ok(())
}

/// Write `content` to `path` atomically: write to a sibling temp file first,
/// then rename over the target so an interrupted write never truncates `path`.
fn atomic_write(path: &std::path::Path, content: &str) -> Result<(), String> {
    crate::util::atomic_write_bytes(path, content.as_bytes())
        .map_err(|e| format!("failed to write {}: {e}", path.display()))
}

fn write_json_config(host_info: &HostInfo) -> Result<(), String> {
    let servers_key = match host_info.format {
        ConfigFormat::Json { servers_key } | ConfigFormat::JsonLocal { servers_key } => servers_key,
        ConfigFormat::Toml => unreachable!("write_json_config called for TOML host"),
    };

    let mut config: Value = if host_info.path.exists() {
        let raw = fs::read_to_string(&host_info.path)
            .map_err(|e| format!("failed to read {}: {e}", host_info.path.display()))?;
        serde_json::from_str(&raw)
            .map_err(|e| format!("invalid JSON in {}: {e}", host_info.path.display()))?
    } else {
        json!({})
    };

    upsert_json_server(
        &mut config,
        servers_key,
        tilth_server_entry(&host_info.format),
    )?;

    let out =
        serde_json::to_string_pretty(&config).expect("serde_json::Value is always serializable");
    atomic_write(&host_info.path, &out)?;
    Ok(())
}

/// Builds the `[mcp_servers.tilth]` TOML table for the given command/args.
/// Split from `upsert_toml_tilth_table` so a test can feed it arbitrary
/// strings (e.g. containing `"`) without going through `tilth_command_and_args`.
fn build_tilth_toml_table(command: &str, args: &[String]) -> toml_edit::Table {
    let mut table = toml_edit::Table::new();
    table["command"] = toml_edit::value(command);

    let mut args_arr = toml_edit::Array::new();
    for a in args {
        args_arr.push(a.as_str());
    }
    table["args"] = toml_edit::value(args_arr);
    table
}

/// Inserts `table` as `[mcp_servers.tilth]` under `root`, creating an
/// implicit `mcp_servers` parent table if absent so it renders as a bare
/// `[mcp_servers.tilth]` header rather than an empty `[mcp_servers]` one.
/// Plain `doc["mcp_servers"]["tilth"] = ...` auto-vivifies as a dotted inline
/// table instead of a real header, which does not round-trip through the
/// `toml` crate parser used elsewhere in this crate.
fn insert_tilth_table(root: &mut toml_edit::Table, table: toml_edit::Table) -> Result<(), String> {
    if !root.contains_key("mcp_servers") {
        let mut parent = toml_edit::Table::new();
        parent.set_implicit(true);
        root.insert("mcp_servers", toml_edit::Item::Table(parent));
    }
    let mcp_servers = root["mcp_servers"]
        .as_table_mut()
        .ok_or("mcp_servers is not a TOML table")?;
    mcp_servers.insert("tilth", toml_edit::Item::Table(table));
    Ok(())
}

/// Inserts/replaces the `[mcp_servers.tilth]` table in a parsed TOML document,
/// preserving every other table, key, and comment via `toml_edit`'s
/// format-preserving edit model.
fn upsert_toml_tilth_table(doc: &mut toml_edit::DocumentMut) -> Result<(), String> {
    let (command, args) = tilth_command_and_args();
    let table = build_tilth_toml_table(&command, &args);
    insert_tilth_table(doc.as_table_mut(), table)
}

/// Writes a `[mcp_servers.tilth]` section into a TOML config file, preserving
/// the rest of the document (formatting, comments, other tables) untouched.
fn write_toml_config(host_info: &HostInfo) -> Result<(), String> {
    let existing = if host_info.path.exists() {
        fs::read_to_string(&host_info.path)
            .map_err(|e| format!("failed to read {}: {e}", host_info.path.display()))?
    } else {
        String::new()
    };

    let mut doc: toml_edit::DocumentMut = existing
        .parse()
        .map_err(|e| format!("invalid TOML in {}: {e}", host_info.path.display()))?;

    upsert_toml_tilth_table(&mut doc).map_err(|e| format!("{}: {e}", host_info.path.display()))?;

    atomic_write(&host_info.path, &doc.to_string())?;
    Ok(())
}

/// Returns (command, args) for the tilth MCP server entry.
fn tilth_command_and_args() -> (String, Vec<String>) {
    let mcp_args: Vec<String> = vec!["--mcp".into()];

    let via_npm = std::env::current_exe()
        .ok()
        .and_then(|p| p.to_str().map(|s| s.contains("node_modules")))
        .unwrap_or(false);

    if via_npm {
        let mut args = vec!["tilth".to_string()];
        args.extend(mcp_args);
        ("npx".into(), args)
    } else {
        let command = std::env::current_exe()
            .ok()
            .and_then(|p| p.to_str().map(String::from))
            .unwrap_or_else(|| "tilth".into());
        (command, mcp_args)
    }
}

#[derive(Debug)]
enum ConfigFormat {
    /// JSON with a configurable servers key, using standard {command, args} entry shape.
    Json { servers_key: &'static str },
    /// JSON with a configurable servers key, using opencode's local entry shape {type, command[]}.
    JsonLocal { servers_key: &'static str },
    /// TOML with `[mcp_servers.<name>]` sections.
    Toml,
}

struct HostInfo {
    path: PathBuf,
    format: ConfigFormat,
    /// Optional note printed after success.
    note: Option<&'static str>,
}

fn resolve_host(host: &str) -> Result<HostInfo, String> {
    let home = home_dir()?;

    match host {
        // Claude Code user scope: ~/.claude.json → mcpServers
        "claude-code" => Ok(HostInfo {
            path: home.join(".claude.json"),
            format: ConfigFormat::Json {
                servers_key: "mcpServers",
            },
            note: None,
        }),

        // Cursor global: ~/.cursor/mcp.json → mcpServers
        "cursor" => Ok(HostInfo {
            path: home.join(".cursor/mcp.json"),
            format: ConfigFormat::Json {
                servers_key: "mcpServers",
            },
            note: None,
        }),

        // Windsurf global: ~/.codeium/windsurf/mcp_config.json → mcpServers
        "windsurf" => Ok(HostInfo {
            path: home.join(".codeium/windsurf/mcp_config.json"),
            format: ConfigFormat::Json {
                servers_key: "mcpServers",
            },
            note: None,
        }),

        // VS Code project scope: .vscode/mcp.json → servers (NOT mcpServers)
        "vscode" => Ok(HostInfo {
            path: PathBuf::from(".vscode/mcp.json"),
            format: ConfigFormat::Json {
                servers_key: "servers",
            },
            note: Some("Project scope — run from your project root."),
        }),

        "claude-desktop" => Ok(HostInfo {
            path: claude_desktop_path()?,
            format: ConfigFormat::Json {
                servers_key: "mcpServers",
            },
            note: None,
        }),

        // OpenCode user scope: ~/.config/opencode/opencode.json → mcp (local entry shape)
        "opencode" => Ok(HostInfo {
            path: home.join(".config/opencode/opencode.json"),
            format: ConfigFormat::JsonLocal { servers_key: "mcp" },
            note: Some("User scope — available in all projects."),
        }),

        // Gemini CLI user scope: ~/.gemini/settings.json → mcpServers
        "gemini" => Ok(HostInfo {
            path: home.join(".gemini/settings.json"),
            format: ConfigFormat::Json {
                servers_key: "mcpServers",
            },
            note: Some("User scope — available in all projects."),
        }),

        // Codex CLI user scope: ~/.codex/config.toml → [mcp_servers.tilth] (TOML)
        "codex" => Ok(HostInfo {
            path: home.join(".codex/config.toml"),
            format: ConfigFormat::Toml,
            note: Some("User scope — available in all projects."),
        }),

        // Amp user scope: ~/.config/amp/settings.json → amp.mcpServers
        // Verified from official docs: https://ampcode.com/manual
        "amp" => Ok(HostInfo {
            path: home.join(".config/amp/settings.json"),
            format: ConfigFormat::Json {
                servers_key: "amp.mcpServers",
            },
            note: Some("User scope — available in all projects."),
        }),

        // Google Antigravity user scope: ~/.gemini/antigravity/mcp_config.json → mcpServers
        // Verified from official docs: https://antigravity.google/docs/mcp
        "antigravity" => Ok(HostInfo {
            path: home.join(".gemini/antigravity/mcp_config.json"),
            format: ConfigFormat::Json {
                servers_key: "mcpServers",
            },
            note: Some("User scope — available in all projects."),
        }),

        // Factory Droid user scope: ~/.factory/mcp.json → mcpServers
        // Verified from official docs: https://docs.factory.ai/cli/configuration/mcp
        "droid" => Ok(HostInfo {
            path: home.join(".factory/mcp.json"),
            format: ConfigFormat::Json {
                servers_key: "mcpServers",
            },
            note: Some("User scope — available in all projects."),
        }),

        // Zed user scope: ~/.config/zed/settings.json → context_servers (NOT mcpServers)
        // Verified from official docs: https://zed.dev/docs/ai/mcp
        "zed" => Ok(HostInfo {
            path: home.join(".config/zed/settings.json"),
            format: ConfigFormat::Json {
                servers_key: "context_servers",
            },
            note: Some("User scope — available in all projects."),
        }),

        // GitHub Copilot CLI user scope: ~/.copilot/mcp-config.json → mcpServers
        // Verified from official docs: https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers
        "copilot-cli" => Ok(HostInfo {
            path: home.join(".copilot/mcp-config.json"),
            format: ConfigFormat::Json {
                servers_key: "mcpServers",
            },
            note: Some("User scope — available in all projects."),
        }),

        // AugmentCode user scope: ~/.augment/settings.json → mcpServers
        // Verified from official docs: https://docs.augmentcode.com/cli/integrations
        "augment" => Ok(HostInfo {
            path: home.join(".augment/settings.json"),
            format: ConfigFormat::Json {
                servers_key: "mcpServers",
            },
            note: Some("User scope — available in all projects."),
        }),

        // Kiro user scope: ~/.kiro/settings/mcp.json → mcpServers
        // Verified from official docs: https://kiro.dev/docs/mcp/configuration/
        "kiro" => Ok(HostInfo {
            path: home.join(".kiro/settings/mcp.json"),
            format: ConfigFormat::Json {
                servers_key: "mcpServers",
            },
            note: Some("User scope — available in all projects."),
        }),

        // Kilo Code (VS Code extension): globalStorage → mcpServers
        // Verified from official docs: https://kilo.ai/docs/automate/mcp/using-in-kilo-code
        "kilo-code" => Ok(HostInfo {
            path: vscode_global_storage_path("kilocode.kilo-code", "mcp_settings.json")?,
            format: ConfigFormat::Json {
                servers_key: "mcpServers",
            },
            note: None,
        }),

        // Cline (VS Code extension): globalStorage → mcpServers
        // Verified from official docs: https://docs.cline.bot/mcp-servers/configuring-mcp-servers
        "cline" => Ok(HostInfo {
            path: vscode_global_storage_path("saoudrizwan.claude-dev", "cline_mcp_settings.json")?,
            format: ConfigFormat::Json {
                servers_key: "mcpServers",
            },
            note: None,
        }),

        // Roo Code (VS Code extension): globalStorage → mcpServers
        // Verified from official docs: https://docs.roocode.com/features/mcp/using-mcp-in-roo
        "roo-code" => Ok(HostInfo {
            path: vscode_global_storage_path("rooveterinaryinc.roo-cline", "mcp_settings.json")?,
            format: ConfigFormat::Json {
                servers_key: "mcpServers",
            },
            note: None,
        }),

        // Trae project scope: .trae/mcp.json → mcpServers
        // Verified from official docs: https://docs.trae.ai/ide/add-mcp-servers
        "trae" => Ok(HostInfo {
            path: PathBuf::from(".trae/mcp.json"),
            format: ConfigFormat::Json {
                servers_key: "mcpServers",
            },
            note: Some("Project scope — run from your project root."),
        }),

        // Qwen Code user scope: ~/.qwen/settings.json → mcpServers
        // Verified from official docs: https://qwenlm.github.io/qwen-code-docs/en/users/features/mcp/
        "qwen-code" => Ok(HostInfo {
            path: home.join(".qwen/settings.json"),
            format: ConfigFormat::Json {
                servers_key: "mcpServers",
            },
            note: Some("User scope — available in all projects."),
        }),

        // Crush user scope: ~/.config/crush/crush.json → mcp (NOT mcpServers)
        // Verified from official docs: https://github.com/charmbracelet/crush
        "crush" => Ok(HostInfo {
            path: home.join(".config/crush/crush.json"),
            format: ConfigFormat::Json { servers_key: "mcp" },
            note: Some("User scope — available in all projects."),
        }),

        // Pi coding agent user scope: ~/.pi/agent/mcp.json → mcpServers
        // Verified from: https://github.com/badlogic/pi-mono/issues/563
        "pi" => Ok(HostInfo {
            path: home.join(".pi/agent/mcp.json"),
            format: ConfigFormat::Json {
                servers_key: "mcpServers",
            },
            note: Some("User scope — available in all projects."),
        }),

        _ => Err(format!(
            "unknown host: {host}. Supported: {}",
            SUPPORTED_HOSTS.join(", ")
        )),
    }
}

/// Cross-platform home-directory lookup, with an actionable error message.
fn home_dir() -> Result<PathBuf, String> {
    home::home_dir().ok_or_else(|| "home directory not found ($HOME / $USERPROFILE)".into())
}

/// Merge a tilth server entry into a JSON config under the given servers key.
/// Extracted for testability — used by `write_json_config` and unit tests.
fn upsert_json_server(config: &mut Value, servers_key: &str, entry: Value) -> Result<(), String> {
    config
        .as_object_mut()
        .ok_or("config root is not a JSON object")?
        .entry(servers_key)
        .or_insert(json!({}))
        .as_object_mut()
        .ok_or_else(|| format!("{servers_key} is not a JSON object"))?
        .insert("tilth".into(), entry);
    Ok(())
}

/// Returns the VS Code globalStorage path for a given extension and settings filename.
fn vscode_global_storage_path(extension_id: &str, filename: &str) -> Result<PathBuf, String> {
    let base = vscode_global_storage_base()?;
    Ok(base.join(extension_id).join("settings").join(filename))
}

fn vscode_global_storage_base() -> Result<PathBuf, String> {
    #[cfg(target_os = "macos")]
    {
        let home = home_dir()?;
        Ok(home.join("Library/Application Support/Code/User/globalStorage"))
    }

    #[cfg(target_os = "windows")]
    {
        let appdata = std::env::var("APPDATA").map_err(|_| "APPDATA not set")?;
        Ok(PathBuf::from(appdata).join("Code/User/globalStorage"))
    }

    #[cfg(target_os = "linux")]
    {
        let home = home_dir()?;
        Ok(home.join(".config/Code/User/globalStorage"))
    }

    #[cfg(not(any(target_os = "macos", target_os = "windows", target_os = "linux")))]
    {
        Err("VS Code globalStorage path unknown on this OS".into())
    }
}

fn claude_desktop_path() -> Result<PathBuf, String> {
    #[cfg(target_os = "macos")]
    {
        let home = home_dir()?;
        Ok(home.join("Library/Application Support/Claude/claude_desktop_config.json"))
    }

    #[cfg(target_os = "windows")]
    {
        let appdata = std::env::var("APPDATA").map_err(|_| "APPDATA not set")?;
        Ok(PathBuf::from(appdata).join("Claude/claude_desktop_config.json"))
    }

    #[cfg(not(any(target_os = "macos", target_os = "windows")))]
    {
        Err("claude-desktop config path unknown on this OS".into())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn toml_section_appended_when_absent() {
        let mut doc: toml_edit::DocumentMut = "[other]\nk = 1\n".parse().unwrap();
        upsert_toml_tilth_table(&mut doc).unwrap();
        let out = doc.to_string();
        assert!(out.contains("[other]"));
        assert!(out.contains("[mcp_servers.tilth]"));
        assert_eq!(doc["other"]["k"].as_integer(), Some(1));
    }

    #[test]
    fn toml_preserves_comments_and_unrelated_section() {
        let existing = "# legacy note about [mcp_servers.tilth] kept for humans\n[other]\nk = 1\n";
        let mut doc: toml_edit::DocumentMut = existing.parse().unwrap();
        upsert_toml_tilth_table(&mut doc).unwrap();
        let out = doc.to_string();
        assert!(
            out.contains("# legacy note about [mcp_servers.tilth] kept for humans"),
            "comment lost during upsert: {out:?}"
        );
        assert!(out.contains("[other]"));
        assert_eq!(
            doc["other"]["k"].as_integer(),
            Some(1),
            "unrelated section survived"
        );
        assert!(doc["mcp_servers"]["tilth"]["command"].as_str().is_some());
    }

    #[test]
    fn toml_section_replaces_existing_tilth_table() {
        let existing = "[mcp_servers.tilth]\ncommand = \"old\"\nargs = []\n[other]\nk = 1\n";
        let mut doc: toml_edit::DocumentMut = existing.parse().unwrap();
        upsert_toml_tilth_table(&mut doc).unwrap();
        let out = doc.to_string();
        assert!(!out.contains("\"old\""), "old command not removed: {out:?}");
        assert!(out.contains("[other]"));
        assert_eq!(doc["other"]["k"].as_integer(), Some(1));
    }

    #[test]
    fn toml_quoted_command_and_arg_round_trip() {
        // A `"` in the resolved command path or an arg must not corrupt the
        // emitted TOML — toml_edit escapes it, and re-parsing must recover
        // the exact original string.
        let command = "C:\\Program Files\\has \"quotes\"\\tilth.exe";
        let args = vec!["--mcp".to_string(), "say \"hi\"".to_string()];
        let table = build_tilth_toml_table(command, &args);

        let mut doc = toml_edit::DocumentMut::new();
        insert_tilth_table(doc.as_table_mut(), table).unwrap();
        let text = doc.to_string();

        let reparsed: toml_edit::DocumentMut = text.parse().expect("emitted TOML must parse");
        assert_eq!(
            reparsed["mcp_servers"]["tilth"]["command"].as_str(),
            Some(command)
        );
        let reparsed_args: Vec<&str> = reparsed["mcp_servers"]["tilth"]["args"]
            .as_array()
            .expect("args must be an array")
            .iter()
            .map(|v| v.as_str().expect("arg must be a string"))
            .collect();
        assert_eq!(reparsed_args, vec!["--mcp", "say \"hi\""]);

        // Also verify via the standalone `toml` crate (used elsewhere in this
        // crate) so the fix isn't just self-consistent with toml_edit.
        let via_toml: toml::Value = toml::from_str(&text).expect("toml crate must also parse it");
        assert_eq!(
            via_toml["mcp_servers"]["tilth"]["command"].as_str(),
            Some(command)
        );
    }

    #[test]
    fn write_toml_config_error_includes_path_when_mcp_servers_not_a_table() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("config.toml");
        fs::write(&path, "mcp_servers = 1\n").unwrap();
        let host_info = HostInfo {
            path: path.clone(),
            format: ConfigFormat::Toml,
            note: None,
        };
        let err = write_toml_config(&host_info).unwrap_err();
        assert!(
            err.contains(&path.display().to_string()),
            "error must include config path, got: {err:?}"
        );
    }

    #[test]
    fn amp_resolve_host() {
        let info = resolve_host("amp").expect("amp should resolve");
        assert!(
            info.path.ends_with(".config/amp/settings.json"),
            "path should end with .config/amp/settings.json, got: {}",
            info.path.display()
        );
        match info.format {
            ConfigFormat::Json { servers_key } => {
                assert_eq!(servers_key, "amp.mcpServers");
            }
            ConfigFormat::JsonLocal { .. } => panic!("amp should use Json format, not JsonLocal"),
            ConfigFormat::Toml => panic!("amp should use JSON format, not TOML"),
        }
    }

    #[test]
    fn amp_dotted_key_is_literal_not_nested() {
        let mut config = json!({});
        let entry = json!({"command": "tilth", "args": ["--mcp"]});
        upsert_json_server(&mut config, "amp.mcpServers", entry).unwrap();

        // Top-level key must be the literal "amp.mcpServers"
        assert!(
            config.get("amp.mcpServers").is_some(),
            "should have literal top-level key 'amp.mcpServers'"
        );
        // Must NOT create a nested "amp" object
        assert!(
            config.get("amp").is_none(),
            "should NOT have a nested 'amp' key"
        );
        // Verify tilth entry is inside
        assert_eq!(config["amp.mcpServers"]["tilth"]["command"], json!("tilth"));
    }

    #[test]
    fn amp_preserves_unrelated_config() {
        let mut config = json!({
            "amp.theme": "dark",
            "amp.mcpServers": {
                "other": {"command": "foo", "args": []}
            }
        });
        let entry = json!({"command": "tilth", "args": ["--mcp"]});
        upsert_json_server(&mut config, "amp.mcpServers", entry).unwrap();

        assert_eq!(config["amp.theme"], json!("dark"));
        assert_eq!(config["amp.mcpServers"]["other"]["command"], json!("foo"));
        assert!(config["amp.mcpServers"]["tilth"].is_object());
    }

    #[test]
    fn amp_overwrites_existing_tilth() {
        let mut config = json!({
            "amp.mcpServers": {
                "tilth": {"command": "old", "args": ["--old"]}
            }
        });
        let entry = json!({"command": "tilth", "args": ["--mcp"]});
        upsert_json_server(&mut config, "amp.mcpServers", entry).unwrap();

        assert_eq!(config["amp.mcpServers"]["tilth"]["args"], json!(["--mcp"]));
    }

    #[test]
    fn amp_overwrite_drops_stale_env_key() {
        let mut config = json!({
            "amp.mcpServers": {
                "tilth": {"command": "old", "args": ["--old"], "env": {"TILTH_MCP_CWD_HOOK_INJECTED": "1"}}
            }
        });
        let entry = json!({"command": "tilth", "args": ["--mcp"]});
        upsert_json_server(&mut config, "amp.mcpServers", entry).unwrap();

        // Asserting the whole entry, not just `env`'s absence: indexing a missing
        // key yields `Value::Null`, whose `.get("env")` is also `None`, so an
        // absence-only check would pass even if the upsert dropped `tilth` entirely.
        assert_eq!(
            config["amp.mcpServers"]["tilth"],
            json!({"command": "tilth", "args": ["--mcp"]}),
            "upsert must replace the whole entry, dropping the stale env key: {config:?}"
        );
    }

    #[test]
    fn amp_error_when_servers_key_not_object() {
        let mut config = json!({"amp.mcpServers": []});
        let entry = json!({"command": "tilth", "args": ["--mcp"]});
        let err = upsert_json_server(&mut config, "amp.mcpServers", entry).unwrap_err();
        assert!(
            err.contains("amp.mcpServers is not a JSON object"),
            "error should mention the key, got: {err}"
        );
    }

    #[test]
    fn droid_resolve_host() {
        let info = resolve_host("droid").expect("droid should resolve");
        assert!(
            info.path.ends_with(".factory/mcp.json"),
            "path should end with .factory/mcp.json, got: {}",
            info.path.display()
        );
        match info.format {
            ConfigFormat::Json { servers_key } => {
                assert_eq!(servers_key, "mcpServers");
            }
            ConfigFormat::JsonLocal { .. } => panic!("droid should use Json format, not JsonLocal"),
            ConfigFormat::Toml => panic!("droid should use JSON format, not TOML"),
        }
    }

    #[test]
    fn droid_preserves_existing_servers() {
        let mut config = json!({
            "mcpServers": {
                "playwright": {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]}
            }
        });
        let entry = json!({"command": "tilth", "args": ["--mcp"]});
        upsert_json_server(&mut config, "mcpServers", entry).unwrap();

        assert_eq!(config["mcpServers"]["playwright"]["command"], json!("npx"));
        assert!(config["mcpServers"]["tilth"].is_object());
    }

    #[test]
    fn unknown_host_error_includes_droid() {
        let err = resolve_host("nope")
            .err()
            .expect("unknown host should return an error");
        assert!(
            err.contains("droid"),
            "error should list droid in supported hosts, got: {err}"
        );
    }

    #[test]
    fn antigravity_resolve_host() {
        let info = resolve_host("antigravity").expect("antigravity should resolve");
        assert!(
            info.path.ends_with(".gemini/antigravity/mcp_config.json"),
            "path should end with .gemini/antigravity/mcp_config.json, got: {}",
            info.path.display()
        );
        match info.format {
            ConfigFormat::Json { servers_key } => {
                assert_eq!(servers_key, "mcpServers");
            }
            ConfigFormat::JsonLocal { .. } => {
                panic!("antigravity should use Json format, not JsonLocal")
            }
            ConfigFormat::Toml => panic!("antigravity should use JSON format, not TOML"),
        }
    }

    #[test]
    fn antigravity_preserves_existing_servers() {
        let mut config = json!({
            "mcpServers": {
                "firebase": {"command": "npx", "args": ["-y", "firebase-tools@latest", "mcp"]}
            }
        });
        let entry = json!({"command": "tilth", "args": ["--mcp"]});
        upsert_json_server(&mut config, "mcpServers", entry).unwrap();

        assert_eq!(config["mcpServers"]["firebase"]["command"], json!("npx"));
        assert!(config["mcpServers"]["tilth"].is_object());
    }

    #[test]
    fn unknown_host_error_includes_antigravity() {
        let err = resolve_host("nope")
            .err()
            .expect("unknown host should return an error");
        assert!(
            err.contains("antigravity"),
            "error should list antigravity in supported hosts, got: {err}"
        );
    }

    #[test]
    fn zed_resolve_host() {
        let info = resolve_host("zed").expect("zed should resolve");
        assert!(
            info.path.ends_with(".config/zed/settings.json"),
            "path should end with .config/zed/settings.json, got: {}",
            info.path.display()
        );
        match info.format {
            ConfigFormat::Json { servers_key } => {
                assert_eq!(servers_key, "context_servers");
            }
            ConfigFormat::JsonLocal { .. } => panic!("zed should use Json format, not JsonLocal"),
            ConfigFormat::Toml => panic!("zed should use JSON format, not TOML"),
        }
    }

    #[test]
    fn zed_uses_context_servers_not_mcp_servers() {
        let mut config = json!({});
        let entry = json!({"command": "tilth", "args": ["--mcp"]});
        upsert_json_server(&mut config, "context_servers", entry).unwrap();

        assert!(config.get("context_servers").is_some());
        assert!(config.get("mcpServers").is_none());
        assert_eq!(
            config["context_servers"]["tilth"]["command"],
            json!("tilth")
        );
    }

    #[test]
    fn copilot_cli_resolve_host() {
        let info = resolve_host("copilot-cli").expect("copilot-cli should resolve");
        assert!(
            info.path.ends_with(".copilot/mcp-config.json"),
            "path should end with .copilot/mcp-config.json, got: {}",
            info.path.display()
        );
        match info.format {
            ConfigFormat::Json { servers_key } => {
                assert_eq!(servers_key, "mcpServers");
            }
            ConfigFormat::JsonLocal { .. } => {
                panic!("copilot-cli should use Json format, not JsonLocal")
            }
            ConfigFormat::Toml => panic!("copilot-cli should use JSON format, not TOML"),
        }
    }

    #[test]
    fn augment_resolve_host() {
        let info = resolve_host("augment").expect("augment should resolve");
        assert!(
            info.path.ends_with(".augment/settings.json"),
            "path should end with .augment/settings.json, got: {}",
            info.path.display()
        );
        match info.format {
            ConfigFormat::Json { servers_key } => {
                assert_eq!(servers_key, "mcpServers");
            }
            ConfigFormat::JsonLocal { .. } => {
                panic!("augment should use Json format, not JsonLocal")
            }
            ConfigFormat::Toml => panic!("augment should use JSON format, not TOML"),
        }
    }

    #[test]
    fn kiro_resolve_host() {
        let info = resolve_host("kiro").expect("kiro should resolve");
        assert!(
            info.path.ends_with(".kiro/settings/mcp.json"),
            "path should end with .kiro/settings/mcp.json, got: {}",
            info.path.display()
        );
        match info.format {
            ConfigFormat::Json { servers_key } => {
                assert_eq!(servers_key, "mcpServers");
            }
            ConfigFormat::JsonLocal { .. } => panic!("kiro should use Json format, not JsonLocal"),
            ConfigFormat::Toml => panic!("kiro should use JSON format, not TOML"),
        }
    }

    #[test]
    fn kilo_code_resolve_host() {
        let info = resolve_host("kilo-code").expect("kilo-code should resolve");
        let path_str = info.path.to_string_lossy();
        assert!(
            path_str.contains("kilocode.kilo-code") && path_str.contains("mcp_settings.json"),
            "path should contain kilocode.kilo-code and mcp_settings.json, got: {path_str}",
        );
        match info.format {
            ConfigFormat::Json { servers_key } => {
                assert_eq!(servers_key, "mcpServers");
            }
            ConfigFormat::JsonLocal { .. } => {
                panic!("kilo-code should use Json format, not JsonLocal")
            }
            ConfigFormat::Toml => panic!("kilo-code should use JSON format, not TOML"),
        }
    }

    #[test]
    fn cline_resolve_host() {
        let info = resolve_host("cline").expect("cline should resolve");
        let path_str = info.path.to_string_lossy();
        assert!(
            path_str.contains("saoudrizwan.claude-dev")
                && path_str.contains("cline_mcp_settings.json"),
            "path should contain saoudrizwan.claude-dev and cline_mcp_settings.json, got: {path_str}",
        );
        match info.format {
            ConfigFormat::Json { servers_key } => {
                assert_eq!(servers_key, "mcpServers");
            }
            ConfigFormat::JsonLocal { .. } => panic!("cline should use Json format, not JsonLocal"),
            ConfigFormat::Toml => panic!("cline should use JSON format, not TOML"),
        }
    }

    #[test]
    fn roo_code_resolve_host() {
        let info = resolve_host("roo-code").expect("roo-code should resolve");
        let path_str = info.path.to_string_lossy();
        assert!(
            path_str.contains("rooveterinaryinc.roo-cline")
                && path_str.contains("mcp_settings.json"),
            "path should contain rooveterinaryinc.roo-cline and mcp_settings.json, got: {path_str}",
        );
        match info.format {
            ConfigFormat::Json { servers_key } => {
                assert_eq!(servers_key, "mcpServers");
            }
            ConfigFormat::JsonLocal { .. } => {
                panic!("roo-code should use Json format, not JsonLocal")
            }
            ConfigFormat::Toml => panic!("roo-code should use JSON format, not TOML"),
        }
    }

    #[test]
    fn trae_resolve_host() {
        let info = resolve_host("trae").expect("trae should resolve");
        assert!(
            info.path.ends_with(".trae/mcp.json"),
            "path should end with .trae/mcp.json, got: {}",
            info.path.display()
        );
        match info.format {
            ConfigFormat::Json { servers_key } => {
                assert_eq!(servers_key, "mcpServers");
            }
            ConfigFormat::JsonLocal { .. } => panic!("trae should use Json format, not JsonLocal"),
            ConfigFormat::Toml => panic!("trae should use JSON format, not TOML"),
        }
        assert_eq!(
            info.note,
            Some("Project scope — run from your project root.")
        );
    }

    #[test]
    fn qwen_code_resolve_host() {
        let info = resolve_host("qwen-code").expect("qwen-code should resolve");
        assert!(
            info.path.ends_with(".qwen/settings.json"),
            "path should end with .qwen/settings.json, got: {}",
            info.path.display()
        );
        match info.format {
            ConfigFormat::Json { servers_key } => {
                assert_eq!(servers_key, "mcpServers");
            }
            ConfigFormat::JsonLocal { .. } => {
                panic!("qwen-code should use Json format, not JsonLocal")
            }
            ConfigFormat::Toml => panic!("qwen-code should use JSON format, not TOML"),
        }
    }

    #[test]
    fn crush_resolve_host() {
        let info = resolve_host("crush").expect("crush should resolve");
        assert!(
            info.path.ends_with(".config/crush/crush.json"),
            "path should end with .config/crush/crush.json, got: {}",
            info.path.display()
        );
        match info.format {
            ConfigFormat::Json { servers_key } => {
                assert_eq!(servers_key, "mcp");
            }
            ConfigFormat::JsonLocal { .. } => panic!("crush should use Json format, not JsonLocal"),
            ConfigFormat::Toml => panic!("crush should use JSON format, not TOML"),
        }
    }

    #[test]
    fn crush_uses_mcp_not_mcp_servers() {
        let mut config = json!({});
        let entry = json!({"command": "tilth", "args": ["--mcp"]});
        upsert_json_server(&mut config, "mcp", entry).unwrap();

        assert!(config.get("mcp").is_some());
        assert!(config.get("mcpServers").is_none());
        assert_eq!(config["mcp"]["tilth"]["command"], json!("tilth"));
    }

    #[test]
    fn pi_resolve_host() {
        let info = resolve_host("pi").expect("pi should resolve");
        assert!(
            info.path.ends_with(".pi/agent/mcp.json"),
            "path should end with .pi/agent/mcp.json, got: {}",
            info.path.display()
        );
        match info.format {
            ConfigFormat::Json { servers_key } => {
                assert_eq!(servers_key, "mcpServers");
            }
            ConfigFormat::JsonLocal { .. } => panic!("pi should use Json format, not JsonLocal"),
            ConfigFormat::Toml => panic!("pi should use JSON format, not TOML"),
        }
    }

    #[test]
    fn unknown_host_error_includes_amp() {
        let err = resolve_host("nope")
            .err()
            .expect("unknown host should return an error");
        assert!(
            err.contains("amp"),
            "error should list amp in supported hosts, got: {err}"
        );
    }

    #[test]
    fn opencode_resolve_host() {
        let info = resolve_host("opencode").expect("opencode should resolve");
        assert!(
            info.path.ends_with(".config/opencode/opencode.json"),
            "path should end with .config/opencode/opencode.json, got: {}",
            info.path.display()
        );
        match info.format {
            ConfigFormat::JsonLocal { servers_key } => {
                assert_eq!(servers_key, "mcp");
            }
            ConfigFormat::Json { .. } => panic!("opencode should use JsonLocal format, not Json"),
            ConfigFormat::Toml => panic!("opencode should use JSON format, not TOML"),
        }
    }

    #[test]
    fn opencode_entry_uses_local_shape() {
        let entry = tilth_server_entry(&ConfigFormat::JsonLocal { servers_key: "mcp" });
        assert_eq!(entry["type"], json!("local"));
        assert!(entry["command"].is_array());
        assert!(entry.get("args").is_none());
    }

    #[test]
    fn entry_omits_the_retired_edit_flag() {
        let entry = tilth_server_entry(&ConfigFormat::JsonLocal { servers_key: "mcp" });
        let cmd = entry["command"].as_array().unwrap();
        assert!(cmd.iter().any(|v| v == "--mcp"));
        assert!(!cmd.iter().any(|v| v == "--edit"));
    }

    #[test]
    fn standard_entry_format() {
        let entry = tilth_server_entry(&ConfigFormat::Json {
            servers_key: "mcpServers",
        });
        assert!(entry.get("type").is_none());
        assert!(entry["command"].is_string());
        assert!(entry["args"].is_array());
    }

    #[test]
    fn opencode_upserts_under_mcp_key() {
        let mut config = json!({});
        let entry = tilth_server_entry(&ConfigFormat::JsonLocal { servers_key: "mcp" });
        upsert_json_server(&mut config, "mcp", entry).unwrap();

        assert!(config.get("mcp").is_some());
        assert!(config.get("mcpServers").is_none());
        assert_eq!(config["mcp"]["tilth"]["type"], json!("local"));
        assert!(config["mcp"]["tilth"]["command"].is_array());
    }

    /// The message is this function's entire product, and it names two operator-facing
    /// paths plus the hook matcher. Pin the full text so drifting it — or drifting the
    /// path `run` actually probes — fails here instead of telling an operator to delete
    /// a file tilth never looked for.
    #[test]
    fn stale_hook_warning_present_when_script_exists() {
        assert_eq!(
            stale_hook_warning(true),
            Some(
                "tilth: found a stale Claude Code cwd-injection hook. Delete ~/.claude/tilth/inject-cwd.js and remove the mcp__tilth__.* PreToolUse entry from ~/.claude/settings.json — tilth no longer ships this hook, and leaving it wired means an omitted cwd is silently filled with the session root instead of tilth's teaching refusal."
            )
        );
    }

    #[test]
    fn stale_hook_warning_absent_when_script_missing() {
        assert_eq!(stale_hook_warning(false), None);
    }

    fn fixture(host: &str) -> Value {
        let path = format!(
            "{}/tests/fixtures/hook_hosts/{host}.json",
            env!("CARGO_MANIFEST_DIR")
        );
        serde_json::from_str(&fs::read_to_string(path).unwrap()).unwrap()
    }

    /// Parse an official-doc snippet: a bare `"k": [...]` fragment or JSONC trailing commas.
    fn parse_snippet(snippet: &str) -> Value {
        let body = snippet.trim().trim_end_matches(',');
        let body = if body.starts_with('{') {
            body.to_string()
        } else {
            format!("{{{body}}}")
        };
        let chars: Vec<char> = body.chars().collect();
        let (mut out, mut in_str, mut esc) = (String::new(), false, false);
        for (i, &c) in chars.iter().enumerate() {
            if in_str {
                in_str = c != '"' || esc;
                esc = c == '\\' && !esc;
            } else if c == '"' {
                in_str = true;
            } else if c == ',' {
                let next = chars[i + 1..].iter().find(|c| !c.is_whitespace());
                if matches!(next, Some('}' | ']')) {
                    continue;
                }
            }
            out.push(c);
        }
        serde_json::from_str(&out).unwrap_or_else(|e| panic!("snippet not JSON: {e}\n{out}"))
    }

    fn first_event_entry<'a>(v: &'a Value, event: &str) -> Option<&'a Value> {
        let obj = v.as_object()?;
        if let Some(arr) = obj.get(event).and_then(Value::as_array) {
            return arr.first();
        }
        obj.values().find_map(|c| first_event_entry(c, event))
    }

    fn structural_keys(entry: &Value) -> std::collections::BTreeSet<String> {
        let mut keys = std::collections::BTreeSet::new();
        for k in ["matcher", "hooks", "command"] {
            if entry.get(k).is_some() {
                keys.insert(k.to_string());
            }
        }
        if let Some(inner) = entry.pointer("/hooks/0") {
            for k in ["type", "command"] {
                if inner.get(k).is_some() {
                    keys.insert(format!("hooks.{k}"));
                }
            }
        }
        keys
    }

    fn read_json(path: &Path) -> Value {
        serde_json::from_str(&fs::read_to_string(path).unwrap()).unwrap()
    }

    #[test]
    fn hook_fixtures_carry_required_fields() {
        for host in HOOK_HOSTS {
            let f = fixture(host);
            assert!(
                f["source_urls"].as_array().is_some_and(|a| !a.is_empty()),
                "{host}: source_urls"
            );
            assert!(f["registration"]["snippet"].is_string(), "{host}: snippet");
            assert!(f["stdin"]["command_path"].is_string(), "{host}: stdin");
            assert!(f["deny"].is_object(), "{host}: deny");
        }
    }

    #[test]
    fn written_hook_entry_matches_fixture_snippet() {
        for host in HOOK_HOSTS {
            let f = fixture(host);
            let home = tempfile::tempdir().unwrap();
            let target = hook_target(host, home.path()).unwrap();
            install_hook(host, home.path()).unwrap();

            let rel = target.path.strip_prefix(home.path()).unwrap();
            assert!(
                f["config_path"]
                    .as_str()
                    .unwrap()
                    .contains(&format!("~/{}", rel.display())),
                "{host}: config_path"
            );

            let snippet = f["registration"]["snippet"].as_str().unwrap();
            let doc = parse_snippet(snippet);
            if snippet.trim().starts_with('{') && *host != "droid" {
                assert_eq!(doc.get("hooks").is_some(), target.wrap, "{host}: wrap");
            }
            if *host == "droid" {
                // Doc: standalone hooks.json is unwrapped; settings.json wraps in `hooks`.
                assert!(target.wrap, "{host}: fresh install targets settings.json");
                assert!(f["registration"]["settings_json_variant"].is_string());
            }
            let theirs = first_event_entry(&doc, target.event)
                .unwrap_or_else(|| panic!("{host}: no {} in snippet", target.event));
            let written = read_json(&target.path);
            let ours = first_event_entry(&written, target.event).unwrap();

            let mut want = structural_keys(theirs);
            if target.matcher.is_none() {
                want.remove("matcher");
            }
            assert_eq!(structural_keys(ours), want, "{host}: structural keys");
            if target.matcher.is_some() {
                assert_eq!(ours["matcher"], theirs["matcher"], "{host}: matcher");
            }
            let handler = ours.pointer("/hooks/0").unwrap_or(ours);
            assert_eq!(
                handler_line(handler).unwrap(),
                hook_invocation(&target).unwrap().line(),
                "{host}: command"
            );
            assert_eq!(
                handler.get("args").is_some(),
                target.exec_form,
                "{host}: args"
            );

            // Fail-closed fields: only where the host's official doc names one.
            let mut extra: Vec<&String> = handler
                .as_object()
                .unwrap()
                .keys()
                .filter(|k| !["type", "command", "args", "matcher"].contains(&k.as_str()))
                .collect();
            if let Some((key, value)) = &target.fail_closed {
                assert_eq!(&handler[*key], value, "{host}: fail-closed value");
                assert!(f.to_string().contains(key), "{host}: fixture lacks {key}");
                extra.retain(|k| k.as_str() != *key);
            }
            assert!(extra.is_empty(), "{host}: undocumented fields {extra:?}");
        }
    }

    #[test]
    fn hook_rejects_unlanded_hosts_naming_landed_ones() {
        let home = tempfile::tempdir().unwrap();
        for host in ["copilot-cli", "vscode", "gemini", "antigravity", "zed"] {
            let err = install_hook(host, home.path()).unwrap_err();
            for landed in HOOK_HOSTS {
                assert!(err.contains(landed), "{host}: error omits {landed}: {err}");
            }
        }
        assert_eq!(fs::read_dir(home.path()).unwrap().count(), 0);
    }

    #[test]
    fn codex_hook_report_names_slash_hooks() {
        let home = tempfile::tempdir().unwrap();
        assert!(install_hook("codex", home.path())
            .unwrap()
            .contains("/hooks"));
        assert!(!install_hook("claude-code", home.path())
            .unwrap()
            .contains("/hooks"));
    }

    #[test]
    fn claude_hook_merge_is_idempotent_and_keeps_unrelated_entries() {
        let home = tempfile::tempdir().unwrap();
        let path = home.path().join(".claude/settings.json");
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        let other_pre =
            json!({"matcher": "Edit", "hooks": [{"type": "command", "command": "lint.sh"}]});
        let post =
            json!([{"matcher": "Bash", "hooks": [{"type": "command", "command": "log.sh"}]}]);
        fs::write(
            &path,
            serde_json::to_string(&json!({
                "theme": "dark",
                "hooks": {"PreToolUse": [other_pre.clone()], "PostToolUse": post.clone()}
            }))
            .unwrap(),
        )
        .unwrap();

        install_hook("claude-code", home.path()).unwrap();
        let first = fs::read_to_string(&path).unwrap();
        install_hook("claude-code", home.path()).unwrap();
        assert_eq!(
            fs::read_to_string(&path).unwrap(),
            first,
            "second run changed bytes"
        );

        let cfg = read_json(&path);
        assert_eq!(cfg["theme"], json!("dark"));
        assert_eq!(cfg["hooks"]["PostToolUse"], post);
        let pre = cfg["hooks"]["PreToolUse"].as_array().unwrap();
        assert_eq!(pre.len(), 2);
        assert_eq!(pre[0], other_pre);
        let (command, mut args) = tilth_command_and_args();
        args.retain(|a| a != "--mcp");
        let want_args: Vec<String> = args
            .into_iter()
            .chain(["hook".into(), "--host".into(), "claude".into()])
            .collect();
        assert_eq!(
            pre[1],
            json!({"matcher": "Bash", "hooks": [{"type": "command", "command": command, "args": want_args, "onFailure": "block"}]})
        );
    }

    #[test]
    fn stale_tilth_hook_entry_is_replaced_not_duplicated() {
        let home = tempfile::tempdir().unwrap();
        let path = home.path().join(".claude/settings.json");
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        let stale = json!({"matcher": "Bash", "hooks": [{"type": "command", "command": "/old/place/tilth hook --host claude"}]});
        let mine = json!({"matcher": "Bash", "hooks": [{"type": "command", "command": "/usr/bin/notatilth hook --host claude"}]});
        fs::write(
            &path,
            serde_json::to_string(&json!({"hooks": {"PreToolUse": [stale, mine.clone()]}}))
                .unwrap(),
        )
        .unwrap();
        install_hook("claude-code", home.path()).unwrap();
        let pre = read_json(&path)["hooks"]["PreToolUse"].clone();
        let pre = pre.as_array().unwrap();
        assert_eq!(pre.len(), 2);
        assert!(!pre[0].to_string().contains("/old/place"));
        assert_eq!(pre[1], mine);
    }

    #[test]
    fn cursor_merge_keeps_existing_keys() {
        let home = tempfile::tempdir().unwrap();
        let path = home.path().join(".cursor/hooks.json");
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        let other = json!({"command": "./scan.sh", "timeout": 30});
        fs::write(
            &path,
            serde_json::to_string(&json!({"version": 1, "hooks": {"beforeShellExecution": [other.clone()], "stop": [{"command": "x"}]}})).unwrap(),
        )
        .unwrap();
        install_hook("cursor", home.path()).unwrap();
        install_hook("cursor", home.path()).unwrap();
        let cfg = read_json(&path);
        assert_eq!(cfg["version"], json!(1));
        assert_eq!(cfg["hooks"]["stop"], json!([{"command": "x"}]));
        let arr = cfg["hooks"]["beforeShellExecution"].as_array().unwrap();
        assert_eq!(arr.len(), 2);
        assert_eq!(arr[0], other);
        assert!(arr[1]["command"]
            .as_str()
            .unwrap()
            .ends_with(" hook --host cursor"));
    }

    #[test]
    fn non_object_hook_config_is_an_error_not_a_clobber() {
        let home = tempfile::tempdir().unwrap();
        let path = home.path().join(".claude/settings.json");
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(&path, "[1]").unwrap();
        assert!(install_hook("claude-code", home.path()).is_err());
        assert_eq!(fs::read_to_string(&path).unwrap(), "[1]");
    }

    fn seed(home: &Path, rel: &str, content: &str) -> PathBuf {
        let path = home.join(rel);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(&path, content).unwrap();
        path
    }

    /// Rewrite every handler to the shell-form `line`, dropping any `args`.
    fn set_stale_command(v: &mut Value, line: &str) {
        match v {
            Value::Object(m) => {
                if m.get("command").is_some_and(Value::is_string) {
                    m.insert("command".into(), json!(line));
                    m.remove("args");
                }
                m.values_mut().for_each(|x| set_stale_command(x, line));
            }
            Value::Array(a) => a.iter_mut().for_each(|x| set_stale_command(x, line)),
            _ => {}
        }
    }

    fn all_commands(v: &Value) -> Vec<String> {
        fn walk(v: &Value, out: &mut Vec<String>) {
            match v {
                Value::Object(m) => {
                    if let Some(line) = handler_line(v) {
                        out.push(line);
                    }
                    for x in m.values() {
                        walk(x, out);
                    }
                }
                Value::Array(a) => a.iter().for_each(|x| walk(x, out)),
                _ => {}
            }
        }
        let mut out = Vec::new();
        walk(v, &mut out);
        out
    }

    fn tilth_count(v: &Value) -> usize {
        all_commands(v)
            .iter()
            .filter(|c| c.contains(" hook --host "))
            .count()
    }

    #[test]
    fn press_install_is_byte_idempotent_for_every_landed_host() {
        for host in HOOK_HOSTS {
            let home = tempfile::tempdir().unwrap();
            let path = hook_target(host, home.path()).unwrap().path;
            install_hook(host, home.path()).unwrap();
            let first = fs::read(&path).unwrap();
            install_hook(host, home.path()).unwrap();
            assert_eq!(first, fs::read(&path).unwrap(), "{host}");
            assert_eq!(tilth_count(&read_json(&path)), 1, "{host}");
        }
    }

    #[test]
    fn press_stale_entry_at_other_binary_path_is_replaced_for_every_host() {
        for host in HOOK_HOSTS {
            let home = tempfile::tempdir().unwrap();
            let target = hook_target(host, home.path()).unwrap();
            install_hook(host, home.path()).unwrap();
            let current = hook_invocation(&target).unwrap();
            let mut stale = read_json(&target.path);
            set_stale_command(
                &mut stale,
                &format!("/old/bin/tilth hook --host {}", target.family),
            );
            fs::write(&target.path, stale.to_string()).unwrap();
            install_hook(host, home.path()).unwrap();
            let cfg = read_json(&target.path);
            assert_eq!(tilth_count(&cfg), 1, "{host}");
            assert_eq!(all_commands(&cfg), vec![current.line()], "{host}");
        }
    }

    #[test]
    fn press_stale_tilth_hook_in_shared_group_keeps_user_sibling() {
        let home = tempfile::tempdir().unwrap();
        let path = seed(
            home.path(),
            ".claude/settings.json",
            r#"{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[
                {"type":"command","command":"/usr/local/bin/audit-log"},
                {"type":"command","command":"/old/bin/tilth hook --host claude"}]}]}}"#,
        );
        install_hook("claude-code", home.path()).unwrap();
        let cfg = read_json(&path);
        let cmds = all_commands(&cfg);
        assert!(
            cmds.iter().any(|c| c == "/usr/local/bin/audit-log"),
            "user's sibling hook was deleted: {cmds:?}"
        );
        assert_eq!(tilth_count(&cfg), 1);
    }

    #[test]
    fn press_stale_tilth_hook_in_bash_edit_group_keeps_matcher_and_siblings() {
        let home = tempfile::tempdir().unwrap();
        let path = seed(
            home.path(),
            ".claude/settings.json",
            r#"{"hooks":{"PreToolUse":[{"matcher":"Bash|Edit","hooks":[
                {"type":"command","command":"/old/bin/tilth hook --host claude"},
                {"type":"command","command":"/usr/local/bin/fmt-check"}]}]}}"#,
        );
        install_hook("claude-code", home.path()).unwrap();
        let cfg = read_json(&path);
        let cmds = all_commands(&cfg);
        assert!(
            cmds.iter().any(|c| c == "/usr/local/bin/fmt-check"),
            "user's Edit-matcher sibling hook was deleted: {cmds:?}"
        );
        assert_eq!(tilth_count(&cfg), 1);
    }

    #[test]
    fn press_two_stale_entries_collapse_to_one_and_keep_others() {
        let home = tempfile::tempdir().unwrap();
        let path = seed(
            home.path(),
            ".cursor/hooks.json",
            r#"{"version":2,"hooks":{"beforeShellExecution":[
                {"command":"/a/tilth hook --host cursor"},
                {"command":"./mine.sh"},
                {"command":"/b/tilth hook --host cursor"}],
              "afterFileEdit":[{"command":"./fmt.sh"}]}}"#,
        );
        install_hook("cursor", home.path()).unwrap();
        let cfg = read_json(&path);
        assert_eq!(cfg["version"], 2);
        assert_eq!(tilth_count(&cfg), 1);
        assert_eq!(
            cfg["hooks"]["beforeShellExecution"]
                .as_array()
                .unwrap()
                .len(),
            2
        );
        assert_eq!(cfg["hooks"]["afterFileEdit"][0]["command"], "./fmt.sh");
        assert!(all_commands(&cfg).contains(&"./mine.sh".to_string()));
    }

    #[test]
    fn press_windsurf_keeps_other_events() {
        let home = tempfile::tempdir().unwrap();
        let path = seed(
            home.path(),
            ".codeium/windsurf/hooks.json",
            r#"{"hooks":{"post_run_command":[{"command":"./post.sh"}]}}"#,
        );
        install_hook("windsurf", home.path()).unwrap();
        let cfg = read_json(&path);
        assert_eq!(cfg["hooks"]["post_run_command"][0]["command"], "./post.sh");
        assert_eq!(tilth_count(&cfg), 1);
    }

    #[test]
    fn press_malformed_shapes_error_without_touching_the_file() {
        let bad = [
            r#"{"hooks":null}"#,
            r#"{"hooks":[]}"#,
            r#"{"hooks":"x"}"#,
            r#"{"hooks":{"PreToolUse":{}}}"#,
            r#"{"hooks":{"PreToolUse":null}}"#,
            "",
            "\u{feff}{}",
            "{ // jsonc\n}",
        ];
        for content in bad {
            let home = tempfile::tempdir().unwrap();
            let path = seed(home.path(), ".claude/settings.json", content);
            let err = install_hook("claude-code", home.path());
            assert!(err.is_err(), "accepted {content:?}");
            assert_eq!(fs::read_to_string(&path).unwrap(), content);
        }
    }

    #[test]
    fn press_crush_jsonc_error_names_the_file() {
        let home = tempfile::tempdir().unwrap();
        seed(home.path(), ".config/crush/crush.json", "{ // c\n}");
        let err = install_hook("crush", home.path()).unwrap_err();
        assert!(err.contains("crush.json"), "{err}");
    }

    #[test]
    fn press_user_group_with_other_hooks_gets_separate_tilth_group() {
        let home = tempfile::tempdir().unwrap();
        let path = seed(
            home.path(),
            ".claude/settings.json",
            r#"{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"./mine.sh"}]}]}}"#,
        );
        install_hook("claude-code", home.path()).unwrap();
        install_hook("claude-code", home.path()).unwrap();
        let cfg = read_json(&path);
        assert_eq!(cfg["hooks"]["PreToolUse"].as_array().unwrap().len(), 2);
        assert_eq!(
            cfg["hooks"]["PreToolUse"][0]["hooks"][0]["command"],
            "./mine.sh"
        );
    }

    /// Grouped hosts as `(host, relative config path, hook target)` with an exact matcher.
    fn grouped_hosts(home: &Path) -> Vec<(&'static str, HookTarget)> {
        let hosts: Vec<_> = HOOK_HOSTS
            .iter()
            .filter_map(|h| {
                let t = hook_target(h, home).unwrap();
                (t.nested && t.matcher.is_some()).then_some((*h, t))
            })
            .collect();
        assert!(hosts.len() >= 4, "expected claude/codex/droid/augment");
        hosts
    }

    fn nested_config(target: &HookTarget, groups: Value) -> String {
        let mut events = serde_json::Map::new();
        events.insert(target.event.into(), groups);
        let doc = if target.wrap {
            json!({ "hooks": events })
        } else {
            Value::Object(events)
        };
        doc.to_string()
    }

    fn nested_groups<'a>(cfg: &'a Value, target: &HookTarget) -> &'a Vec<Value> {
        let root = if target.wrap { &cfg["hooks"] } else { cfg };
        root[target.event].as_array().unwrap()
    }

    fn tilth_handlers(groups: &[Value]) -> usize {
        groups
            .iter()
            .flat_map(|g| g["hooks"].as_array().into_iter().flatten())
            .filter(|h| handler_line(h).is_some_and(|c| c.contains(" hook --host ")))
            .count()
    }

    fn run_twice_identical(host: &str, home: &Path, path: &Path) -> Value {
        install_hook(host, home).unwrap();
        let first = fs::read_to_string(path).unwrap();
        install_hook(host, home).unwrap();
        assert_eq!(
            fs::read_to_string(path).unwrap(),
            first,
            "{host}: not idempotent"
        );
        read_json(path)
    }

    #[test]
    fn press2_stale_tilth_first_or_last_in_shared_group_keeps_user_hooks() {
        for order in ["first", "last"] {
            let probe = tempfile::tempdir().unwrap();
            for (host, _) in grouped_hosts(probe.path()) {
                let home = tempfile::tempdir().unwrap();
                let t = hook_target(host, home.path()).unwrap();
                let stale = json!({"type": "command", "command": format!("/old/bin/tilth hook --host {}", t.family)});
                let mine = json!({"type": "command", "command": "./mine.sh"});
                let hooks = if order == "first" {
                    json!([stale, mine])
                } else {
                    json!([mine, stale])
                };
                let path = seed(
                    home.path(),
                    t.path.strip_prefix(home.path()).unwrap().to_str().unwrap(),
                    &nested_config(&t, json!([{"matcher": t.matcher, "hooks": hooks}])),
                );
                let cfg = run_twice_identical(host, home.path(), &path);
                let groups = nested_groups(&cfg, &t);
                assert_eq!(groups.len(), 1, "{host}/{order}");
                let hs = groups[0]["hooks"].as_array().unwrap();
                assert_eq!(hs.len(), 2, "{host}/{order}");
                assert!(hs.contains(&mine), "{host}/{order}: user hook lost");
                assert!(hs
                    .iter()
                    .any(|h| handler_line(h) == Some(hook_invocation(&t).unwrap().line())));
                assert!(!hs.contains(&stale), "{host}/{order}: stale kept");
                let pos = hs.iter().position(|h| h == &mine).unwrap();
                assert_eq!(
                    pos,
                    usize::from(order == "first"),
                    "{host}/{order}: order moved"
                );
            }
        }
    }

    #[test]
    fn press2_stale_tilth_in_foreign_matcher_group_moves_to_exact_group() {
        let probe = tempfile::tempdir().unwrap();
        for (host, _) in grouped_hosts(probe.path()) {
            let home = tempfile::tempdir().unwrap();
            let t = hook_target(host, home.path()).unwrap();
            let stale = json!({"type": "command", "command": format!("/old/tilth hook --host {}", t.family)});
            let lint = json!({"type": "command", "command": "lint.sh"});
            let mine = json!({"type": "command", "command": "./mine.sh"});
            let path = seed(
                home.path(),
                t.path.strip_prefix(home.path()).unwrap().to_str().unwrap(),
                &nested_config(
                    &t,
                    json!([
                        {"matcher": "Bash|Edit", "hooks": [lint, stale]},
                        {"matcher": t.matcher, "hooks": [mine]}
                    ]),
                ),
            );
            let cfg = run_twice_identical(host, home.path(), &path);
            let groups = nested_groups(&cfg, &t);
            assert_eq!(tilth_handlers(groups), 1, "{host}");
            assert_eq!(groups[0]["matcher"], "Bash|Edit");
            assert_eq!(groups[0]["hooks"], json!([lint]), "{host}: foreign group");
            let exact: Vec<_> = groups
                .iter()
                .filter(|g| g["matcher"] == json!(t.matcher))
                .collect();
            let all: Vec<&Value> = exact
                .iter()
                .flat_map(|g| g["hooks"].as_array().unwrap())
                .collect();
            assert!(all.contains(&&mine), "{host}: user hook lost");
        }
    }

    #[test]
    fn press2_tilth_handler_with_extra_keys_is_replaced_not_merged() {
        let probe = tempfile::tempdir().unwrap();
        for (host, _) in grouped_hosts(probe.path()) {
            let home = tempfile::tempdir().unwrap();
            let t = hook_target(host, home.path()).unwrap();
            let stale = json!({"type": "command", "command": format!("/old/tilth hook --host {}", t.family), "timeout": 5, "statusMessage": "guarding"});
            let path = seed(
                home.path(),
                t.path.strip_prefix(home.path()).unwrap().to_str().unwrap(),
                &nested_config(&t, json!([{"matcher": t.matcher, "hooks": [stale]}])),
            );
            let cfg = run_twice_identical(host, home.path(), &path);
            let groups = nested_groups(&cfg, &t);
            assert_eq!(
                groups[0]["hooks"],
                json!([hook_entry(&t, &hook_invocation(&t).unwrap())["hooks"][0].clone()]),
                "{host}"
            );
        }
    }

    #[test]
    fn press2_group_with_non_array_hooks_is_left_untouched() {
        let probe = tempfile::tempdir().unwrap();
        for (host, _) in grouped_hosts(probe.path()) {
            let home = tempfile::tempdir().unwrap();
            let t = hook_target(host, home.path()).unwrap();
            let odd = json!({"matcher": t.matcher, "hooks": "not-an-array", "extra": 1});
            let path = seed(
                home.path(),
                t.path.strip_prefix(home.path()).unwrap().to_str().unwrap(),
                &nested_config(&t, json!([odd])),
            );
            let cfg = run_twice_identical(host, home.path(), &path);
            let groups = nested_groups(&cfg, &t);
            assert_eq!(groups[0], odd, "{host}: odd group clobbered");
            assert_eq!(tilth_handlers(groups), 1, "{host}");
        }
    }

    #[test]
    fn press2_handlers_without_command_survive() {
        let probe = tempfile::tempdir().unwrap();
        for (host, _) in grouped_hosts(probe.path()) {
            let home = tempfile::tempdir().unwrap();
            let t = hook_target(host, home.path()).unwrap();
            let bare = json!({"type": "prompt", "prompt": "hi"});
            let path = seed(
                home.path(),
                t.path.strip_prefix(home.path()).unwrap().to_str().unwrap(),
                &nested_config(&t, json!([{"matcher": t.matcher, "hooks": [bare]}])),
            );
            let cfg = run_twice_identical(host, home.path(), &path);
            let groups = nested_groups(&cfg, &t);
            let all: Vec<&Value> = groups
                .iter()
                .flat_map(|g| g["hooks"].as_array().unwrap())
                .collect();
            assert!(all.contains(&&bare), "{host}: command-less handler lost");
            assert_eq!(tilth_handlers(groups), 1, "{host}");
        }
    }

    #[test]
    fn press2_user_hook_appended_to_tilth_group_survives_reinstall() {
        let probe = tempfile::tempdir().unwrap();
        for (host, _) in grouped_hosts(probe.path()) {
            let home = tempfile::tempdir().unwrap();
            let t = hook_target(host, home.path()).unwrap();
            install_hook(host, home.path()).unwrap();
            let mut cfg = read_json(&t.path);
            let mine = json!({"type": "command", "command": "./mine.sh"});
            let root = if t.wrap { &mut cfg["hooks"] } else { &mut cfg };
            let n = root[t.event].as_array().unwrap().len();
            assert_eq!(n, 1);
            root[t.event][0]["hooks"]
                .as_array_mut()
                .unwrap()
                .push(mine.clone());
            fs::write(&t.path, serde_json::to_string_pretty(&cfg).unwrap()).unwrap();
            let cfg = run_twice_identical(host, home.path(), &t.path);
            let groups = nested_groups(&cfg, &t);
            assert_eq!(groups.len(), 1, "{host}");
            assert_eq!(groups[0]["hooks"].as_array().unwrap().len(), 2, "{host}");
            assert!(groups[0]["hooks"].as_array().unwrap().contains(&mine));
            assert_eq!(tilth_handlers(groups), 1, "{host}");
        }
    }

    #[test]
    fn droid_merges_into_settings_json_when_hooks_json_is_absent() {
        let home = tempfile::tempdir().unwrap();
        let user =
            json!({"matcher": "Execute", "hooks": [{"type": "command", "command": "/u/audit"}]});
        let path = seed(
            home.path(),
            ".factory/settings.json",
            &json!({"model": "x", "hooks": {"PreToolUse": [user.clone()]}}).to_string(),
        );
        install_hook("droid", home.path()).unwrap();
        install_hook("droid", home.path()).unwrap();
        assert!(!home.path().join(".factory/hooks.json").exists());
        let cfg = read_json(&path);
        assert_eq!(cfg["model"], "x");
        let cmds = all_commands(&cfg);
        assert!(cmds.contains(&"/u/audit".to_string()), "{cmds:?}");
        assert_eq!(tilth_count(&cfg), 1);
    }

    #[test]
    fn droid_creates_settings_json_hooks_key_on_a_fresh_home() {
        let home = tempfile::tempdir().unwrap();
        install_hook("droid", home.path()).unwrap();
        assert!(!home.path().join(".factory/hooks.json").exists());
        let cfg = read_json(&home.path().join(".factory/settings.json"));
        assert_eq!(tilth_count(&cfg), 1);
        assert_eq!(cfg["hooks"]["PreToolUse"][0]["matcher"], "Execute");
    }

    #[test]
    fn droid_uses_unwrapped_hooks_json_when_present() {
        let home = tempfile::tempdir().unwrap();
        let path = seed(home.path(), ".factory/hooks.json", "{}");
        install_hook("droid", home.path()).unwrap();
        assert!(!home.path().join(".factory/settings.json").exists());
        let cfg = read_json(&path);
        assert!(cfg.get("hooks").is_none());
        assert_eq!(cfg["PreToolUse"][0]["matcher"], "Execute");
        assert_eq!(tilth_count(&cfg), 1);
    }

    /// Per-host fail-closed field, each named in that host's official doc
    /// (claude: raw/claude.md `onFailure`). Cursor gets none: raw/cursor.md counts a silent
    /// exit-0 allow as a failure under `failClosed`, so its guard fails open if the binary is missing.
    #[test]
    fn only_documented_hosts_get_a_fail_closed_field() {
        for host in HOOK_HOSTS {
            let home = tempfile::tempdir().unwrap();
            let t = hook_target(host, home.path()).unwrap();
            install_hook(host, home.path()).unwrap();
            let written = read_json(&t.path);
            let ours = first_event_entry(&written, t.event).unwrap();
            let handler = ours.pointer("/hooks/0").unwrap_or(ours);
            let want = match *host {
                "claude-code" => Some(("onFailure", json!("block"))),
                _ => None,
            };
            let mut keys: Vec<&str> = handler
                .as_object()
                .unwrap()
                .keys()
                .map(String::as_str)
                .filter(|k| !["type", "command", "args", "matcher"].contains(k))
                .collect();
            if let Some((key, value)) = want {
                assert_eq!(handler[key], value, "{host}");
                keys.retain(|k| *k != key);
            }
            assert!(keys.is_empty(), "{host}: {keys:?}");
        }
    }

    #[test]
    fn cursor_entry_has_no_fail_closed_key() {
        let home = tempfile::tempdir().unwrap();
        install_hook("cursor", home.path()).unwrap();
        let cfg = read_json(&home.path().join(".cursor/hooks.json"));
        let entry = &cfg["hooks"]["beforeShellExecution"][0];
        assert!(entry.get("failClosed").is_none(), "{entry}");
    }

    #[test]
    fn shell_quote_leaves_plain_paths_bare() {
        assert_eq!(shell_quote("/usr/bin/tilth").unwrap(), "/usr/bin/tilth");
    }

    #[test]
    fn shell_quote_single_quotes_paths_with_spaces() {
        assert_eq!(shell_quote("/a b/tilth").unwrap(), "'/a b/tilth'");
    }

    #[test]
    fn shell_quote_refuses_expandable_characters() {
        for bad in [
            "/a'b", "/a\"b", "/a$HOME", "/a`x`", "/a%PATH%", "/a\nb", "/a\rb",
        ] {
            assert!(shell_quote(bad).is_err(), "{bad:?}");
        }
    }

    const WIN_BIN: &str = r"C:\Users\a b\tilth.exe";

    #[test]
    fn exec_form_keeps_spaces_and_backslashes_verbatim() {
        let inv = build_hook_invocation(true, false, "claude", WIN_BIN, &[]).unwrap();
        assert_eq!(inv.command, WIN_BIN);
        assert_eq!(
            inv.args,
            Some(vec!["hook".into(), "--host".into(), "claude".into()])
        );
    }

    #[test]
    fn exec_form_only_for_native_absolute_binaries() {
        let a = |s: &[&str]| s.iter().map(ToString::to_string).collect::<Vec<_>>();
        let tilth = a(&["hook"]);
        assert!(exec_form_applies(true, "/usr/bin/tilth", &tilth, false));
        assert!(exec_form_applies(true, WIN_BIN, &tilth, true));
        assert!(!exec_form_applies(false, "/usr/bin/tilth", &tilth, false));
        for windows in [false, true] {
            assert!(!exec_form_applies(true, "npx", &a(&["tilth"]), windows));
            assert!(!exec_form_applies(true, "tilth", &tilth, windows));
        }
        assert!(!exec_form_applies(true, "C:/x/tilth.cmd", &tilth, true));
        assert!(!exec_form_applies(true, r"C:\x\tilth.BAT", &tilth, true));
        assert!(!exec_form_applies(true, "C:/x/tilth.ps1", &tilth, true));
        assert!(!exec_form_applies(
            true,
            "/usr/bin/npx",
            &a(&["tilth"]),
            false
        ));
        assert!(!exec_form_applies(
            true,
            "/usr/bin/node",
            &a(&["t.js"]),
            false
        ));
    }

    #[test]
    fn npx_hook_is_shell_form_and_replaces_stale_exec_entry() {
        let args = vec!["tilth".to_string()];
        let ok = exec_form_applies(true, "npx", &args, true);
        let inv = build_hook_invocation(ok, true, "claude", "npx", &args).unwrap();
        assert_eq!(inv.command, "npx tilth hook --host claude");
        assert!(inv.args.is_none());
        let stale_exec = "npx tilth hook --host claude";
        assert!(is_tilth_hook_command(stale_exec, &inv.own_prefix));
        let native = build_hook_invocation(true, false, "claude", "/a/tilth", &[]).unwrap();
        assert!(is_tilth_hook_command(
            &native.line(),
            "/a/tilth hook --host "
        ));
    }

    #[test]
    fn claude_code_install_writes_exec_form() {
        let home = tempfile::tempdir().unwrap();
        install_hook("claude-code", home.path()).unwrap();
        let cfg = read_json(&home.path().join(".claude/settings.json"));
        let handler = &cfg["hooks"]["PreToolUse"][0]["hooks"][0];
        assert!(!handler["command"].as_str().unwrap().contains(" hook "));
        let args = handler["args"].as_array().unwrap();
        assert_eq!(
            args[args.len() - 3..],
            [json!("hook"), json!("--host"), json!("claude")]
        );
    }

    #[test]
    fn posix_shell_form_single_quotes_and_forward_slashes() {
        let inv = build_hook_invocation(false, false, "codex", "/a b/tilth", &[]).unwrap();
        assert_eq!(inv.command, "'/a b/tilth' hook --host codex");
        assert!(inv.args.is_none());
        let inv = build_hook_invocation(false, false, "codex", WIN_BIN, &[]).unwrap();
        assert_eq!(inv.command, "'C:/Users/a b/tilth.exe' hook --host codex");
        let inv = build_hook_invocation(false, false, "codex", r"C:\tilth\tilth.exe", &[]).unwrap();
        assert_eq!(inv.command, "C:/tilth/tilth.exe hook --host codex");
    }

    #[test]
    fn powershell_shell_form_uses_call_operator_for_quoted_paths() {
        let inv = build_hook_invocation(false, true, "windsurf", WIN_BIN, &[]).unwrap();
        assert_eq!(
            inv.command,
            "& 'C:/Users/a b/tilth.exe' hook --host windsurf"
        );
        let inv = build_hook_invocation(false, true, "windsurf", r"C:\t\tilth.exe", &[]).unwrap();
        assert_eq!(inv.command, "C:/t/tilth.exe hook --host windsurf");
    }

    #[test]
    fn shell_form_refuses_unsafe_binary_paths() {
        for bad in [
            "/a'b/tilth",
            "/a\"b/tilth",
            "/a$b/tilth",
            "/a`b/tilth",
            "/a%b/tilth",
            "/a\nb/tilth",
        ] {
            assert!(
                build_hook_invocation(false, false, "codex", bad, &[]).is_err(),
                "{bad:?}"
            );
        }
    }

    #[test]
    fn stale_shell_form_entry_is_replaced_by_exec_form() {
        let home = tempfile::tempdir().unwrap();
        let path = seed(
            home.path(),
            ".claude/settings.json",
            r#"{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[
                {"type":"command","command":"\"/old dir/tilth\" hook --host claude"}]}]}}"#,
        );
        install_hook("claude-code", home.path()).unwrap();
        let cfg = run_twice_identical("claude-code", home.path(), &path);
        assert_eq!(tilth_count(&cfg), 1);
        let handler = &cfg["hooks"]["PreToolUse"][0]["hooks"][0];
        assert!(handler["args"].is_array(), "{handler}");
    }

    #[test]
    fn stale_exec_form_entry_is_replaced() {
        let home = tempfile::tempdir().unwrap();
        let path = seed(
            home.path(),
            ".claude/settings.json",
            r#"{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[
                {"type":"command","command":"/old dir/tilth","args":["hook","--host","claude"]},
                {"type":"command","command":"node","args":["audit.js"]}]}]}}"#,
        );
        let cfg = run_twice_identical("claude-code", home.path(), &path);
        assert_eq!(tilth_count(&cfg), 1);
        let cmds = all_commands(&cfg);
        assert!(cmds.contains(&"node audit.js".to_string()), "{cmds:?}");
        assert!(!cmds.iter().any(|c| c.contains("/old dir")), "{cmds:?}");
    }

    #[test]
    fn quoted_tilth_path_is_still_recognised_as_stale() {
        let own = "/elsewhere/tilth hook --host ";
        for cmd in [
            "\"/a b/tilth\" hook --host claude",
            "'/a b/tilth' hook --host claude",
            "& 'C:/a b/tilth.exe' hook --host windsurf",
            r"C:\a\tilth.exe hook --host windsurf",
        ] {
            assert!(is_tilth_hook_command(cmd, own), "{cmd}");
        }
        assert!(!is_tilth_hook_command(
            "\"/a b/other\" hook --host claude",
            own
        ));
    }

    #[test]
    fn bad_hook_file_fails_before_the_mcp_config_is_written() {
        let home = tempfile::tempdir().unwrap();
        let path = seed(home.path(), ".claude/settings.json", "{ not json");
        let mut wrote_mcp = false;
        let err = install_with_hook("claude-code", home.path(), |_| {
            wrote_mcp = true;
            Ok(())
        })
        .unwrap_err();
        assert!(err.contains("settings.json"), "{err}");
        assert!(
            !wrote_mcp,
            "MCP config written before the hook file was validated"
        );
        assert_eq!(fs::read_to_string(&path).unwrap(), "{ not json");
    }
}
