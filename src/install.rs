use std::fs;
use std::path::PathBuf;

use serde_json::{json, Value};

// Supported MCP hosts and their config locations.
//
// Paths verified from official docs (2025):
//   claude-code:    ~/.claude.json                            (user scope)
//   cursor:         ~/.cursor/mcp.json                        (global)
//   windsurf:       ~/.codeium/windsurf/mcp_config.json       (global)
//   vscode:         .vscode/mcp.json                          (project scope)
//   claude-desktop: ~/Library/Application Support/Claude/...  (global)
//   opencode:       ~/.opencode.json                          (user scope)
//   gemini:         ~/.gemini/settings.json                   (user scope)
//   codex:          ~/.codex/config.toml                      (user scope, TOML)
const SUPPORTED_HOSTS: &[&str] = &[
    "claude-code",
    "cursor",
    "windsurf",
    "vscode",
    "claude-desktop",
    "opencode",
    "gemini",
    "codex",
];

/// The tilth server entry as JSON, for hosts that use JSON config.
fn tilth_server_entry(edit: bool) -> Value {
    let (command, args) = tilth_command_and_args(edit);
    json!({
        "command": command,
        "args": args
    })
}

/// Write MCP config for the given host, preserving existing config.
pub fn run(host: &str, edit: bool) -> Result<(), String> {
    let host_info = resolve_host(host)?;

    if let Some(parent) = host_info.path.parent() {
        fs::create_dir_all(parent)
            .map_err(|e| format!("failed to create {}: {e}", parent.display()))?;
    }

    if host == "codex" {
        write_codex_toml(&host_info, edit)?;
    } else {
        write_json_config(&host_info, edit)?;
    }

    if edit {
        eprintln!("✓ tilth (edit mode) added to {}", host_info.path.display());
    } else {
        eprintln!("✓ tilth added to {}", host_info.path.display());
    }
    if let Some(note) = host_info.note {
        eprintln!("  {note}");
    }
    Ok(())
}

fn write_json_config(host_info: &HostInfo, edit: bool) -> Result<(), String> {
    let mut config: Value = if host_info.path.exists() {
        let raw = fs::read_to_string(&host_info.path)
            .map_err(|e| format!("failed to read {}: {e}", host_info.path.display()))?;
        serde_json::from_str(&raw)
            .map_err(|e| format!("invalid JSON in {}: {e}", host_info.path.display()))?
    } else {
        json!({})
    };

    let servers_key = host_info.servers_key;

    config
        .as_object_mut()
        .ok_or("config root is not a JSON object")?
        .entry(servers_key)
        .or_insert(json!({}))
        .as_object_mut()
        .ok_or_else(|| format!("{servers_key} is not a JSON object"))?
        .insert("tilth".into(), tilth_server_entry(edit));

    let out =
        serde_json::to_string_pretty(&config).expect("serde_json::Value is always serializable");
    fs::write(&host_info.path, &out)
        .map_err(|e| format!("failed to write {}: {e}", host_info.path.display()))?;
    Ok(())
}

/// Codex CLI uses TOML config at ~/.codex/config.toml.
/// Appends/replaces the `[mcp_servers.tilth]` section.
fn write_codex_toml(host_info: &HostInfo, edit: bool) -> Result<(), String> {
    let (command, args) = tilth_command_and_args(edit);

    // Build the TOML section
    let args_toml: Vec<String> = args.iter().map(|a| format!("\"{a}\"")).collect();
    let section = format!(
        "[mcp_servers.tilth]\ncommand = \"{}\"\nargs = [{}]\n",
        command,
        args_toml.join(", ")
    );

    let existing = if host_info.path.exists() {
        fs::read_to_string(&host_info.path)
            .map_err(|e| format!("failed to read {}: {e}", host_info.path.display()))?
    } else {
        String::new()
    };

    // Remove existing [mcp_servers.tilth] section if present
    let output = if let Some(start) = existing.find("[mcp_servers.tilth]") {
        // Find end of section: next [header] or EOF
        let rest = &existing[start..];
        let end = rest[1..] // skip the opening '['
            .find("\n[")
            .map_or(existing.len(), |i| start + 1 + i + 1);
        format!("{}{}{}", &existing[..start], section, &existing[end..])
    } else {
        // Append with a blank line separator
        let sep = if existing.is_empty() || existing.ends_with('\n') {
            ""
        } else {
            "\n"
        };
        format!("{existing}{sep}\n{section}")
    };

    fs::write(&host_info.path, &output)
        .map_err(|e| format!("failed to write {}: {e}", host_info.path.display()))?;
    Ok(())
}

/// Returns (command, args) for the tilth MCP server entry.
fn tilth_command_and_args(edit: bool) -> (String, Vec<String>) {
    let mut mcp_args: Vec<String> = vec!["--mcp".into()];
    if edit {
        mcp_args.push("--edit".into());
    }

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

struct HostInfo {
    path: PathBuf,
    /// JSON key holding the servers map ("mcpServers" or "servers").
    servers_key: &'static str,
    /// Optional note printed after success.
    note: Option<&'static str>,
}

fn resolve_host(host: &str) -> Result<HostInfo, String> {
    let home = home_dir()?;

    match host {
        // Claude Code user scope: ~/.claude.json → mcpServers
        // Available in all projects without checking into source control.
        "claude-code" => Ok(HostInfo {
            path: home.join(".claude.json"),
            servers_key: "mcpServers",
            note: Some("User scope — available in all projects."),
        }),

        // Cursor global: ~/.cursor/mcp.json → mcpServers
        "cursor" => Ok(HostInfo {
            path: home.join(".cursor/mcp.json"),
            servers_key: "mcpServers",
            note: None,
        }),

        // Windsurf global: ~/.codeium/windsurf/mcp_config.json → mcpServers
        "windsurf" => Ok(HostInfo {
            path: home.join(".codeium/windsurf/mcp_config.json"),
            servers_key: "mcpServers",
            note: None,
        }),

        // VS Code project scope: .vscode/mcp.json → servers (NOT mcpServers)
        "vscode" => Ok(HostInfo {
            path: PathBuf::from(".vscode/mcp.json"),
            servers_key: "servers",
            note: Some("Project scope — run from your project root."),
        }),

        "claude-desktop" => Ok(HostInfo {
            path: claude_desktop_path()?,
            servers_key: "mcpServers",
            note: None,
        }),

        // OpenCode user scope: ~/.opencode.json → mcpServers
        // Verified from opencode source: internal/config/config.go (viper config name ".opencode")
        "opencode" => Ok(HostInfo {
            path: home.join(".opencode.json"),
            servers_key: "mcpServers",
            note: Some("User scope — available in all projects."),
        }),

        // Gemini CLI user scope: ~/.gemini/settings.json → mcpServers
        "gemini" => Ok(HostInfo {
            path: home.join(".gemini/settings.json"),
            servers_key: "mcpServers",
            note: Some("User scope — available in all projects."),
        }),

        // Codex CLI user scope: ~/.codex/config.toml → [mcp_servers.tilth] (TOML)
        "codex" => Ok(HostInfo {
            path: home.join(".codex/config.toml"),
            servers_key: "mcp_servers",
            note: Some("User scope — available in all projects."),
        }),

        _ => Err(format!(
            "unknown host: {host}. Supported: {}",
            SUPPORTED_HOSTS.join(", ")
        )),
    }
}

fn home_dir() -> Result<PathBuf, String> {
    #[cfg(target_os = "windows")]
    {
        std::env::var("USERPROFILE")
            .map(PathBuf::from)
            .map_err(|_| "USERPROFILE not set".into())
    }

    #[cfg(not(target_os = "windows"))]
    {
        std::env::var("HOME")
            .map(PathBuf::from)
            .map_err(|_| "HOME not set".into())
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
