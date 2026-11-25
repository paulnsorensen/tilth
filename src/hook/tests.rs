use super::{hints, run_cli, run_io, verdict, Capture, Hint, Host, Verdict};
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

/// Parses the embedded command corpus, panicking if the fixture is invalid JSON.
fn corpus() -> Value {
    serde_json::from_str(CORPUS).expect("corpus parses")
}

/// Extracts commands and optional rule names from one corpus list.
fn rows_of(list: &Value) -> Vec<(String, Option<String>)> {
    list.as_array()
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

/// Extracts commands and optional rule names from a named corpus list.
fn rows(list: &str) -> Vec<(String, Option<String>)> {
    rows_of(&corpus()[list])
}

/// Extracts the rows of a list in the `allow_capture` section: the verdicts with
/// `--allow-capture`.
fn capture_rows(list: &str) -> Vec<(String, Option<String>)> {
    rows_of(&corpus()["allow_capture"][list])
}

/// Combines the AC-1 commands with the must-deny corpus rows.
fn all_deny_commands() -> Vec<String> {
    let mut all: Vec<String> = AC1.iter().map(|s| (*s).to_string()).collect();
    all.extend(rows("must_deny").into_iter().map(|(c, _)| c));
    all
}

/// Combines the AC-2 commands with the must-allow corpus rows.
fn all_allow_commands() -> Vec<String> {
    let mut all: Vec<String> = AC2.iter().map(|s| (*s).to_string()).collect();
    all.extend(rows("must_allow").into_iter().map(|(c, _)| c));
    all
}

/// Serializes a Claude-shaped Bash event for the supplied command.
fn claude_event(command: &str) -> String {
    json!({"tool_name": "Bash", "tool_input": {"command": command}}).to_string()
}

/// Serializes a shell event in the selected host family's format.
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

/// Evaluates an event and returns its exit code and captured stderr as UTF-8.
fn run(host: Host, hint: Hint, input: &str) -> (i32, String) {
    let mut err = Vec::new();
    let code = run_io(host, hint, Capture::Deny, input, &mut err);
    (code, String::from_utf8(err).expect("utf8 stderr"))
}

/// Reports whether a denial message names at least one tilth tool.
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
        match verdict(&command, Hint::Tilth, Capture::Deny) {
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
    assert_eq!(run_cli("bogus", "tilth", Capture::Deny), 2);
    assert_eq!(run_cli("claude", "bogus", Capture::Allow), 2);
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
        verdict("echo \"$(", Hint::Tilth, Capture::Deny),
        Verdict::Deny { .. }
    ));
    assert!(matches!(
        verdict("if then fi ((", Hint::Native, Capture::Allow),
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

// Press round 1 attacks.

/// Reports whether a shell command is denied with tilth hints.
fn denies(command: &str) -> bool {
    matches!(
        verdict(command, Hint::Tilth, Capture::Deny),
        Verdict::Deny { .. }
    )
}

/// Asserts that every command is denied, reporting any commands that were allowed.
fn assert_all_deny(commands: &[&str]) {
    let leaked: Vec<&&str> = commands.iter().filter(|c| !denies(c)).collect();
    assert!(leaked.is_empty(), "allowed but must deny: {leaked:?}");
}

/// Asserts that every command is allowed, reporting any commands that were denied.
fn assert_all_allow(commands: &[&str]) {
    let blocked: Vec<&&str> = commands.iter().filter(|c| denies(c)).collect();
    assert!(blocked.is_empty(), "denied but must allow: {blocked:?}");
}

#[test]
fn press_redirect_forms_deny() {
    assert_all_deny(&[
        "cat >| f",
        "echo x &> f",
        "echo x >> f",
        "echo x 2> f",
        "cat <> f",
        "exec 3< f",
        "exec > f",
        "cat<f",
        "<f cat",
        "cat 0<f",
        "echo x >/dev/stdout",
    ]);
}

#[test]
fn press_quoting_tricks_deny() {
    assert_all_deny(&[
        "'c'at f",
        "\\cat f",
        "ca\"\"t f",
        "/bin/cat f",
        "/usr/bin/grep x f",
    ]);
}

#[test]
fn press_wrappers_deny() {
    assert_all_deny(&[
        "env cat f",
        "env FOO=1 cat f",
        "command cat f",
        "builtin cat f",
        "nice cat f",
        "nice -n 5 cat f",
        "timeout 5 cat f",
        "timeout -s KILL 5 cat f",
        "sudo cat f",
        "exec cat f",
        "time cat f",
        "! cat f",
        "nohup cat f",
        "echo f | xargs -I{} cat {}",
        "echo f | xargs -I{} sh -c 'cat {}'",
    ]);
}

/// Wrapper table: name, one valid option with its value, whether it takes `VAR=value`
/// words, and how many positionals it requires.
const WRAPPERS: &[(&str, &[&str], bool, usize)] = &[
    ("env", &["-u", "X"], true, 0),
    ("command", &["-p"], false, 0),
    ("builtin", &[], false, 0),
    ("exec", &["-a", "x"], false, 0),
    ("nice", &["-n", "5"], false, 0),
    ("nohup", &[], false, 0),
    ("timeout", &["-s", "KILL"], false, 1),
    ("stdbuf", &["-o", "L"], false, 0),
    ("sudo", &["-u", "root"], true, 0),
    ("doas", &["-u", "root"], false, 0),
    ("time", &["-f", "%e"], false, 0),
    ("xargs", &["-n", "1"], false, 0),
];

/// Enumerates every ordering of the supplied wrapper-prefix parts.
fn permutations(items: &[String]) -> Vec<Vec<String>> {
    if items.len() <= 1 {
        return vec![items.to_vec()];
    }
    let mut out = Vec::new();
    for i in 0..items.len() {
        let mut rest = items.to_vec();
        let head = rest.remove(i);
        for mut tail in permutations(&rest) {
            tail.insert(0, head.clone());
            out.push(tail);
        }
    }
    out
}

/// Every wrapper prefix the real tool accepts: any subset of {option+value, `--`,
/// `A=1`, required positional} in any order, with the option before `--`.
fn wrapper_prefixes() -> Vec<String> {
    let mut out = Vec::new();
    for (name, opt, assigns, positionals) in WRAPPERS {
        let mut items: Vec<(&str, String)> = Vec::new();
        if !opt.is_empty() {
            items.push(("opt", opt.join(" ")));
        }
        items.push(("dd", "--".to_string()));
        if *assigns {
            items.push(("asg", "A=1".to_string()));
        }
        if *positionals > 0 {
            items.push(("pos", "5".to_string()));
        }
        for mask in 0..(1u32 << items.len()) {
            let picked: Vec<String> = items
                .iter()
                .enumerate()
                .filter(|(k, _)| mask >> k & 1 == 1)
                .map(|(_, (tag, text))| format!("{tag}:{text}"))
                .collect();
            if *positionals > 0 && !picked.iter().any(|p| p.starts_with("pos:")) {
                continue;
            }
            for order in permutations(&picked) {
                let at = |tag: &str| order.iter().position(|p| p.starts_with(tag));
                if let (Some(o), Some(d)) = (at("opt:"), at("dd:")) {
                    if o > d {
                        continue;
                    }
                }
                let words: Vec<&str> = order
                    .iter()
                    .map(|p| p.split_once(':').map_or(p.as_str(), |(_, t)| t))
                    .collect();
                out.push(format!("{name} {}", words.join(" ")));
            }
        }
    }
    out
}

#[test]
fn press_wrapper_orderings_unwrap() {
    let prefixes = wrapper_prefixes();
    assert!(
        prefixes.len() >= 60,
        "too few combinations: {}",
        prefixes.len()
    );
    let deny: Vec<String> = prefixes.iter().map(|p| format!("{p} cat f")).collect();
    let allow: Vec<String> = prefixes
        .iter()
        .map(|p| format!("{p} go test ./..."))
        .collect();
    let leaked: Vec<&String> = deny.iter().filter(|c| !denies(c)).collect();
    assert!(leaked.is_empty(), "allowed but must deny: {leaked:?}");
    let blocked: Vec<&String> = allow.iter().filter(|c| denies(c)).collect();
    assert!(blocked.is_empty(), "denied but must allow: {blocked:?}");
}

#[test]
fn press_nested_wrappers_unwrap() {
    assert_all_deny(&[
        "sudo env -- X=1 timeout 5 -- nice cat f",
        "env -u X -- A=1 nice -n 5 -- timeout -s KILL 5 -- cat f",
        "nohup timeout 5 -- sudo -u root -- env A=1 -- time cat f",
        "doas -u root -- stdbuf -o L -- timeout -- 5 cat f",
    ]);
    assert_all_allow(&[
        "sudo env -- X=1 timeout 5 -- nice go test ./...",
        "env -u X -- A=1 nice -n 5 -- timeout -s KILL 5 -- go test ./...",
        "nohup timeout 5 -- sudo -u root -- env A=1 -- time go test ./...",
    ]);
}

#[test]
fn press_nested_shell_strings_deny() {
    assert_all_deny(&[
        "sh -c \"sh -c 'cat f'\"",
        "bash -lc 'cat f'",
        "eval cat f",
        "eval \"cat f\"",
        "echo 'cat f' | sh",
        "sh <<< 'cat f'",
        "{ cat f; }",
        "(cat f)",
        "if true; then cat f; fi",
        "for i in a; do cat $i; done",
        "f() { cat f; }; f",
        "echo \"$(cat f)\"",
        "echo `cat f`",
    ]);
}

#[test]
fn press_nesting_past_the_depth_limit_denies() {
    let mut command = String::from("cat f");
    for _ in 0..12 {
        command = format!("eval '{}'", command.replace('\'', "'\\''"));
    }
    assert!(denies(&command));
}

#[test]
fn press_file_readers_with_operands_deny() {
    assert_all_deny(&[
        "sed -n p f",
        "awk 1 f",
        "sed -ibak s/a/b/ f",
        "perl -i -pe 1 f",
        "awk -iinplace 1 f",
    ]);
}

#[test]
fn press_inline_interpreter_code_denies() {
    assert_all_deny(&[
        "python -Sc 1",
        "python3.11 -c 1",
        "/usr/bin/python3 -c 1",
        "python3 -W ignore -c 1",
        "echo x | python -c 'print(1)'",
        "echo x | python3 -",
        "node --eval 1",
        "node -p 1",
        "deno eval 1",
        "bun -e 1",
        "ruby -e 1",
        "php -r 1",
        "perl -E 1",
    ]);
}

#[test]
fn press_filters_in_pipelines_allow() {
    assert_all_allow(&[
        "echo a | grep x",
        "echo a | grep -e x",
        "echo a | sed s/a/b/",
        "echo a | awk 1",
        "ls | tail -n 5",
        "echo a | rg x",
        "go test ./... 2>&1",
        "go test ./... >&2",
        "go test ./... > /dev/null 2>&1",
    ]);
}

#[test]
fn press_recursive_search_via_directories_flag_denies() {
    assert_all_deny(&["grep -d recurse x", "grep --directories=recurse x"]);
}

#[test]
fn press_device_paths_with_dot_segments_deny() {
    assert_all_deny(&[
        "cp f /dev/./stdout",
        "cp f /dev//stdout",
        "cp f /dev/./stderr",
        "cp f /proc/self/./fd/1",
        "cp f /dev/./fd/1",
    ]);
}

#[test]
fn press_availability_probes_for_searchers_allow() {
    assert_all_allow(&["rg --version", "rg -V", "rg --help", "command -v rg"]);
}

#[test]
fn press_bare_rg_recurses_and_denies() {
    assert_all_deny(&["rg", "rg x .", "rg x"]);
}

#[test]
fn press_non_string_command_denies_in_every_shape() {
    for host in HOSTS {
        let ev = match host {
            Host::Claude | Host::Copilot => {
                json!({"tool_name": "Bash", "tool_input": {"command": 7}})
            }
            Host::Cursor => json!({"command": 7}),
            Host::Windsurf => json!({"tool_info": {"command_line": 7}}),
        }
        .to_string();
        assert_eq!(run(host, Hint::Tilth, &ev).0, 2, "{host:?}");
    }
}

#[test]
fn press_null_command_with_shell_name_denies() {
    let ev = json!({"tool_name": "Bash", "tool_input": {"command": null}}).to_string();
    assert_eq!(run(Host::Claude, Hint::Tilth, &ev).0, 2);
}

#[test]
fn press_shell_names_without_command_deny() {
    for name in [
        "Bash",
        "bash",
        "Execute",
        "launch-process",
        "run_shell_command",
        "shell",
        "run_command",
    ] {
        let ev = json!({"tool_name": name, "tool_input": {}}).to_string();
        assert_eq!(run(Host::Claude, Hint::Tilth, &ev).0, 2, "{name}");
    }
}

// ---- press round 2 ----

#[test]
fn press2_device_dot_dot_variants_deny() {
    assert_all_deny(&[
        "cp f /dev/../dev/stdout",
        "cp f /dev/fd/../stdout",
        "cp f /./dev/stdout",
        "cp f /dev/stdout/",
        "cp f //dev/stdout",
        "cp f /proc/./self/../self/fd/1",
        "cp f /dev/././stderr",
        "cp f --target-directory=/dev/./fd/1",
    ]);
}

#[test]
fn press2_grep_directories_recurse_spellings_deny() {
    assert_all_deny(&[
        "grep -drecurse x",
        "grep --directories recurse x",
        "grep --directories=recurse x",
        "grep -nd recurse x",
        "grep -rd skip x",
        "grep -d skip -r x",
        "egrep -d recurse x",
    ]);
}

#[test]
fn press2_grep_directories_abbreviations_deny() {
    // GNU grep accepts unambiguous abbreviations of the ACTION and of long options.
    assert_all_deny(&[
        "grep -d rec x",
        "grep -d r x",
        "grep --directories=rec x",
        "grep --rec x",
        "grep --recurs x",
        "grep --directo=recurse x",
    ]);
}

#[test]
fn press2_probe_version_flag_with_file_operand_allows() {
    assert_all_allow(&[
        "rg --version f",
        "rg -V -e x .",
        "rg --help --files",
        "grep --help -r x .",
        "command -v rg",
        "command -v rg && command -v cat",
    ]);
}

#[test]
fn press2_probe_chains_do_not_launder_reads() {
    assert_all_deny(&[
        "command -v rg && rg x",
        "command -v x; cat f",
        "command -v x || cat f",
        "command -v cat | xargs cat",
        "command cat f",
        "command -v cat; cat f",
        "command -vp cat f",
        "command -v x > f",
        "command -v x >> f",
        "command -v $(cat f)",
        "command -v x & cat f",
        "command -v x\ncat f",
        "if command -v rg; then rg x; fi",
        "command -v rg >/dev/stdout",
    ]);
}

#[test]
fn press2_version_flag_swallowed_as_value_still_denies() {
    // The value of a valued flag is not a flag; rg then searches recursively.
    assert_all_deny(&[
        "rg --path-separator --version x",
        "rg --context-separator --version x",
        "grep --exclude-from --version x f",
        "rg -e --version f",
        "rg -- --version f",
    ]);
}

#[test]
fn press3_grep_h_is_no_filename_not_help_and_reads() {
    // In grep and ack, -h is --no-filename; `grep -h x f` reads f. Only rg/ag -h is help.
    assert_all_deny(&["grep -h x f", "egrep -h x f", "fgrep -h x f", "ack -h x f"]);
}

#[test]
fn press3_grep_recursion_variants_deny() {
    assert_all_deny(&[
        "grep --dereference-rec x",
        "grep --dereference-rec x .",
        "grep --dir=recurse x",
        "grep -nd recurse x",
    ]);
}

#[test]
fn press3_grep_invalid_directories_action_exits_before_reading() {
    // GNU grep matches the ACTION case-sensitively; `-d rECURSE` is an error, not a search.
    assert_all_allow(&["grep -d rECURSE x", "grep -d --version x"]);
}

#[test]
fn press3_help_probe_first_exits_before_reading() {
    assert_all_allow(&[
        "rg -h x .",
        "rg --help -e x .",
        "grep --help x f",
        "grep -V x f",
        "grep -V -r x .",
        "rg -V -r x .",
    ]);
}

#[test]
fn press4_short_probe_flags_do_not_launder_file_reads() {
    // -h is --no-filename for grep-family and ack; -hn / -hr are bundles, not help.
    assert_all_deny(&[
        "fgrep -hn x f",
        "grep -hr x .",
        "ack -h x",
        "ack -h x f",
        "ack -V x f",
        "ag -V x f",
    ]);
}

#[test]
fn press4_rg_ag_help_probe_still_allowed() {
    assert_all_allow(&["ag -h x f", "ag --help x f", "ack --version x f"]);
}

#[test]
fn cure1_every_redirect_destination_word_is_an_operand() {
    // The grammar repeats `destination`, so words after a redirect target are command arguments.
    assert_all_deny(&[
        "cat 2>/dev/null src/lib.rs",
        "grep foo 2>/dev/null f",
        "head -n 50 2>&1 f",
        "tee 2>/dev/null out.txt",
        "sed 2>/dev/null -i s/a/b/ f",
        "cat 2>/dev/null a b >/dev/null c",
        "cat <<EOF src/lib.rs\nx\nEOF",
    ]);
}

#[test]
fn cure1_redirect_to_null_without_operands_still_allowed() {
    assert_all_allow(&[
        "go test ./... 2>/dev/null",
        "cargo build 2>&1",
        "cat <<EOF | wc -l\nx\nEOF",
    ]);
}

#[test]
fn cure1_shell_reading_stdin_script_denies() {
    assert_all_deny(&[
        "bash -s -- a",
        "sh -s x",
        "bash /dev/stdin",
        "bash /dev/fd/0",
        "zsh /proc/self/fd/0",
        "bash <(echo ls)",
        "sh <(cat f)",
        "source <(echo ls)",
        ". <(echo ls)",
        "source /dev/stdin",
        ". /dev/stdin",
        "source $SCRIPT",
        ". \"$HOME/x.sh\"",
    ]);
}

#[test]
fn cure1_shell_and_source_of_literal_file_allowed() {
    assert_all_allow(&[
        "bash script.sh",
        "sh ./run.sh arg",
        "source ./env.sh",
        ". ./env.sh",
    ]);
}

#[test]
fn cure1_wrappers_skip_option_values() {
    assert_all_deny(&[
        "timeout -s KILL 5 cat f",
        "timeout --signal KILL 5 cat f",
        "timeout --signal=KILL 5 cat f",
        "sudo -u root cat f",
        "sudo --user root cat f",
        "stdbuf -o L cat f",
        "nice --adjustment 5 cat f",
        "nice -n 5 cat f",
        "env --chdir /tmp cat f",
        "env --unset FOO cat f",
        "env -- cat f",
        "sudo -- cat f",
        "xargs -n 1 cat",
        "xargs -I {} cat {}",
        "xargs -P 4 cat",
        "xargs --max-args 1 cat",
        "xargs -n1 cat",
        "xargs -- cat",
    ]);
}

#[test]
fn cure3_assignments_after_terminator_are_skipped() {
    assert_all_deny(&[
        "env -- FOO=1 cat f",
        "env -- A=1 B=2 grep x f",
        "command env -- X=1 head f",
        "sudo -- FOO=1 cat f",
        "sudo -u root -- FOO=1 cat f",
    ]);
}

#[test]
fn cure1_wrapped_allowed_commands_stay_allowed() {
    assert_all_allow(&[
        "timeout -s KILL 5 go test ./...",
        "sudo -u root go test ./...",
        "stdbuf -o L cargo build",
        "nice --adjustment 5 cargo build",
        "env --chdir /tmp make test",
        "xargs -n 1 echo",
        "xargs -I {} echo {}",
    ]);
}

#[test]
fn cure1_unknown_wrapper_option_fails_closed() {
    assert_all_deny(&[
        "env --bogus go test",
        "sudo --bogus go test",
        "timeout -Z 5 go test",
        "xargs --bogus echo",
        "env -S 'cat f'",
    ]);
}

/// Asserts that each command is denied by its expected rule.
fn assert_rules(rows: &[(&str, &str)]) {
    for (command, rule) in rows {
        match verdict(command, Hint::Tilth, Capture::Deny) {
            Verdict::Deny { rule: got, .. } => {
                assert_eq!(got, *rule, "wrong rule for {command:?}");
            }
            Verdict::Allow => panic!("should deny: {command:?}"),
        }
    }
}

#[test]
fn cure2_redirects_nested_under_a_heredoc_pass_their_words_to_the_command() {
    assert_rules(&[
        ("cat <<EOF 2>/dev/null f\nx\nEOF", "cat"),
        ("cat <<EOF 2>/dev/null f g\nx\nEOF", "cat"),
        ("grep x <<EOF 2>&1 f\nx\nEOF", "grep"),
        ("cat <<EOF >/dev/null 2>&1 f\nx\nEOF", "cat"),
        ("cat <<EOF >/dev/null 2>/dev/null f\nx\nEOF", "cat"),
        ("cat <<EOF f\nx\nEOF", "cat"),
        ("cat <<-EOF 2>/dev/null f\nx\nEOF", "cat"),
        ("cat <<<x 2>/dev/null f", "cat"),
        ("cat <<< x f", "cat"),
        ("{ cat <<EOF 2>/dev/null f\nx\nEOF\n}", "cat"),
        ("true && cat <<EOF 2>/dev/null f\nx\nEOF", "unresolved"),
        ("cat <<EOF 2>/dev/null $F\nx\nEOF", "cat"),
    ]);
}

#[test]
fn cure2_heredoc_without_extra_words_still_allowed() {
    assert_all_allow(&[
        "go test ./... <<EOF 2>/dev/null\nx\nEOF",
        "cargo build <<EOF >/dev/null 2>&1\nx\nEOF",
        "cat <<EOF | wc -l\nx\nEOF",
    ]);
}

#[test]
fn cure2_shell_with_non_literal_script_denies() {
    assert_rules(&[
        ("sh $X <<<'cat f'", "unresolved"),
        ("bash \"$S\"", "unresolved"),
        ("bash $(echo /dev/stdin) <<EOF\ncat f\nEOF", "unresolved"),
        ("echo cat f | sh $X", "unresolved"),
        ("bash -e \"$S\" arg", "unresolved"),
    ]);
}

#[test]
fn cure2_xargs_arg_file_denies_as_input_read() {
    assert_rules(&[
        ("xargs -a f echo", "input redirect"),
        ("xargs -af echo", "input redirect"),
        ("xargs --arg-file=src/lib.rs echo", "input redirect"),
        ("xargs --arg-file f echo", "input redirect"),
        ("xargs -n1 -a f echo", "input redirect"),
        ("xargs -0a f echo", "input redirect"),
        ("sudo xargs -a f echo", "input redirect"),
    ]);
    assert_all_allow(&[
        "xargs -a /dev/null echo",
        "xargs -n1 echo",
        "xargs -I -a echo",
    ]);
}

#[test]
fn cure2_wrappers_skip_leading_assignments() {
    assert_rules(&[
        ("sudo FOO=1 cat f", "cat"),
        ("sudo -E FOO=1 cat f", "cat"),
        ("doas FOO=1 cat f", "cat"),
        ("nice FOO=1 cat f", "cat"),
        ("nohup FOO=1 cat f", "cat"),
        ("stdbuf -o L FOO=1 cat f", "cat"),
        ("command FOO=1 cat f", "cat"),
        ("exec FOO=1 cat f", "cat"),
    ]);
    assert_all_allow(&["sudo FOO=1 go test ./...", "nice FOO=1 make test"]);
}

#[test]
fn cure2_time_output_option_is_a_write() {
    assert_rules(&[
        ("command time -o out.txt ls", "output redirect"),
        ("env time --output=out.txt ls", "output redirect"),
        ("command time -f %e -o out.txt ls", "output redirect"),
        ("command time --output out.txt ls", "output redirect"),
    ]);
    assert_all_allow(&[
        "command time -p make test",
        "command time -o /dev/null make test",
    ]);
}

#[test]
fn cure3_double_quote_escapes_decode_before_reparse() {
    assert_rules(&[
        ("bash -c \"echo \\`cat src/lib.rs\\`\"", "cat"),
        ("eval \"echo \\`cat f\\`\"", "cat"),
        ("bash -c \"echo \\$(cat f)\"", "cat"),
        ("bash -c \"cat \\\"f\\\"\"", "cat"),
    ]);
    assert_all_allow(&["bash -c \"echo \\$HOME\"", "bash -lc \"go test ./...\""]);
}

#[test]
fn cure3_ansi_c_strings_decode_before_literal_check() {
    assert_rules(&[
        ("$'\\x63at' src/lib.rs", "cat"),
        ("bash -c $'cat\\x20f'", "cat"),
        ("cp f $'/dev/std\\x6fut'", "device path"),
        ("$'\\143at' f", "cat"),
        ("$'\\u0063at' f", "cat"),
        ("$'ca\\x74' f", "cat"),
        ("bash -c $'\\ud800'", "unresolved"),
    ]);
    assert_all_allow(&["echo $'\\n'", "printf $'a\\tb\\n'"]);
}

#[test]
fn cure3_dev_stdin_is_a_device_path() {
    assert_rules(&[
        ("source /dev/stdin", "device path"),
        ("python3 /dev/stdin", "device path"),
        ("perl /dev/stdin", "device path"),
    ]);
}

#[test]
fn cure3_sort_output_and_files0_from_deny() {
    assert_rules(&[
        ("sort -o out.txt f", "sort"),
        ("sort -oout.txt f", "sort"),
        ("sort --output=out.txt f", "sort"),
        ("sort --output out.txt f", "sort"),
        ("sort -k1 -o out.txt", "sort"),
        ("sort --files0-from=list", "sort"),
        ("sort --files0-from list", "sort"),
    ]);
    assert_all_allow(&[
        "echo b a | sort",
        "echo b a | sort -r -k1",
        "echo b a | sort -o /dev/null",
        "echo b a | sort --output=/dev/null",
    ]);
}

#[test]
fn cure3_interpreter_stdin_script_operands_deny() {
    assert_rules(&[
        ("python3 <(echo 'print(1)')", "python"),
        ("node <(echo 'x')", "node"),
        ("echo 'print(1)' | python3 -- -", "python"),
        ("echo x | python3 -- /dev/fd/0", "device path"),
        ("echo x | deno run -", "deno"),
        ("deno run /dev/stdin", "device path"),
        ("echo x | bun run -", "bun"),
        ("bun exec 'echo hi'", "bun"),
        ("deno run -A -", "deno"),
    ]);
    assert_all_allow(&[
        "python3 script.py",
        "python3 -- script.py",
        "deno run main.ts",
        "bun run build",
    ]);
}

#[test]
fn cure3_uncovered_commands_deny() {
    assert_rules(&[
        ("fish -c 'cat f'", "cat"),
        ("tcsh -c 'cat f'", "cat"),
        ("trap 'cat f' EXIT", "cat"),
        ("script -qc 'cat f' /dev/null", "cat"),
        ("script -q out.log", "output redirect"),
        ("flock lock cat f", "cat"),
        ("taskset 1 cat f", "cat"),
        ("strace cat f", "cat"),
        ("strace -o out.txt ls", "output redirect"),
        ("fd -x cat", "find -exec"),
        ("fd -X cat .rs", "find -exec"),
        ("fd --exec cat", "find -exec"),
        ("ugrep -r foo .", "grep"),
        ("zgrep foo f.gz", "grep"),
        ("ast-grep -p foo .", "rg"),
        ("sg -p foo .", "rg"),
        ("paste f", "paste"),
        ("rev f", "rev"),
        ("fold f", "fold"),
        ("pr f", "pr"),
        ("comm a b", "comm"),
        ("column f", "column"),
        ("expand f", "expand"),
        ("iconv -f utf8 f", "iconv"),
        ("iconv -f a -t b -o out f", "iconv"),
        ("ed f", "ed"),
        ("ex f", "ex"),
        ("vim -es f", "vim"),
        ("echo x | sponge f", "sponge"),
        ("curl file:///etc/passwd", "input redirect"),
        ("curl -o out.txt http://x", "output redirect"),
        ("curl -O http://x/f", "output redirect"),
        ("curl -d @f http://x", "input redirect"),
        ("curl -T f http://x", "input redirect"),
        ("git diff --no-index a b", "diff"),
        ("git -c alias.x='!cat f' x", "git -c"),
        ("git -c core.pager='cat f' log", "git -c"),
        ("tar -xOf a.tar", "tar"),
        ("tar xOf a.tar", "tar"),
        ("tar --to-stdout -xf a.tar", "tar"),
    ]);
    assert_all_allow(&[
        "echo x | sponge",
        "curl -s http://example.com",
        "curl -o /dev/null http://x",
        "curl -d x=1 http://x",
        "vim --version",
        "trap - EXIT",
        "git -c color.ui=never log",
        "git diff HEAD~1",
        "fd -e rs",
        "tar -tf a.tar",
        "echo a b | paste",
        "echo a | rev",
        "script -qc 'go test ./...' /dev/null",
    ]);
}

#[test]
fn cure3_file_options_and_script_io_deny() {
    assert_rules(&[
        ("echo x | sed 'r f'", "sed"),
        ("echo x | sed -e 'R f'", "sed"),
        ("echo x | sed -n 'w out'", "sed"),
        ("echo x | sed '1W out'", "sed"),
        ("echo x | sed 's/a/b/w f'", "sed"),
        ("echo x | sed '/x/r f'", "sed"),
        ("echo | sed --e='r f'", "sed"),
        ("echo | sed --ex='r f'", "sed"),
        ("echo | awk '{getline l < \"f\"; print l}'", "awk"),
        ("echo | awk '{print > \"f\"}'", "awk"),
        ("echo | awk '{ print | \"sh\" }'", "awk"),
        ("echo | awk 'BEGIN { system(\"cat f\") }'", "awk"),
        ("echo | awk 'BEGIN{ARGV[1]=\"f\";ARGC=2}{print}'", "awk"),
        ("jq -n --rawfile x f '$x'", "jq"),
        ("jq -n --slurpfile x f '$x'", "jq"),
        ("python3 -m base64 f", "python -m"),
        ("python3 -m tokenize f", "python -m"),
        ("python3 -m code", "python"),
    ]);
    assert_all_allow(&[
        "git -c color.ui=always log",
        "git diff HEAD~1 -- src/x.rs",
        "curl -sS https://example.com",
        "tar -tf a.tar",
        "deno run --config x.json main.ts",
        "echo | unexpand",
        "echo | fmt",
        "echo '{}' | jq .",
        "jq -n '1'",
        "jq --arg x 1 -n '$x'",
        "echo a | sed 's/a/b/'",
        "echo a | sed -n '/a/p'",
        "echo a | sed 'y/a/b/'",
        "echo a | awk '{ print $1 }'",
        "echo a | awk '$1 > 1 { print $1 }'",
        "python3 -m pytest",
        "python3 -m json.tool --help",
    ]);
}

#[test]
fn cure4_second_round_denies() {
    assert_rules(&[
        ("git -c alias.s=show s HEAD:f", "git -c"),
        ("git -c alias.g=grep g x", "git -c"),
        ("git --config-env alias.g=V g x", "git -c"),
        ("git --config-env=alias.g=V g x", "git -c"),
        ("git diff /dev/null src/main.rs", "diff"),
        ("git diff /etc/passwd src/main.rs", "diff"),
        ("curl -d@f https://x.test", "input redirect"),
        ("curl -sd@f https://x.test", "input redirect"),
        ("curl -w@f https://x.test", "input redirect"),
        ("tar -xf a.tar --to-command=cat", "tar"),
        ("tar -xf a.tar --to-c=cat", "tar"),
        ("tar -cf - f", "tar"),
        ("tar -c f", "tar"),
        ("tar --create f", "tar"),
        ("fish -C 'cat f'", "shell stdin"),
        ("deno run --config x.json -", "deno"),
        ("deno run --import-map m.json -", "deno"),
        ("unexpand f", "unexpand"),
        ("fmt f", "fmt"),
        ("join a b", "join"),
        ("zcat f.gz", "zcat"),
    ]);
    assert_all_allow(&["tar -cf a.tar f", "tar -tf a.tar"]);
}

#[test]
fn cure5_third_round_denies() {
    assert_rules(&[
        ("true | sg -p 'fn $F'", "rg"),
        ("true | sg run -p a -r b -U", "rg"),
        ("echo x | sg --stdin -p a --update-all", "rg"),
        ("true | ast-grep -p a", "rg"),
        (
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.s GIT_CONFIG_VALUE_0='!cat f' git s",
            "git -c",
        ),
        ("GIT_PAGER='cat f' git log", "git -c"),
        ("GIT_EXTERNAL_DIFF=x git diff", "git -c"),
        ("env GIT_PAGER='cat f' git log", "git -c"),
        ("GIT_PAGER=less\\ -f git log", "git -c"),
        ("git -c credential.helper=x log", "git -c"),
        ("git -c gpg.ssh.program=x log", "git -c"),
        ("git -c core.hooksPath=h log", "git -c"),
        ("git -c filter.x.smudge=c log", "git -c"),
        ("git -c diff.x.command=c log", "git -c"),
        ("fish -C true -c 'cat f'", "cat"),
        ("fish --init-command=true -c 'cat f'", "cat"),
        ("fish -C 'cat f' -c true", "cat"),
        ("deno run --reload -", "deno"),
        ("deno run --env-file -", "deno"),
        ("deno run --lock -", "deno"),
    ]);
    assert_all_allow(&[
        "echo x | sg --stdin -p a -l rust",
        "GIT_AUTHOR_NAME=x git commit -m m",
        "GIT_PAGER=cat git log",
        "fish -C true -c 'echo hi'",
        "deno run --config x.json main.ts",
    ]);
}

#[test]
fn cure3_xargs_tee_denies() {
    assert_rules(&[("echo f | xargs tee", "tee")]);
    assert_all_allow(&["echo x | tee", "echo x | tee /dev/null"]);
}

// The benchmark strict-cell probe (`benchmark/run.py` `_PROBE_*`, audited by `benchmark/tests/test_strict_hook.py`)
// depends on this exact contract: allow exits 0, deny exits 2, and each hint's denial names its read tool.
#[test]
fn benchmark_probe_contract() {
    let allow = "go test ./...";
    let deny = "cat src/lib.rs";
    for (hint, text) in [(Hint::Tilth, "tilth_read"), (Hint::Native, "Read tool")] {
        let (code, err) = run(Host::Claude, hint, &claude_event(allow));
        assert_eq!((code, err.as_str()), (0, ""), "allow {allow:?}");
        let (code, err) = run(Host::Claude, hint, &claude_event(deny));
        assert_eq!(code, 2, "deny {deny:?}");
        assert!(
            err.contains(text),
            "denial for {deny:?} lacks {text:?}: {err}"
        );
    }
}

// Tilth-mode hint text points at the cheapest correct tilth call.

/// Returns the tilth-mode denial text for a command.
fn tilth_reason_for(command: &str) -> String {
    match verdict(command, Hint::Tilth, Capture::Deny) {
        Verdict::Deny { reason, .. } => reason,
        Verdict::Allow => panic!("allowed: {command:?}"),
    }
}

#[test]
fn hint_redirect_and_tee_lead_with_create_file_and_skip_the_read() {
    for command in ["cat > f <<EOF\nx\nEOF", "echo x > f", "cmd | tee f"] {
        let reason = tilth_reason_for(command);
        assert!(reason.contains("create_file"), "{command:?}: {reason}");
        assert!(reason.contains("New file: no read needed"), "{reason}");
        assert!(reason.contains("Existing file"), "{reason}");
        assert!(
            !reason.contains("Read the file with tilth_read first"),
            "{reason}"
        );
        let new_at = reason.find("create_file").expect("create_file");
        let edit_at = reason.find("replace_text").expect("replace_text");
        assert!(new_at < edit_at, "new-file call comes first: {reason}");
    }
}

#[test]
fn hint_write_kind_reuses_the_tag_the_agent_already_holds() {
    for command in [
        "sed -i s/a/b/ f.go",
        "patch -p1 < x.diff",
        "git apply x.diff",
    ] {
        let reason = tilth_reason_for(command);
        assert!(
            reason.contains("you already have from tilth_read or tilth_search"),
            "{command:?}: {reason}"
        );
        assert!(reason.contains("only if you have none"), "{reason}");
        assert!(
            !reason.contains("Read the file with tilth_read first"),
            "{reason}"
        );
    }
}

#[test]
fn hint_interpreters_cover_computing_and_writing() {
    for command in [
        "python3 - <<EOF\nprint(1)\nEOF",
        "python -c 'print(1)'",
        "perl -e 1",
        "node -e 1",
        "ruby -e 1",
    ] {
        let reason = tilth_reason_for(command);
        assert!(
            reason.contains("To compute or check"),
            "{command:?}: {reason}"
        );
        assert!(
            reason.contains("project's own test or build command"),
            "{reason}"
        );
        assert!(reason.contains("tilth_search or tilth_read"), "{reason}");
        assert!(reason.contains("create_file"), "{reason}");
        assert!(reason.contains("replace_text"), "{reason}");
    }
}

#[test]
fn hint_shell_map_names_the_write_then_run_route() {
    for command in [
        "python -c 'print(1)'",
        "node -e 1",
        "wc -l src/lib.rs",
        "git grep foo",
    ] {
        let reason = tilth_reason_for(command);
        assert!(
            reason.contains("unique scratch dir (`mktemp -d`"),
            "{command:?}: {reason}"
        );
        assert!(reason.contains("run that file with Bash"), "{reason}");
        assert!(!reason.contains("Do not script it"), "{reason}");
    }
    let wc = tilth_reason_for("wc -l src/lib.rs");
    assert!(wc.contains("git grep repo files"), "{wc}");
    assert!(wc.contains("inline interpreters"), "{wc}");
    assert!(denies("python -c 'print(1)'") && denies("wc -l src/lib.rs"));
}

#[test]
fn hint_wc_points_at_the_read_header_and_listings() {
    let reason = tilth_reason_for("wc -l src/lib.rs");
    assert!(
        reason.contains("whole path shows its total line count in the header"),
        "{reason}"
    );
    assert!(reason.contains("signature outline"), "{reason}");
    assert!(reason.contains("`ls` or `find`"), "{reason}");
    assert!(!reason.contains("#1-1"), "{reason}");
}

#[test]
fn hint_other_readers_and_search_keep_their_text() {
    let cat = tilth_reason_for("cat f");
    assert!(cat.contains("Append #start-end or #symbol"), "{cat}");
    assert!(!cat.contains("line count"), "{cat}");
    let grep = tilth_reason_for("grep -rn foo .");
    assert!(grep.contains("searches files"), "{grep}");
}

#[test]
fn hint_rewording_leaves_every_corpus_decision_and_rule_unchanged() {
    for (command, rule) in rows("must_deny") {
        let rule = rule.expect("must_deny row names a rule");
        for hint in [Hint::Tilth, Hint::Native] {
            match verdict(&command, hint, Capture::Deny) {
                Verdict::Deny { rule: got, .. } => assert_eq!(got, rule, "{command:?}"),
                Verdict::Allow => panic!("allowed: {command:?}"),
            }
        }
    }
    for command in all_allow_commands() {
        assert!(!denies(&command), "denied but must allow: {command:?}");
    }
}

// Tool runners, debuggers, and programs that read commands from stdin.

#[test]
fn runners_unwrap_to_the_command_they_run() {
    assert_rules(&[
        ("mise exec -- cat src/lib.rs", "cat"),
        ("mise x -- cat f", "cat"),
        ("mise exec node@20 -- cat f", "cat"),
        ("mise -C /tmp exec -- cat f", "cat"),
        ("mise exec -c 'cat f'", "cat"),
        ("mise exec --command='cat f'", "cat"),
        ("mise exec -- sh", "shell stdin"),
        ("uv run cat f", "cat"),
        ("uv run --python 3.12 cat f", "cat"),
        ("uv run --with=x -- head f", "head"),
        ("uv --directory d run grep x f", "grep"),
        ("uv run python", "python"),
        ("uv run -m json.tool f", "python -m json.tool"),
        ("uv run --module=base64 f", "python -m"),
        ("uv run -", "python"),
        ("uvx --from x cat f", "cat"),
        ("npx cat f", "cat"),
        ("npx -p x cat f", "cat"),
        ("npx -c 'cat f'", "cat"),
        ("npm exec -- cat f", "cat"),
        ("poetry run cat f", "cat"),
        ("poetry -C d run python -c 1", "python"),
        ("bundle exec cat f", "cat"),
        ("pnpm exec cat f", "cat"),
        ("pnpm dlx -c 'cat f'", "cat"),
        ("pnpm --filter x exec head f", "head"),
        ("yarn exec cat f", "cat"),
        ("yarn dlx -p x cat f", "cat"),
        ("bunx cat f", "cat"),
        ("bun x cat f", "cat"),
        ("direnv exec . cat f", "cat"),
        ("nix develop -c cat f", "cat"),
        ("nix develop .#x --command bash -c 'cat f'", "cat"),
        ("nix develop", "shell stdin"),
        ("nix shell nixpkgs#jq", "shell stdin"),
        ("nix-shell --run 'cat f'", "cat"),
        ("nix-shell -p jq --command 'cat f'", "cat"),
        ("nix-shell", "shell stdin"),
        ("asdf exec cat f", "cat"),
        ("pixi run -e dev cat f", "cat"),
        ("conda run -n env cat f", "cat"),
        ("micromamba run -n env cat f", "cat"),
        ("timeout 5 mise exec -- env cat f", "cat"),
        ("mise exec -- uv run cat f", "cat"),
        ("echo f | xargs uv run cat", "cat"),
    ]);
    assert_all_allow(&[
        "mise exec -- cargo test",
        "mise exec go@1.24.7 -- python3 script.py",
        "mise install",
        "uv run pytest -q",
        "uv run --frozen pytest",
        "uv run pytest -q -p no:cacheprovider tests",
        "uv run --with pytest-xdist pytest -n 4",
        "uv run -m pytest",
        "uv run script.py",
        "uv sync",
        "npx jest",
        "npx jest --ci",
        "npm test",
        "npm exec -- tsc",
        "bundle exec rspec",
        "bundle install",
        "poetry run pytest",
        "pnpm exec vitest run",
        "pnpm test",
        "yarn test",
        "yarn exec jest",
        "bunx tsc",
        "bun run build",
        "bun test",
        "direnv exec . go test ./...",
        "nix develop -c cargo test",
        "nix build",
        "nix-shell --run 'cargo test'",
        "asdf exec go test ./...",
        "pixi run pytest",
        "conda run -n env pytest",
    ]);
}

#[test]
fn debuggers_need_batch_and_plain_commands() {
    assert_rules(&[
        ("gdb -batch -ex \"shell cat src/main.rs\"", "cat"),
        ("gdb -batch -ex '!cat f'", "cat"),
        ("gdb --batch --ex='shell head f'", "head"),
        ("gdb -batch -ex 'shell true' -ex 'shell cat f'", "cat"),
        ("gdb -batch -eval-command 'she grep x f'", "grep"),
        ("gdb -batch -ex shell", "shell stdin"),
        (
            "gdb -batch -ex 'python print(open(\"f\").read())'",
            "debugger script",
        ),
        ("gdb -batch -ex 'py print(1)'", "debugger script"),
        ("gdb -batch -ex pi", "debugger script"),
        ("gdb -batch -ex 'pipe info frame | cat'", "debugger script"),
        ("gdb -batch -ex '| bt | cat'", "debugger script"),
        ("gdb -batch -iex 'guile (display 1)'", "debugger script"),
        ("gdb -batch -x -", "stdin repl"),
        ("gdb ./a.out", "stdin repl"),
        ("gdb -p 1234", "stdin repl"),
        ("gdb", "stdin repl"),
        ("rust-gdb ./a.out", "stdin repl"),
        ("lldb ./a.out", "stdin repl"),
        ("lldb-18 ./a.out", "stdin repl"),
        ("lldb -b -o 'platform shell cat f'", "cat"),
        (
            "lldb --batch --one-line='script print(1)'",
            "debugger script",
        ),
        ("lldb -b -o script", "debugger script"),
    ]);
    assert_all_allow(&[
        "gdb -batch -ex run --args ./a.out",
        "gdb -batch -ex bt -ex 'info registers' ./a.out core",
        "gdb -batch -ex 'shell make test'",
        "gdb -batch -x cmds.gdb ./a.out",
        "gdb --version",
        "lldb -b -o run -- ./a.out",
        "lldb --batch -o 'bt all' -p 1234",
        "rust-gdb -batch -ex bt ./a.out",
    ]);
}

#[test]
fn stdin_repls_need_a_program() {
    assert_rules(&[
        ("irb", "stdin repl"),
        ("irb -r json", "stdin repl"),
        ("pry", "stdin repl"),
        ("pry -e 'x = 1'", "stdin repl"),
        ("lua", "stdin repl"),
        ("lua -", "stdin repl"),
        ("lua -i script.lua", "stdin repl"),
        ("lua5.4 -l socket", "stdin repl"),
        ("luajit", "stdin repl"),
        ("sqlite3", "stdin repl"),
        ("sqlite3 db.sqlite", "stdin repl"),
        ("sqlite3 -cmd .tables db.sqlite", "stdin repl"),
        ("sqlite3 -interactive db.sqlite 'select 1'", "stdin repl"),
        ("echo 'select 1' | sqlite3 db.sqlite", "stdin repl"),
        ("make -f -", "stdin repl"),
        ("make -f-", "stdin repl"),
        ("make --file=- all", "stdin repl"),
        ("gmake --makefile - all", "stdin repl"),
        ("R", "stdin repl"),
        ("R --vanilla", "stdin repl"),
        ("R -f -", "stdin repl"),
        ("R script.R", "stdin repl"),
        ("tclsh", "stdin repl"),
        ("tclsh8.6 -encoding utf-8", "stdin repl"),
        ("guile", "stdin repl"),
        ("guile -l init.scm", "stdin repl"),
        ("ghci", "stdin repl"),
        ("ghci Main.hs", "stdin repl"),
        ("jshell", "stdin repl"),
        ("jshell script.jsh", "stdin repl"),
        ("mise exec -- irb", "stdin repl"),
    ]);
    assert_all_allow(&[
        "irb script.rb",
        "irb --version",
        "pry script.rb",
        "lua script.lua",
        "lua -- script.lua",
        "lua script.lua -i",
        "lua -e 'print(1)'",
        "lua -v",
        "luajit -b in.lua out.o",
        "sqlite3 db.sqlite \"select 1\"",
        "sqlite3 -line db.sqlite .tables",
        "sqlite3 -version",
        "make",
        "make test",
        "make -j4 build",
        "make -f Makefile test",
        "make --file=build.mk all",
        "gmake -C dir test",
        "R -f script.R",
        "R --file=script.R",
        "R -e 'testthat::test_dir(\"tests\")'",
        "R CMD check pkg",
        "R --version",
        "tclsh script.tcl",
        "guile script.scm",
        "guile -s script.scm",
        "guile -c '(display 1)'",
        "ghci -e main Main.hs",
        "jshell --version",
    ]);
}

#[test]
fn hint_stdin_repl_and_debugger_script_name_the_fix() {
    for command in ["irb", "gdb -batch -ex 'python print(1)'"] {
        let reason = tilth_reason_for(command);
        assert!(names_tilth_tool(&reason), "{command:?}: {reason}");
        assert!(reason.contains("-batch"), "{reason}");
        match verdict(command, Hint::Native, Capture::Deny) {
            Verdict::Deny { reason, .. } => {
                assert!(!reason.to_lowercase().contains("tilth"), "{reason}");
                assert!(reason.contains("-batch"), "{reason}");
            }
            Verdict::Allow => panic!("allowed: {command:?}"),
        }
    }
    let repl = tilth_reason_for("irb");
    assert!(repl.contains("reads its commands from stdin"), "{repl}");
    assert!(repl.contains("run that file with Bash"), "{repl}");
}

// Review fixes for PR #466. Each test failed before its fix.

#[test]
fn review1_debugger_checks_the_program_and_its_arguments() {
    assert_rules(&[
        ("gdb -batch -ex run --args cat README.md", "cat"),
        ("gdb -batch -ex run --args bash -c 'cat README.md'", "cat"),
        ("lldb -b -o run /bin/cat -- README.md", "cat"),
        ("lldb -b -o run -- cat README.md", "cat"),
        ("lldb -b -o run -f /bin/cat -- README.md", "cat"),
        ("gdb -batch -ex 'run README.md' /bin/cat", "debugger script"),
        ("gdb -batch -ex 'r < README.md' /bin/cat", "debugger script"),
        (
            "gdb -batch -ex 'set args README.md' -ex run /bin/cat",
            "debugger script",
        ),
        (
            "lldb -b -o 'process launch -- README.md' /bin/cat",
            "debugger script",
        ),
        (
            "lldb -b -o 'settings set target.run-args README.md' -o run /bin/cat",
            "debugger script",
        ),
    ]);
    assert_all_deny(&["gdb -batch -ex run python3"]);
    assert_all_allow(&[
        "gdb -batch -ex run --args ./a.out",
        "gdb -batch -ex run --args ./a.out --flag value",
        "gdb -batch -ex bt ./core",
        "gdb -batch -ex bt ./a.out core",
        "gdb -batch -e ./a.out -ex run",
        "gdb -batch -ex run -ex bt -p 1234",
        "lldb -b -o run -- ./a.out arg",
        "lldb -b -o run ./a.out -- arg",
        "lldb --batch -o 'bt all' -p 1234",
    ]);
}

#[test]
fn review2_debugger_meta_commands_deny() {
    assert_rules(&[
        (
            "gdb -batch -ex 'eval \"shell cat README.md\"'",
            "debugger script",
        ),
        (
            "gdb -batch -ex 'alias xx = shell' -ex 'xx cat README.md'",
            "debugger script",
        ),
        (
            "gdb -batch -ex 'with language c -- shell cat README.md'",
            "debugger script",
        ),
        ("gdb -batch -ex 'make -v; cat README.md'", "debugger script"),
        (
            "lldb -b -o 'command alias xx platform shell' -o 'xx cat README.md'",
            "debugger script",
        ),
        ("lldb -b -o 'command script import x.py'", "debugger script"),
        ("gdb -batch -ex 'define xx' ./a.out", "debugger script"),
        ("gdb -batch -ex 'document xx'", "debugger script"),
        ("gdb -batch -ex 'source cmds.gdb'", "debugger script"),
        ("gdb -batch -ex 'if 1'", "debugger script"),
        ("gdb -batch -ex 'while 1'", "debugger script"),
        (
            "gdb -batch -ex 'compile code system(\"cat f\")'",
            "debugger script",
        ),
        (
            "gdb -batch -ex 'print $_shell(\"cat f\")'",
            "debugger script",
        ),
        ("gdb -batch -ex 'thread apply all shell cat f'", "cat"),
        ("gdb -batch -ex 'taas shell cat f'", "cat"),
        ("gdb -batch -ex 't a a py print(1)'", "debugger script"),
    ]);
    assert_all_allow(&[
        "gdb -batch -ex 'thread apply all bt' ./a.out core",
        "gdb -batch -ex 't a a bt full' -p 1234",
        "gdb -batch -ex 'frame apply all -q p $pc' -p 1234",
        "gdb -batch -ex 'info source' -ex 'info frame' ./a.out core",
        "gdb -batch -ex 'set pagination off' -ex bt ./a.out core",
        "gdb -batch -ex 'print x' -ex 'p/x y' ./a.out core",
        "lldb -b -o 'thread backtrace all' -o 'frame variable' -p 1234",
        "lldb -b -o 'process launch' -- ./a.out",
    ]);
}

#[test]
fn review3_bare_npx_and_npm_exec_start_a_stdin_shell() {
    assert_rules(&[
        ("npx", "shell stdin"),
        ("npm exec", "shell stdin"),
        ("npm x", "shell stdin"),
        ("npx --yes", "shell stdin"),
        ("npx -p cowsay", "shell stdin"),
        ("npx --", "shell stdin"),
        ("npm exec --", "shell stdin"),
    ]);
    assert_all_allow(&[
        "npx -c 'npm test'",
        "npm exec -- jest",
        "npx --yes prettier --check .",
        "npm",
        "npm test",
    ]);
}

#[test]
fn review4_short_option_equals_is_not_part_of_the_value() {
    assert_rules(&[
        ("mise exec -c='cat README.md'", "cat"),
        ("npx -c='cat README.md'", "cat"),
        ("gdb -batch -x=-", "stdin repl"),
        ("uv run -m=json.tool f", "python -m json.tool"),
    ]);
    assert_all_allow(&["uv run -p=3.12 pytest", "mise exec -c='cargo test'"]);
}

#[test]
fn review5_shell_mode_before_the_subcommand_reparses() {
    assert_rules(&[
        ("pnpm -c exec 'cat README.md'", "cat"),
        ("pnpm --shell-mode exec 'cat README.md'", "cat"),
        ("npm -c 'cat README.md' exec", "cat"),
        ("npm --call='cat README.md' x", "cat"),
    ]);
    assert_all_allow(&[
        "pnpm -c exec 'npm test'",
        "pnpm --filter x exec vitest run",
        "npm -c 'npm test' exec",
    ]);
}

#[test]
fn review6_unlisted_runner_option_checks_both_readings() {
    assert_rules(&[
        ("uv run --fork-strategy fewest cat README.md", "cat"),
        ("npx --registry https://r cat README.md", "cat"),
        ("npx -C /tmp cat README.md", "cat"),
        ("uvx -c constraints.txt cat README.md", "cat"),
        ("npx --some-new-option v cat README.md", "cat"),
        ("uv run --some-new-option v cat README.md", "cat"),
        ("uv --some-new-option v run cat README.md", "cat"),
        ("npm --some-new-option v exec cat README.md", "cat"),
        ("npx --some-new-option v -c 'cat README.md'", "cat"),
    ]);
    assert_all_allow(&[
        "uv run --with pytest-xdist pytest -n 4",
        "uv run --frozen pytest -q",
        "uv run --isolated python script.py",
        "uv run --no-sync python -m pytest",
        "npx --yes prettier --check .",
        "npx --prefer-offline jest",
        "pnpm --recursive exec tsc",
        "bundle exec --keep-file-descriptors rake test",
    ]);
}

#[test]
fn review8_sqlite_file_and_shell_commands_deny() {
    assert_rules(&[
        ("sqlite3 db \"select readfile('README.md')\"", "sqlite3 io"),
        ("sqlite3 db \"SELECT ReadFile ('README.md')\"", "sqlite3 io"),
        ("sqlite3 db \"select writefile('x', 'y')\"", "sqlite3 io"),
        (
            "sqlite3 db \"select name, data from fsdir('src')\"",
            "sqlite3 io",
        ),
        ("sqlite3 db \"select load_extension('x')\"", "sqlite3 io"),
        (
            "sqlite3 db \"select edit('', 'cat README.md;')\"",
            "sqlite3 io",
        ),
        ("sqlite3 db '.shell cat README.md'", "sqlite3 io"),
        ("sqlite3 db '.system cat README.md'", "sqlite3 io"),
        ("sqlite3 db '.sh cat README.md'", "sqlite3 io"),
        ("sqlite3 db '.read README.md'", "sqlite3 io"),
        ("sqlite3 db '.import README.md t'", "sqlite3 io"),
        ("sqlite3 db '.output out.txt'", "sqlite3 io"),
        ("sqlite3 db '.once out.txt'", "sqlite3 io"),
        (
            "sqlite3 -cmd '.shell cat README.md' db 'select 1'",
            "sqlite3 io",
        ),
        ("sqlite3 db 'select 1' '.read README.md'", "sqlite3 io"),
        ("sqlite3 -A -x db.sqlar", "sqlite3 io"),
    ]);
    assert_all_allow(&[
        "sqlite3 -json db \"select 1\"",
        "sqlite3 db.sqlite .tables",
        "sqlite3 db '.schema t'",
        "sqlite3 db \"select t.output, t.reader from t\"",
        "sqlite3 db \"select * from t where action = 'edit'\"",
        "sqlite3 db \"select '.shell x'\"",
        "sqlite3 -cmd '.mode json' db 'select 1'",
    ]);
    let repl = tilth_reason_for("sqlite3");
    assert!(repl.contains("plain SQL"), "{repl}");
    assert!(!repl.contains("\"<sql>\""), "{repl}");
}

#[test]
fn review9_make_eval_and_command_line_functions_deny() {
    assert_rules(&[
        ("make --eval='$(info $(file <README.md))'", "make --eval"),
        ("make -E '$(info $(shell cat README.md))'", "make --eval"),
        ("make --eval 'all: ; cat README.md' all", "make --eval"),
        ("make --ev=x", "make --eval"),
        ("make -kE x", "make --eval"),
        ("gmake --eval=x", "make --eval"),
        ("make 'X:=$(shell cat README.md)' test", "make --eval"),
        ("make 'X!=cat README.md' test", "make --eval"),
        ("make 'X:=${file <README.md}'", "make --eval"),
    ]);
    assert_all_allow(&[
        "make -C sub test",
        "make test CFLAGS=-O2",
        "make -j4 build V=1",
        "make -e test",
        "make -C sub -f Makefile.ci test",
    ]);
}

#[test]
fn review_hints_name_the_fix_for_each_new_rule() {
    for command in [
        "gdb -batch -ex 'eval x'",
        "sqlite3 db '.shell ls'",
        "make --eval=x",
    ] {
        let reason = tilth_reason_for(command);
        assert!(names_tilth_tool(&reason), "{command:?}: {reason}");
        match verdict(command, Hint::Native, Capture::Deny) {
            Verdict::Deny { reason, .. } => {
                assert!(!reason.to_lowercase().contains("tilth"), "{reason}");
            }
            Verdict::Allow => panic!("allowed: {command:?}"),
        }
    }
    let debugger = tilth_reason_for("gdb -batch -ex 'eval x'");
    assert!(debugger.contains("--args"), "{debugger}");
    let make = tilth_reason_for("make --eval=x");
    assert!(make.contains("make -f <file>"), "{make}");
}

// Review fixes for PR #466, round 2. Each test failed before its fix.

#[test]
fn round2_gdb_resolves_unique_prefixes_and_denies_unknown_options() {
    assert_rules(&[
        ("gdb -batch -eval 'shell cat README.md'", "cat"),
        ("gdb -batch --init-e 'shell cat README.md'", "cat"),
        ("gdb -batch -ev 'shell cat README.md'", "cat"),
        ("gdb -batch -eiex 'shell cat README.md'", "cat"),
        ("gdb -batch -early-init-eval 'shell cat README.md'", "cat"),
        ("gdb -batch -ex=shell\\ cat\\ README.md", "cat"),
        ("gdb -batch -ex run -ar cat README.md", "cat"),
        ("gdb -batch -ex run --arg cat README.md", "cat"),
        ("gdb -batch-s -ex 'shell cat README.md'", "cat"),
        ("gdb -batch -eix -", "stdin repl"),
        ("gdb -batch -bogus", "unresolved"),
        ("gdb -bat -ex bt ./a.out", "unresolved"),
        ("gdb -batch -co core ./a.out", "unresolved"),
        ("gdb --batch=1 -ex bt ./a.out", "unresolved"),
        ("gdb -batch -cfoo ./a.out", "unresolved"),
        (
            "gdb -batch -tty README.md -ex run /bin/cat",
            "debugger script",
        ),
        ("lldb -b -one-line 'platform shell cat README.md'", "cat"),
        (
            "lldb -b -one-line-before-file 'script print(1)'",
            "debugger script",
        ),
        ("lldb -b -source -", "stdin repl"),
        ("lldb -b --repl", "stdin repl"),
        ("lldb -b -r=swift", "stdin repl"),
    ]);
    assert_all_allow(&[
        "gdb -batch -ex bt ./core",
        "gdb --batch --ex 'info threads' -p 1",
        "gdb -q -batch -ex run --args ./a.out",
        "gdb -nx -batch-silent -ex bt ./a.out core",
        "gdb -batch -ex bt --core=core ./a.out",
        "gdb -batch -ex bt -c core ./a.out",
        "gdb -batch -iex 'set auto-load safe-path /' -ex bt ./a.out core",
        "gdb -v",
        "gdb -h",
        "lldb -batch -o 'bt all' -p 1234",
        "lldb -b -one-line bt ./a.out",
        "lldb --batch --one-line bt --core core ./a.out",
        "lldb -x -b -o bt ./a.out",
        "lldb --repl-language=swift -b -o bt ./a.out",
    ]);
}

#[test]
fn round2_sqlite_denies_hidden_edit_and_sql_built_at_run_time() {
    assert_rules(&[
        (
            "sqlite3 :memory: \"select edit/**/('x','cat README.md')\"",
            "sqlite3 io",
        ),
        (
            "sqlite3 :memory: \"select edit --\n('x','cat README.md')\"",
            "sqlite3 io",
        ),
        ("sqlite3 :memory: 'select \"edit\"(1, 2)'", "sqlite3 io"),
        ("sqlite3 :memory: 'select [EDIT](1, 2)'", "sqlite3 io"),
        (
            "sqlite3 :memory: \"select sha3_query('select ed'||'it(''x'',''cat README.md'')')\"",
            "sqlite3 io",
        ),
        (
            "sqlite3 :memory: \"select sha1_query('select 1')\"",
            "sqlite3 io",
        ),
        ("sqlite3 :memory: 'pragma writable_schema=on'", "sqlite3 io"),
        (
            "sqlite3 :memory: 'select * from sqlite_dbpage'",
            "sqlite3 io",
        ),
        ("sqlite3 :memory: '.selftest'", "sqlite3 io"),
        ("sqlite3 :memory: '.dbconfig defensive off'", "sqlite3 io"),
        ("sqlite3 :memory: '.www'", "sqlite3 io"),
        (
            "sqlite3 :memory: '.session s changeset out.bin'",
            "sqlite3 io",
        ),
        (
            "sqlite3 :memory: \".param set @a \\\"edit('x','cat README.md')\\\"\"",
            "sqlite3 io",
        ),
        (
            "sqlite3 :memory: 'select 1 as \"x'\\''\", edit(1, 2) as \"'\\''\"'",
            "sqlite3 io",
        ),
    ]);
    assert_all_allow(&[
        "sqlite3 app.db \"select count(*) from t\"",
        "sqlite3 app.db \"select * from t where action = 'edit'\"",
        "sqlite3 app.db \"select 'sha3_query(x)' -- edit\"",
        "sqlite3 app.db \"select edited_at from t\"",
        "sqlite3 app.db \"select sha3(x) from t\"",
        "sqlite3 app.db .tables",
    ]);
}

#[test]
fn round2_sqlite_reads_double_dash_options_and_denies_archives_and_init() {
    assert_rules(&[
        ("sqlite3 x.db --Ac README.md", "sqlite3 io"),
        ("sqlite3 x.db --A -c README.md", "sqlite3 io"),
        (
            "sqlite3 x.db \"select sqlar_uncompress(data, sz) from sqlar\"",
            "sqlite3 io",
        ),
        ("sqlite3 x.db \"select sqlar_compress(x)\"", "sqlite3 io"),
        ("sqlite3 -init README.md :memory: 'select 1'", "sqlite3 io"),
        ("sqlite3 --init README.md :memory: 'select 1'", "sqlite3 io"),
        ("sqlite3 --interactive db 'select 1'", "stdin repl"),
    ]);
    assert_all_allow(&[
        "sqlite3 -init /dev/null app.db .tables",
        "sqlite3 --json app.db \"select 1\"",
        "sqlite3 --version",
        "sqlite3 app.db -- 'select 1'",
    ]);
}

#[test]
fn round2_bun_run_uv_tool_run_and_shell_subcommands() {
    assert_rules(&[
        ("bun run cat README.md", "cat"),
        ("bun --cwd . run cat README.md", "cat"),
        ("bun run --silent cat README.md", "cat"),
        ("echo x | bun run -", "bun"),
        (
            "uv tool run python -c 'print(open(\"README.md\").read())'",
            "python",
        ),
        ("uv tool run cat README.md", "cat"),
        ("uv tool run --from x cat README.md", "cat"),
        ("printf 'cat README.md' | mise en", "shell stdin"),
        ("mise en -s bash", "shell stdin"),
        ("poetry shell", "shell stdin"),
    ]);
    assert_all_allow(&[
        "bun run test",
        "bun run build",
        "bun run dev",
        "bun run src/index.ts",
        "bun test",
        "bun install",
        "uv tool run ruff check .",
        "uv tool install ruff",
        "uv tool list",
        "mise shell node@20",
        "mise use node@20",
        "mise run test",
    ]);
}

#[test]
fn round2_pnpm_shell_mode_spellings_and_command_fallback() {
    assert_rules(&[
        ("pnpm --shell-m exec 'cat README.md'", "cat"),
        ("pnpm --shell-mode=true exec 'cat README.md'", "cat"),
        ("pnpm --shell-mode true exec 'cat README.md'", "cat"),
        ("pnpm -c true exec 'cat README.md'", "cat"),
        ("pnpm --shell exec 'cat README.md'", "cat"),
        ("pnpm -rc exec 'cat README.md'", "cat"),
        ("pnpm exec --shell-m 'cat README.md'", "cat"),
        ("pnpm cat README.md", "cat"),
        ("pnpm --filter web cat README.md", "cat"),
        ("pnpm --some-new-option val cat README.md", "cat"),
        ("pnpm recursive exec cat README.md", "cat"),
    ]);
    assert_all_allow(&[
        "pnpm build",
        "pnpm lint --fix",
        "pnpm -r build",
        "pnpm --filter web test",
        "pnpm install",
        "pnpm view react version",
        "pnpm patch foo@1.0.0",
        "pnpm env use --global 20",
        "pnpm --shell-mode=false exec vitest run",
        "pnpm --shell-mode exec 'npm test'",
        "pnpm tsc --noEmit",
        "pnpm",
    ]);
}

#[test]
fn round2_thread_apply_skips_convenience_variable_ids() {
    assert_rules(&[
        (
            "gdb -batch -ex starti -ex 'thread apply $_thread shell cat README.md' /bin/true",
            "cat",
        ),
        (
            "gdb -batch -ex 'thread apply $_gthread 2 py print(1)' -p 1",
            "debugger script",
        ),
    ]);
    assert_all_allow(&["gdb -batch -ex 'thread apply $_thread bt' -p 1"]);
}

#[test]
fn round2_make_checks_environment_assignments() {
    assert_rules(&[
        ("CFLAGS='$(shell cat README.md)' make", "make --eval"),
        (
            "env 'CFLAGS=$(shell cat README.md)' make test",
            "make --eval",
        ),
        ("X='${file <README.md}' gmake all", "make --eval"),
    ]);
    assert_all_allow(&[
        "make CFLAGS=-O2 test",
        "CFLAGS=-O2 make test",
        "CC=clang make",
        "env CFLAGS=-O2 make",
    ]);
}

#[test]
fn round2_more_runners_unwrap() {
    assert_rules(&[
        ("pipenv run cat README.md", "cat"),
        ("pipenv --python 3.12 run cat README.md", "cat"),
        ("pdm run cat README.md", "cat"),
        ("pdm run -p . cat README.md", "cat"),
        ("hatch run cat README.md", "cat"),
        ("hatch run dev:cat README.md", "cat"),
        ("hatch run +py=3.12 cat README.md", "cat"),
        ("hatch -e dev run 'cat README.md'", "cat"),
        ("rye run cat README.md", "cat"),
        ("devbox run cat README.md", "cat"),
        ("devbox run 'cat README.md'", "cat"),
        ("pixi exec cat README.md", "cat"),
        ("pixi run 'cat README.md'", "cat"),
        ("pipenv shell", "shell stdin"),
        ("hatch shell", "shell stdin"),
        ("pixi shell", "shell stdin"),
        ("devbox shell", "shell stdin"),
    ]);
    assert_all_allow(&[
        "pipenv run pytest",
        "pipenv install",
        "pdm run pytest",
        "pdm install",
        "hatch run test",
        "hatch run test:cov",
        "hatch run +py=3.12 test",
        "hatch env create",
        "rye run pytest",
        "devbox run test",
        "devbox run -- make test",
        "pixi run test",
        "pixi run pytest -q",
        "pixi exec ruff check .",
        "pixi install",
    ]);
}

#[test]
fn round3_sqlite_skips_every_option_value() {
    assert_rules(&[
        ("sqlite3 -lookaside 64 8 app.db", "stdin repl"),
        ("sqlite3 -pagecache 64 8 app.db", "stdin repl"),
        ("sqlite3 -nonce x app.db", "stdin repl"),
        ("sqlite3 --lookaside 64 8 app.db", "stdin repl"),
        ("sqlite3 -screenwidth 80 app.db", "stdin repl"),
        ("sqlite3 -threadsafe 1 app.db", "stdin repl"),
        ("sqlite3 -cmdline-edit off app.db", "stdin repl"),
        (
            "sqlite3 -lookaside 64 8 app.db \"select readfile('README.md')\"",
            "sqlite3 io",
        ),
    ]);
    assert_all_allow(&[
        "sqlite3 -lookaside 64 8 app.db \"select 1\"",
        "sqlite3 -pagecache 64 8 app.db .tables",
        "sqlite3 -nonce x app.db \"select 1\"",
        "sqlite3 -cmd 'pragma foreign_keys=on' app.db \"select 1\"",
        // The database operand is not SQL, so its name cannot deny.
        "sqlite3 -lookaside 64 8 edit.db \"select 1\"",
    ]);
}

#[test]
fn round3_bun_repl_reads_stdin() {
    assert_rules(&[
        ("bun repl", "shell stdin"),
        ("bun --cwd web repl", "shell stdin"),
    ]);
    assert_all_allow(&[
        "bun test",
        "bun run build",
        "bun install",
        "bun x prettier --check .",
    ]);
}

#[test]
fn round3_yarn_node_runs_node() {
    assert_rules(&[
        (
            "yarn node -p \"require('fs').readFileSync('README.md','utf8')\"",
            "node",
        ),
        ("yarn node -e 'console.log(1)'", "node"),
        ("yarn --cwd web node -p 1", "node"),
        ("yarn node", "node"),
    ]);
    assert_all_allow(&[
        "yarn test",
        "yarn build",
        "yarn node scripts/build.js",
        "yarn node --version",
        "yarn add -D vitest",
    ]);
}

#[test]
fn round3_npm_explore_runs_shell_source() {
    assert_rules(&[
        ("npm explore foo -- cat README.md", "cat"),
        ("npm explore foo cat README.md", "cat"),
        ("npm explore -- foo cat README.md", "cat"),
        ("npm explore foo -- 'cat README.md'", "cat"),
        ("npm --prefix web explore foo -- cat README.md", "cat"),
        ("npm explore foo", "shell stdin"),
        ("npm explore foo --", "shell stdin"),
        ("npm explore foo -- sh", "shell stdin"),
    ]);
    assert_all_allow(&[
        "npm explore foo -- npm test",
        "npm explore foo -- ls",
        "npm test",
        "npm run build",
    ]);
}

#[test]
fn round3_gdb_edit_runs_the_editor_through_a_shell() {
    assert_rules(&[
        ("gdb -batch -ex 'edit main' ./a.out", "debugger script"),
        ("gdb -batch -ex 'ed main' ./a.out", "debugger script"),
        (
            "gdb -batch -ex 'thread apply all edit' -p 1",
            "debugger script",
        ),
    ]);
    assert_all_allow(&[
        "gdb -batch -ex 'info frame' ./core",
        "gdb -batch -ex 'echo hi' ./a.out",
        "gdb -batch -ex 'enable 1' ./a.out",
    ]);
}

#[test]
fn round3_gdb_aliases_of_denied_commands_deny() {
    assert_rules(&[
        ("gdb -batch -ex guile-repl", "debugger script"),
        ("gdb -batch -ex gr", "debugger script"),
        ("gdb -batch -ex guile-r", "debugger script"),
        ("gdb -batch -ex pi", "debugger script"),
        ("gdb -batch -ex python-interactive", "debugger script"),
        (
            "gdb -batch -ex 'expression int x = 1;' -p 1",
            "debugger script",
        ),
        ("gdb -batch -ex 'expr 1' -p 1", "debugger script"),
    ]);
    assert_all_allow(&[
        "gdb -batch -ex 'info frame' ./core",
        "gdb -batch -ex 'explore x' ./a.out",
        "lldb -b -o 'expr x' ./a.out",
        "lldb -b -o 'expression x' ./a.out",
    ]);
}

#[test]
fn round3_lldb_platform_file_and_process_commands() {
    assert_rules(&[
        (
            "lldb -b -o 'platform file open README.md'",
            "debugger script",
        ),
        (
            "lldb -b -o 'platform file read -o 0 -c 100 0'",
            "debugger script",
        ),
        (
            "lldb -b -o 'platform get-file README.md out'",
            "debugger script",
        ),
        (
            "lldb -b -o 'platform put-file README.md out'",
            "debugger script",
        ),
        (
            "lldb -b -o 'platform process launch -- /bin/cat README.md'",
            "debugger script",
        ),
        ("lldb -b -o 'platform shell cat README.md'", "cat"),
    ]);
    assert_all_allow(&[
        "lldb -b -o 'platform status'",
        "lldb -b -o 'platform list'",
        "lldb -b -o 'platform process list'",
        "lldb -b -o 'platform file-exists README.md'",
    ]);
}

#[test]
fn round3_hatch_env_run_unwraps() {
    assert_rules(&[
        ("hatch env run -- cat README.md", "cat"),
        ("hatch env run -e test -- cat README.md", "cat"),
        ("hatch env run -i py=3.12 -- cat README.md", "cat"),
        ("hatch env run 'cat README.md'", "cat"),
    ]);
    assert_all_allow(&[
        "hatch env run -- pytest",
        "hatch env run -i py=3.12 -x version=9000 test:pytest",
        "hatch env create",
        "hatch env show",
        "hatch env prune",
        "hatch run test",
    ]);
}

#[test]
fn round3_nix_run_checks_the_program_of_the_installable() {
    assert_rules(&[
        ("nix run nixpkgs#bash", "shell stdin"),
        ("nix run nixpkgs#bashInteractive", "shell stdin"),
        ("nix run nixpkgs#bash -- -c 'cat README.md'", "cat"),
        ("nix run nixpkgs#bat -- README.md", "bat"),
        ("nix run nixpkgs#bat README.md", "bat"),
        ("nix run nixpkgs#python3", "python"),
        ("nix run -- nixpkgs#bash", "shell stdin"),
        ("nix run --option sandbox false nixpkgs#bash", "shell stdin"),
        (
            "nix run --new-opt nixpkgs#hello nixpkgs#bash",
            "shell stdin",
        ),
        ("nix run -f '<nixpkgs>' bash", "shell stdin"),
        ("env GIT_PAGER='cat f' nix run nixpkgs#git -- log", "git -c"),
        ("nix run", "unresolved"),
        ("nix run .", "unresolved"),
        ("nix run \"$X\"", "unresolved"),
    ]);
    assert_all_allow(&[
        "nix run nixpkgs#hello",
        "nix run nixpkgs#hello -- --greeting hi",
        "nix run --impure nixpkgs#hello",
        "nix run .#test",
        "nix develop -c cargo test",
        "nix build",
        "nix flake check",
    ]);
}

// `--allow-capture` (issue #470).

/// The rule that denies a command with `--allow-capture`, or `None` when the command is allowed.
fn capture_rule(command: &str, hint: Hint) -> Option<&'static str> {
    match verdict(command, hint, Capture::Allow) {
        Verdict::Deny { rule, .. } => Some(rule),
        Verdict::Allow => None,
    }
}

#[test]
fn capture_corpus_rows_hold_in_both_hint_modes() {
    let allow = capture_rows("must_allow");
    let deny = capture_rows("must_deny");
    assert!(!allow.is_empty() && !deny.is_empty());
    for hint in [Hint::Tilth, Hint::Native] {
        for (command, _) in &allow {
            assert_eq!(capture_rule(command, hint), None, "{hint:?} {command:?}");
        }
        for (command, rule) in &deny {
            let rule = rule.as_deref().expect("must_deny row names a rule");
            assert_eq!(
                capture_rule(command, hint),
                Some(rule),
                "{hint:?} {command:?}"
            );
        }
    }
}

#[test]
fn capture_rows_all_deny_without_the_option() {
    // An allow row that passes in the default mode proves nothing about the option, and a
    // deny row that passes in the default mode means the option tightens a rule.
    for list in ["must_allow", "must_deny"] {
        for (command, _) in capture_rows(list) {
            assert!(
                denies(&command),
                "default mode allows {list} row {command:?}"
            );
        }
    }
}

#[test]
fn capture_lifts_only_the_default_denials_that_the_corpus_lists() {
    let listed: Vec<String> = capture_rows("must_allow")
        .into_iter()
        .map(|(command, _)| command)
        .collect();
    for (command, rule) in rows("must_deny") {
        match capture_rule(&command, Hint::Tilth) {
            Some(got) => assert_eq!(Some(got), rule.as_deref(), "rule changed: {command:?}"),
            None => assert!(
                listed.contains(&command),
                "`--allow-capture` allows {command:?}, and no allow_capture.must_allow row lists it"
            ),
        }
    }
    for command in all_allow_commands() {
        assert_eq!(capture_rule(&command, Hint::Tilth), None, "{command:?}");
    }
}

#[test]
fn capture_reasons_name_the_route_for_command_output() {
    let reason = |command: &str, hint, capture| match verdict(command, hint, capture) {
        Verdict::Deny { reason, .. } => reason,
        Verdict::Allow => panic!("allowed: {command:?}"),
    };
    // Without the option, a blocked capture names the route that stays open: a filter.
    for command in ["just check > check.log 2>&1", "just check 2>&1 | tee log"] {
        let tilth = reason(command, Hint::Tilth, Capture::Deny);
        assert!(tilth.contains("| tail -n 40"), "{tilth}");
        assert!(tilth.contains("tilth_write"), "{tilth}");
        let native = reason(command, Hint::Native, Capture::Deny);
        assert!(native.contains("| tail -n 40"), "{native}");
        assert!(!native.to_lowercase().contains("tilth"), "{native}");
    }
    // With the option, a blocked write names the capture forms that pass.
    for command in ["echo x > f", "printf a | tee f"] {
        let tilth = reason(command, Hint::Tilth, Capture::Allow);
        assert!(tilth.contains("capture its own output"), "{tilth}");
        assert!(tilth.contains("create_file"), "{tilth}");
        assert!(!tilth.contains("tail -n 40"), "{tilth}");
        let native = reason(command, Hint::Native, Capture::Allow);
        assert!(native.contains("capture its own output"), "{native}");
        assert!(native.contains("Write tool"), "{native}");
        assert!(!native.to_lowercase().contains("tilth"), "{native}");
    }
    // The option changes no other reason.
    for command in ["cat f", "grep -rn foo .", "sed -i s/a/b/ f", "irb"] {
        for hint in [Hint::Tilth, Hint::Native] {
            assert_eq!(
                reason(command, hint, Capture::Allow),
                reason(command, hint, Capture::Deny),
                "{hint:?} {command:?}"
            );
        }
    }
}

#[test]
fn capture_every_host_shape_gives_the_claude_verdict() {
    let commands = capture_rows("must_allow")
        .into_iter()
        .chain(capture_rows("must_deny"));
    for (command, _) in commands {
        let run = |host| {
            let mut err = Vec::new();
            run_io(
                host,
                Hint::Tilth,
                Capture::Allow,
                &event(host, &command),
                &mut err,
            )
        };
        let want = run(Host::Claude);
        for host in HOSTS {
            assert_eq!(run(host), want, "{host:?} differs for {command:?}");
        }
    }
}

#[test]
fn capture_flag_maps_to_the_policy() {
    assert_eq!(Capture::default(), Capture::Deny);
    assert_eq!(Capture::from(false), Capture::Deny);
    assert_eq!(Capture::from(true), Capture::Allow);
}

#[test]
fn capture_tracks_pipeline_stages_and_heredoc_tail_output() {
    for command in [
        "go test ./... | # stage note\n tee capture.log",
        "go test ./... <<EOF | tee capture.log\ninput\nEOF",
    ] {
        assert_eq!(capture_rule(command, Hint::Tilth), None, "{command:?}");
    }
    for command in [
        "printf input | # stage note\n tr a-z A-Z > capture.log",
        "printf input <<EOF | tr a-z A-Z > capture.log\ninput\nEOF",
    ] {
        assert_eq!(
            capture_rule(command, Hint::Tilth),
            Some("output redirect"),
            "{command:?}"
        );
    }
}

#[test]
fn capture_tracks_connected_search_stdin_without_authored_provenance() {
    for command in [
        "rg '^MemTotal:' < /proc/meminfo",
        "rg '^MemTotal:' 0< /proc/meminfo",
        "go test ./... <<EOF | rg result > capture.log\ninput\nEOF",
    ] {
        assert_eq!(capture_rule(command, Hint::Tilth), None, "{command:?}");
        assert!(denies(command), "strict mode allows {command:?}");
    }
    for command in [
        "rg pattern /proc/meminfo",
        "rg pattern 1< /proc/meminfo",
        "rg pattern < ./file",
    ] {
        assert!(capture_rule(command, Hint::Tilth).is_some(), "{command:?}");
    }
    assert_eq!(
        capture_rule("rg pattern < /proc/meminfo > capture.log", Hint::Tilth),
        Some("output redirect")
    );
}

#[test]
fn capture_routes_stdin_to_the_redirect_owner() {
    for command in [
        "rg pattern | rg pattern < /dev/null",
        "rg pattern && rg pattern < /dev/null",
        "rg pattern; rg pattern < /dev/null",
    ] {
        assert!(capture_rule(command, Hint::Tilth).is_some(), "{command:?}");
    }
    for command in [
        "rg pattern < /dev/null | rg pattern",
        "rg pattern < /dev/null && go test ./...",
        "rg pattern 0< /dev/null",
    ] {
        assert_eq!(capture_rule(command, Hint::Tilth), None, "{command:?}");
    }
}

#[test]
fn capture_debugger_tracks_only_launched_program_output() {
    for command in [
        "gdb -batch -ex run --args echo input > capture.log",
        "gdb -batch -ex starti --args echo input > capture.log",
        "lldb -b -o run -- echo input > capture.log",
    ] {
        assert_eq!(
            capture_rule(command, Hint::Tilth),
            Some("output redirect"),
            "{command:?}"
        );
    }
    for command in [
        "gdb -batch -ex bt --args echo input > capture.log",
        "gdb -batch -ex run --args go test ./... > capture.log",
    ] {
        assert_eq!(capture_rule(command, Hint::Tilth), None, "{command:?}");
    }
}
