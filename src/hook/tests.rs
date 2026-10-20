use super::{hints, run_cli, run_io, verdict, Hint, Host, Verdict};
use serde_json::{json, Value};

const CORPUS: &str = include_str!("../../benchmark/hook_corpus.json");

const AC1: &[&str] = &[
    "cat src/lib.rs",
    "grep -rn foo .",
    "< src/a.go grep x",
    "echo $(< f)",
    "ls | xargs cat",
    "sort f",
    "cp f /dev/stdout",
    "git show HEAD:src/a.go",
    "sed -i s/a/b/ f.go",
    "perl -pi -e s/a/b/ f.go",
    "python3 - <<EOF\nprint(1)\nEOF",
    "echo x > f",
    "cmd | tee f",
    "bash -c \"cat f\"",
    "find . -exec cat {} +",
];

const AC2: &[&str] = &[
    "go test ./...",
    "cargo test -p x",
    "cargo build",
    "python -m pytest tests/a.py -x",
    "pytest -q tests",
    "npm test",
    "make test",
    "bash -c \"go test ./...\"",
    "go test ./... 2>&1 | grep FAIL",
    "git diff",
    "git show HEAD",
    "cp a b",
    "ls src",
];

fn corpus() -> Value {
    serde_json::from_str(CORPUS).expect("corpus parses")
}

fn rows(list: &str) -> Vec<(String, Option<String>)> {
    corpus()[list]
        .as_array()
        .expect("list is an array")
        .iter()
        .map(|row| {
            (
                row["command"].as_str().expect("command").to_string(),
                row["rule"].as_str().map(str::to_string),
            )
        })
        .collect()
}

fn all_deny_commands() -> Vec<String> {
    let mut all: Vec<String> = AC1.iter().map(|s| (*s).to_string()).collect();
    all.extend(rows("must_deny").into_iter().map(|(c, _)| c));
    all
}

fn all_allow_commands() -> Vec<String> {
    let mut all: Vec<String> = AC2.iter().map(|s| (*s).to_string()).collect();
    all.extend(rows("must_allow").into_iter().map(|(c, _)| c));
    all
}

fn claude_event(command: &str) -> String {
    json!({"tool_name": "Bash", "tool_input": {"command": command}}).to_string()
}

fn event(host: Host, command: &str) -> String {
    match host {
        Host::Claude | Host::Copilot => claude_event(command),
        Host::Cursor => json!({"command": command, "cwd": "/x"}).to_string(),
        Host::Windsurf => {
            json!({"agent_action_name": "pre_run_command", "tool_info": {"command_line": command, "cwd": "/x"}})
                .to_string()
        }
    }
}

fn run(host: Host, hint: Hint, input: &str) -> (i32, String) {
    let mut err = Vec::new();
    let code = run_io(host, hint, input, &mut err);
    (code, String::from_utf8(err).expect("utf8 stderr"))
}

fn names_tilth_tool(stderr: &str) -> bool {
    ["tilth_read", "tilth_search", "tilth_write"]
        .iter()
        .any(|t| stderr.contains(t))
}

const HOSTS: [Host; 4] = [Host::Claude, Host::Copilot, Host::Cursor, Host::Windsurf];

// AC-1
#[test]
fn ac1_listed_and_corpus_commands_deny_with_tilth_hint() {
    for command in all_deny_commands() {
        let (code, err) = run(Host::Claude, Hint::Tilth, &claude_event(&command));
        assert_eq!(code, 2, "should deny: {command:?}");
        assert!(
            names_tilth_tool(&err),
            "reason names no tilth tool for {command:?}: {err}"
        );
    }
}

#[test]
fn ac1_deny_rule_matches_corpus_row() {
    for (command, rule) in rows("must_deny") {
        let rule = rule.expect("must_deny row names a rule");
        match verdict(&command, Hint::Tilth) {
            Verdict::Deny { rule: got, .. } => assert_eq!(got, rule, "command {command:?}"),
            Verdict::Allow => panic!("allowed: {command:?}"),
        }
    }
}

#[test]
fn ac1_every_rule_entry_has_a_must_deny_row() {
    let used: Vec<String> = rows("must_deny")
        .into_iter()
        .filter_map(|(_, r)| r)
        .collect();
    let known = hints::all_rules();
    for rule in &known {
        assert!(
            used.iter().any(|u| u == rule),
            "no must_deny row for rule {rule:?}"
        );
    }
    for rule in &used {
        assert!(
            known.contains(&rule.as_str()),
            "row names unknown rule {rule:?}"
        );
    }
}

#[test]
fn deny_writes_nothing_on_stdout_by_construction() {
    // run_io has no stdout parameter; a deny puts the reason on the stderr writer only.
    let (code, err) = run(Host::Claude, Hint::Tilth, &claude_event("cat f"));
    assert_eq!(code, 2);
    assert_ne!(err, "");
}

// AC-2
#[test]
fn ac2_allowed_commands_exit_zero_silently() {
    for command in all_allow_commands() {
        let (code, err) = run(Host::Claude, Hint::Tilth, &claude_event(&command));
        assert_eq!((code, err.as_str()), (0, ""), "should allow: {command:?}");
    }
}

// AC-3
#[test]
fn ac3_malformed_json_denies() {
    for host in HOSTS {
        assert_eq!(run(host, Hint::Tilth, "{not json").0, 2, "{host:?}");
        assert_eq!(run(host, Hint::Tilth, "[]").0, 2, "{host:?}");
    }
}

#[test]
fn ac3_unknown_host_or_hint_denies() {
    assert_eq!(run_cli("bogus", "tilth"), 2);
    assert_eq!(run_cli("claude", "bogus"), 2);
    assert!("bogus".parse::<Host>().is_err());
    assert!("bogus".parse::<Hint>().is_err());
}

#[test]
fn ac3_shell_event_without_command_denies_in_every_shape() {
    let events = [
        (Host::Claude, json!({"tool_name": "Bash", "tool_input": {}})),
        (
            Host::Copilot,
            json!({"tool_name": "bash", "tool_input": {"command": 5}}),
        ),
        (Host::Cursor, json!({"cwd": "/x"})),
        (Host::Windsurf, json!({"tool_info": {"cwd": "/x"}})),
        (Host::Claude, json!({"tool_input": {}})),
    ];
    for (host, ev) in events {
        assert_eq!(
            run(host, Hint::Tilth, &ev.to_string()).0,
            2,
            "{host:?} {ev}"
        );
    }
}

#[test]
fn ac3_error_node_denies() {
    let (code, _) = run(Host::Claude, Hint::Tilth, &claude_event("echo \"$("));
    assert_eq!(code, 2);
    assert!(matches!(
        verdict("echo \"$(", Hint::Tilth),
        Verdict::Deny { .. }
    ));
    assert!(matches!(
        verdict("if then fi ((", Hint::Native),
        Verdict::Deny { .. }
    ));
}

// AC-4
#[test]
fn ac4_native_hint_names_native_tools_and_never_tilth() {
    let native = ["Read", "Grep", "Glob", "Edit", "Write"];
    for command in all_deny_commands() {
        let (code, err) = run(Host::Claude, Hint::Native, &claude_event(&command));
        assert_eq!(code, 2, "should deny: {command:?}");
        assert!(
            native.iter().any(|t| err.contains(t)),
            "no native tool in {err:?}"
        );
        assert!(
            !err.to_lowercase().contains("tilth"),
            "native reason names tilth: {err:?}"
        );
    }
}

#[test]
fn ac4_native_host_level_denies_never_name_tilth() {
    let (code, err) = run(Host::Claude, Hint::Native, "{not json");
    assert_eq!(code, 2);
    assert!(!err.to_lowercase().contains("tilth"));
}

// AC-5
#[test]
fn ac5_every_host_shape_gives_the_claude_verdict() {
    let commands: Vec<String> = all_deny_commands()
        .into_iter()
        .chain(all_allow_commands())
        .collect();
    for command in commands {
        let want = run(Host::Claude, Hint::Tilth, &event(Host::Claude, &command));
        for host in HOSTS {
            let got = run(host, Hint::Tilth, &event(host, &command));
            assert_eq!(got, want, "{host:?} differs for {command:?}");
        }
    }
}

#[test]
fn ac5_other_shell_tool_names_are_evaluated() {
    for name in [
        "Execute",
        "bash",
        "launch-process",
        "run_shell_command",
        "shell",
        "run_command",
    ] {
        let ev = json!({"tool_name": name, "tool_input": {"command": "cat f"}}).to_string();
        assert_eq!(run(Host::Claude, Hint::Tilth, &ev).0, 2, "{name}");
        let ok = json!({"tool_name": name, "tool_input": {"command": "go test ./..."}}).to_string();
        assert_eq!(run(Host::Claude, Hint::Tilth, &ok).0, 0, "{name}");
    }
}

#[test]
fn ac5_command_is_evaluated_whatever_the_tool_name() {
    let ev = json!({"tool_name": "Mystery", "tool_input": {"command": "cat f"}}).to_string();
    assert_eq!(run(Host::Claude, Hint::Tilth, &ev).0, 2);
}

#[test]
fn ac5_non_shell_event_without_command_is_allowed() {
    let ev = json!({"tool_name": "Read", "tool_input": {"file_path": "x"}}).to_string();
    assert_eq!(run(Host::Claude, Hint::Tilth, &ev), (0, String::new()));
    assert_eq!(run(Host::Copilot, Hint::Tilth, &ev), (0, String::new()));
}

#[test]
fn ac5_shell_tool_without_command_denies() {
    let ev = json!({"tool_name": "Bash", "tool_input": {}}).to_string();
    assert_eq!(run(Host::Claude, Hint::Tilth, &ev).0, 2);
    assert_eq!(run(Host::Copilot, Hint::Tilth, &ev).0, 2);
}

#[test]
fn host_and_hint_parse_names() {
    assert_eq!("windsurf".parse::<Host>(), Ok(Host::Windsurf));
    assert_eq!("native".parse::<Hint>(), Ok(Hint::Native));
    assert_eq!(Host::default(), Host::Claude);
    assert_eq!(Hint::default(), Hint::Tilth);
}
