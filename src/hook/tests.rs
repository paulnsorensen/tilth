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

/// Parses the embedded command corpus, panicking if the fixture is invalid JSON.
fn corpus() -> Value {
    serde_json::from_str(CORPUS).expect("corpus parses")
}

/// Extracts commands and optional rule names from a named corpus list.
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
    let code = run_io(host, hint, input, &mut err);
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

// Press round 1 attacks.

/// Reports whether a shell command is denied with tilth hints.
fn denies(command: &str) -> bool {
    matches!(verdict(command, Hint::Tilth), Verdict::Deny { .. })
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
        match verdict(command, Hint::Tilth) {
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
        (
            "true && cat <<EOF 2>/dev/null f\nx\nEOF",
            "shell -c unresolved",
        ),
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
        ("sh $X <<<'cat f'", "shell -c unresolved"),
        ("bash \"$S\"", "shell -c unresolved"),
        (
            "bash $(echo /dev/stdin) <<EOF\ncat f\nEOF",
            "shell -c unresolved",
        ),
        ("echo cat f | sh $X", "shell -c unresolved"),
        ("bash -e \"$S\" arg", "shell -c unresolved"),
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
