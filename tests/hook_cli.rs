//! End-to-end tests for `tilth hook` and `tilth install <host> --hook`, run through the
//! real binary.

use std::collections::BTreeMap;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use serde_json::{json, Value};

const TILTH: &str = env!("CARGO_BIN_EXE_tilth");

const LANDED: [&str; 7] = [
    "claude-code",
    "codex",
    "droid",
    "crush",
    "augment",
    "cursor",
    "windsurf",
];

const SUPPORTED_HOSTS: [&str; 22] = [
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

struct Out {
    code: i32,
    stdout: String,
    stderr: String,
}

fn assert_not_real_home(home: &Path) {
    if let Some(real) = std::env::var_os("HOME") {
        assert_ne!(
            home,
            Path::new(&real),
            "test sandbox HOME equals the real home"
        );
    }
}

/// Every child runs with a cleared environment and a sandbox home, so no test can touch the
/// developer's real config files. `None` still gets a fresh sandbox.
fn run(args: &[&str], stdin: &str, home: Option<&Path>) -> Out {
    let scratch = tempfile::tempdir().unwrap();
    let home = home.unwrap_or_else(|| scratch.path());
    assert_not_real_home(home);
    let mut cmd = Command::new(TILTH);
    cmd.args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", home)
        .env("USERPROFILE", home)
        .env("XDG_CONFIG_HOME", home.join(".config"))
        .env("XDG_DATA_HOME", home.join(".local/share"))
        .env("APPDATA", home.join("AppData"))
        .current_dir(home);
    let mut child = cmd.spawn().unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(stdin.as_bytes())
        .unwrap();
    let out = child.wait_with_output().unwrap();
    Out {
        code: out.status.code().unwrap(),
        stdout: String::from_utf8_lossy(&out.stdout).into_owned(),
        stderr: String::from_utf8_lossy(&out.stderr).into_owned(),
    }
}

fn claude_event(command: &str) -> String {
    json!({"tool_name": "Bash", "tool_input": {"command": command}}).to_string()
}

fn corpus() -> Value {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("benchmark/hook_corpus.json");
    serde_json::from_str(&fs::read_to_string(path).unwrap()).unwrap()
}

#[test]
fn corpus_must_deny_rows_exit_2_naming_tilth_tools() {
    let corpus = corpus();
    let rows = corpus["must_deny"].as_array().unwrap();
    assert_ne!(rows.len(), 0);
    for row in rows {
        let command = row["command"].as_str().unwrap();
        let out = run(&["hook"], &claude_event(command), None);
        assert_eq!(out.code, 2, "{command}");
        assert_eq!(out.stdout, "", "{command}");
        assert!(out.stderr.contains("tilth_"), "{command}: {}", out.stderr);

        let native = run(&["hook", "--hint", "native"], &claude_event(command), None);
        assert_eq!(native.code, 2, "{command} native");
        assert_eq!(native.stdout, "", "{command} native");
        assert!(!native.stderr.is_empty(), "{command} native");
        assert!(
            !native.stderr.to_lowercase().contains("tilth"),
            "{command}: {}",
            native.stderr
        );
    }
}

#[test]
fn corpus_must_allow_rows_exit_0_silently() {
    let corpus = corpus();
    let rows = corpus["must_allow"].as_array().unwrap();
    assert_ne!(rows.len(), 0);
    for row in rows {
        let command = row["command"].as_str().unwrap();
        let out = run(&["hook"], &claude_event(command), None);
        assert_eq!(out.code, 0, "{command}: {}", out.stderr);
        assert_eq!(out.stdout, "", "{command}");
        assert_eq!(out.stderr, "", "{command}");
    }
}

#[test]
fn malformed_json_and_unknown_host_deny() {
    let bad = run(&["hook"], "{not json", None);
    assert_eq!(bad.code, 2);
    assert_eq!(bad.stdout, "");
    assert_ne!(bad.stderr, "");

    let bogus = run(&["hook", "--host", "bogus"], &claude_event("ls"), None);
    assert_eq!(bogus.code, 2);
    assert_eq!(bogus.stdout, "");
    assert!(bogus.stderr.contains("bogus"));
}

#[test]
fn non_shell_tool_event_is_allowed() {
    let event = json!({"tool_name": "Read", "tool_input": {"file_path": "/x"}}).to_string();
    let out = run(&["hook"], &event, None);
    assert_eq!(
        (out.code, out.stdout.as_str(), out.stderr.as_str()),
        (0, "", "")
    );
}

#[test]
fn cursor_and_windsurf_shapes() {
    let cursor = |c: &str| json!({"command": c, "cwd": "/p", "sandbox": false}).to_string();
    let deny = run(
        &["hook", "--host", "cursor"],
        &cursor("cat src/lib.rs"),
        None,
    );
    assert_eq!(deny.code, 2);
    assert_eq!(deny.stdout, "");
    assert!(deny.stderr.contains("tilth_"));
    let allow = run(&["hook", "--host", "cursor"], &cursor("cargo test"), None);
    assert_eq!(
        (allow.code, allow.stdout.as_str(), allow.stderr.as_str()),
        (0, "", "")
    );

    let windsurf = |c: &str| json!({"tool_info": {"command_line": c, "cwd": "/p"}}).to_string();
    let deny = run(
        &["hook", "--host", "windsurf"],
        &windsurf("cat src/lib.rs"),
        None,
    );
    assert_eq!(deny.code, 2);
    assert_eq!(deny.stdout, "");
    assert!(deny.stderr.contains("tilth_"));
    let allow = run(
        &["hook", "--host", "windsurf"],
        &windsurf("cargo test"),
        None,
    );
    assert_eq!(
        (allow.code, allow.stdout.as_str(), allow.stderr.as_str()),
        (0, "", "")
    );

    // A Claude-shaped event on a cursor hook has no top-level command: fail closed.
    let wrong = run(&["hook", "--host", "cursor"], &claude_event("ls"), None);
    assert_eq!(wrong.code, 2);
}

fn files_under(root: &Path) -> BTreeMap<PathBuf, Vec<u8>> {
    fn walk(dir: &Path, root: &Path, out: &mut BTreeMap<PathBuf, Vec<u8>>) {
        let Ok(entries) = fs::read_dir(dir) else {
            return;
        };
        for e in entries.flatten() {
            let p = e.path();
            if p.is_dir() {
                walk(&p, root, out);
            } else {
                out.insert(
                    p.strip_prefix(root).unwrap().to_path_buf(),
                    fs::read(&p).unwrap(),
                );
            }
        }
    }
    let mut out = BTreeMap::new();
    walk(root, root, &mut out);
    out
}

#[test]
fn install_claude_code_hook_twice_leaves_one_entry() {
    let home = tempfile::tempdir().unwrap();
    for _ in 0..2 {
        let out = run(&["install", "claude-code", "--hook"], "", Some(home.path()));
        assert_eq!(out.code, 0, "{}", out.stderr);
    }
    let settings: Value = serde_json::from_str(
        &fs::read_to_string(home.path().join(".claude/settings.json")).unwrap(),
    )
    .unwrap();
    let pre = settings["hooks"]["PreToolUse"].as_array().unwrap();
    assert_eq!(pre.len(), 1);
    assert_eq!(pre[0]["matcher"], "Bash");
    assert_eq!(pre[0]["hooks"][0]["type"], "command");
    assert_eq!(pre[0]["hooks"][0]["command"], TILTH);
    assert_eq!(
        pre[0]["hooks"][0]["args"],
        json!(["hook", "--host", "claude"])
    );
    // The MCP entry still lands in ~/.claude.json.
    assert!(home.path().join(".claude.json").exists());
}

#[test]
fn install_hook_codex_prints_trust_step() {
    let home = tempfile::tempdir().unwrap();
    let out = run(&["install", "codex", "--hook"], "", Some(home.path()));
    assert_eq!(out.code, 0, "{}", out.stderr);
    assert!(out.stderr.contains("/hooks"), "{}", out.stderr);
    assert!(home.path().join(".codex/hooks.json").exists());
}

#[test]
fn install_hook_refuses_unlanded_hosts_before_writing() {
    for host in [
        "copilot-cli",
        "vscode",
        "gemini",
        "antigravity",
        "zed",
        "cline",
    ] {
        let home = tempfile::tempdir().unwrap();
        let out = run(&["install", host, "--hook"], "", Some(home.path()));
        assert_eq!(out.code, 1, "{host}");
        for landed in LANDED {
            assert!(out.stderr.contains(landed), "{host}: {}", out.stderr);
        }
        assert!(files_under(home.path()).is_empty(), "{host} wrote files");
    }
}

#[test]
fn install_without_hook_flag_writes_no_hook_config() {
    for host in SUPPORTED_HOSTS {
        let plain = tempfile::tempdir().unwrap();
        let out = run(&["install", host], "", Some(plain.path()));
        let plain_files = files_under(plain.path());
        if out.code != 0 {
            assert!(plain_files.is_empty(), "{host}: failed install wrote files");
            continue;
        }
        assert!(!plain_files.is_empty(), "{host}: wrote nothing");
        for (path, bytes) in &plain_files {
            let text = String::from_utf8_lossy(bytes);
            assert!(
                !text.contains(" hook --host ") && !text.contains("PreToolUse"),
                "{host}: {} holds hook config",
                path.display()
            );
        }

        // Deterministic: a second fresh home gets the same bytes.
        let again = tempfile::tempdir().unwrap();
        let again_out = run(&["install", host], "", Some(again.path()));
        assert_eq!(again_out.code, 0, "{host}: {}", again_out.stderr);
        assert_eq!(files_under(again.path()), plain_files, "{host}");

        // --hook adds hook files only on landed hosts, and leaves MCP bytes alone
        // wherever the two writers target different files.
        let hooked = tempfile::tempdir().unwrap();
        let out = run(&["install", host, "--hook"], "", Some(hooked.path()));
        let hooked_files = files_under(hooked.path());
        if LANDED.contains(&host) {
            assert_eq!(out.code, 0, "{host}: {}", out.stderr);
            assert!(hooked_files.len() >= plain_files.len(), "{host}");
            let marks_hook = |b: &[u8]| {
                let t = String::from_utf8_lossy(b);
                t.contains("\"hook\"") || t.contains(" hook --host ")
            };
            let mut hook_only = 0;
            for (path, bytes) in &hooked_files {
                match plain_files.get(path) {
                    None => hook_only += 1,
                    Some(plain) if marks_hook(bytes) => {
                        assert!(!marks_hook(plain), "{host}: {}", path.display());
                    }
                    Some(plain) => assert_eq!(
                        plain,
                        bytes,
                        "{host}: --hook changed MCP file {}",
                        path.display()
                    ),
                }
            }
            assert!(
                hook_only > 0 || hooked_files.values().any(|b| marks_hook(b)),
                "{host}: no hook config written"
            );
        } else {
            assert_eq!(out.code, 1, "{host}");
            assert!(hooked_files.is_empty(), "{host}");
        }
    }
}

#[test]
fn press_hook_with_empty_stdin_fails_closed() {
    for host in ["claude", "cursor", "windsurf"] {
        let out = run(&["hook", "--host", host], "", None);
        assert_eq!(out.code, 2, "{host}: {}", out.stderr);
    }
}

#[test]
fn press_hook_with_bogus_hint_is_rejected() {
    let out = run(
        &["hook", "--hint", "bogus"],
        &claude_event("cat a.rs"),
        None,
    );
    assert_eq!(out.code, 2, "{}", out.stderr);
}

#[test]
fn press_install_hook_with_edit_flag_and_uppercase_host() {
    let home = tempfile::tempdir().unwrap();
    let out = run(
        &["install", "claude-code", "--hook", "--edit"],
        "",
        Some(home.path()),
    );
    assert_eq!(out.code, 0, "{}", out.stderr);
    assert!(home.path().join(".claude/settings.json").exists());
    let upper = tempfile::tempdir().unwrap();
    let out = run(
        &["install", "CLAUDE-CODE", "--hook"],
        "",
        Some(upper.path()),
    );
    assert_eq!(out.code, 1, "{}", out.stderr);
    assert!(files_under(upper.path()).is_empty());
}

#[test]
fn press_install_hook_keeps_user_hooks_through_the_binary() {
    let home = tempfile::tempdir().unwrap();
    let settings = home.path().join(".claude/settings.json");
    fs::create_dir_all(settings.parent().unwrap()).unwrap();
    let seeded = json!({"model": "x", "hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [
            {"type": "command", "command": "/u/audit"},
            {"type": "command", "command": "/old/tilth hook --host claude"}]}]}});
    fs::write(&settings, seeded.to_string()).unwrap();
    let out = run(&["install", "claude-code", "--hook"], "", Some(home.path()));
    assert_eq!(out.code, 0, "{}", out.stderr);
    let text = fs::read_to_string(&settings).unwrap();
    assert!(text.contains("/u/audit"), "user hook deleted: {text}");
    assert!(!text.contains("/old/tilth"), "stale entry kept: {text}");
    assert_eq!(text.matches("\"--host\"").count(), 1, "{text}");
}

#[test]
fn install_hook_bad_hook_file_leaves_mcp_config_unwritten() {
    let home = tempfile::tempdir().unwrap();
    let settings = home.path().join(".claude/settings.json");
    fs::create_dir_all(settings.parent().unwrap()).unwrap();
    fs::write(&settings, "{ not json").unwrap();
    let out = run(&["install", "claude-code", "--hook"], "", Some(home.path()));
    assert_eq!(out.code, 1, "{}", out.stderr);
    assert!(!home.path().join(".claude.json").exists());
    assert_eq!(fs::read_to_string(&settings).unwrap(), "{ not json");
}

#[test]
fn install_hook_droid_keeps_settings_json_hooks_and_claude_fails_closed() {
    let home = tempfile::tempdir().unwrap();
    let settings = home.path().join(".factory/settings.json");
    fs::create_dir_all(settings.parent().unwrap()).unwrap();
    let seeded = json!({"hooks": {"PreToolUse": [
        {"matcher": "Execute", "hooks": [{"type": "command", "command": "/u/audit"}]}]}});
    fs::write(&settings, seeded.to_string()).unwrap();
    let out = run(&["install", "droid", "--hook"], "", Some(home.path()));
    assert_eq!(out.code, 0, "{}", out.stderr);
    assert!(!home.path().join(".factory/hooks.json").exists());
    let text = fs::read_to_string(&settings).unwrap();
    assert!(text.contains("/u/audit"), "{text}");
    assert_eq!(text.matches(" hook --host claude").count(), 1, "{text}");

    let out = run(&["install", "claude-code", "--hook"], "", Some(home.path()));
    assert_eq!(out.code, 0, "{}", out.stderr);
    let claude: Value = serde_json::from_str(
        &fs::read_to_string(home.path().join(".claude/settings.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(
        claude["hooks"]["PreToolUse"][0]["hooks"][0]["onFailure"],
        "block"
    );
}

#[test]
fn hook_accepts_install_host_names_as_families() {
    for host in LANDED {
        let event = |command: &str| match host {
            "cursor" => json!({"command": command}).to_string(),
            "windsurf" => json!({"tool_info": {"command_line": command}}).to_string(),
            _ => claude_event(command),
        };
        let deny = run(&["hook", "--host", host], &event("cat a.rs"), None);
        assert_eq!(deny.code, 2, "{host}: {}", deny.stderr);
        assert!(deny.stderr.contains("tilth_"), "{host}: {}", deny.stderr);
        let allow = run(&["hook", "--host", host], &event("ls"), None);
        assert_eq!(allow.code, 0, "{host}: {}", allow.stderr);
        for out in [&deny, &allow] {
            assert!(
                !out.stderr.contains("invalid value") && !out.stderr.contains("unknown host"),
                "{host}: {}",
                out.stderr
            );
        }
    }
    let bogus = run(&["hook", "--host", "bogus"], &claude_event("ls"), None);
    assert_eq!(bogus.code, 2);
    let help = run(&["hook", "--help"], "", None);
    assert!(help.stdout.contains("claude-code"), "{}", help.stdout);
}

#[test]
fn reinstall_keeps_a_customised_hint() {
    let home = tempfile::tempdir().unwrap();
    let settings = home.path().join(".claude/settings.json");
    fs::create_dir_all(settings.parent().unwrap()).unwrap();
    let seeded = json!({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command",
         "command": "/old/tilth hook --host claude --hint native"}]}]}});
    fs::write(&settings, seeded.to_string()).unwrap();
    let out = run(&["install", "claude-code", "--hook"], "", Some(home.path()));
    assert_eq!(out.code, 0, "{}", out.stderr);
    let cfg: Value = serde_json::from_str(&fs::read_to_string(&settings).unwrap()).unwrap();
    let handlers = cfg["hooks"]["PreToolUse"][0]["hooks"].as_array().unwrap();
    assert_eq!(handlers.len(), 1, "{cfg}");
    assert_eq!(handlers[0]["command"], TILTH, "{cfg}");
    let args = handlers[0]["args"].as_array().expect("exec-form args");
    assert_eq!(
        args[args.len() - 4..],
        ["--host", "claude", "--hint", "native"],
        "{cfg}"
    );
}
