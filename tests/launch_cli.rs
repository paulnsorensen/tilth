#![cfg(unix)]

use std::ffi::OsStr;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

const PREAMBLE: &[u8] = include_bytes!("../prompts/preamble.md");

struct Sandbox {
    _root: tempfile::TempDir,
    home: PathBuf,
    xdg: PathBuf,
    bin: PathBuf,
    log: PathBuf,
}

impl Sandbox {
    fn new() -> Self {
        let root = tempfile::tempdir().unwrap();
        let home = root.path().join("home");
        let xdg = root.path().join("xdg");
        let bin = root.path().join("bin");
        for d in [&home, &bin] {
            fs::create_dir_all(d).unwrap();
        }
        let log = root.path().join("argv.log");
        Self {
            _root: root,
            home,
            xdg,
            bin,
            log,
        }
    }

    fn script(&self, name: &str, body: &str) {
        let path = self.bin.join(name);
        fs::write(&path, format!("#!/bin/sh\n{body}\n")).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
    }

    /// A stub harness that logs its argv and exits with `exit`.
    fn fake(&self, name: &str, exit: i32) {
        self.script(
            name,
            &format!(
                "for a in \"$@\"; do printf '%s\\n' \"$a\" >> \"$ARGV_LOG\"; done\nexit {exit}"
            ),
        );
    }

    /// The sandbox environment on a command: stub PATH, private HOME, explicit cache dir.
    fn env<'a>(&self, cmd: &'a mut Command) -> &'a mut Command {
        cmd.env("HOME", &self.home)
            .env("PATH", &self.bin)
            .env("ARGV_LOG", &self.log)
            .env("XDG_CACHE_HOME", &self.xdg)
    }

    fn cmd(&self, harness: &str, args: &[&str]) -> Command {
        let mut cmd = Command::new(env!("CARGO_BIN_EXE_tilth"));
        cmd.arg(harness).args(args);
        self.env(&mut cmd);
        cmd
    }

    fn argv(&self) -> Vec<String> {
        fs::read_to_string(&self.log)
            .unwrap_or_default()
            .lines()
            .map(str::to_owned)
            .collect()
    }

    fn preamble_file(dir: &Path) -> PathBuf {
        let mut found: Vec<PathBuf> = fs::read_dir(dir)
            .unwrap()
            .map(|e| e.unwrap().path())
            .collect();
        assert_eq!(
            found.len(),
            1,
            "expected one cache file in {}: {found:?}",
            dir.display()
        );
        found.pop().unwrap()
    }
}

fn is_preamble_name(p: &Path) -> bool {
    let name = p.file_name().and_then(OsStr::to_str).unwrap_or("");
    name.starts_with("preamble-")
        && Path::new(name)
            .extension()
            .is_some_and(|e| e.eq_ignore_ascii_case("md"))
}

/// The argv a harness must see: the preamble flag for `harness`, then `args` unchanged.
fn launch_expected(harness: &str, file: &Path, args: &[&str]) -> Vec<String> {
    let file = file.to_str().unwrap();
    let mut want: Vec<String> = if harness == "claude" {
        vec!["--system-prompt-file".into(), file.into()]
    } else {
        vec![
            "-c".into(),
            format!("model_instructions_file={}", toml_edit::Value::from(file)),
        ]
    };
    want.extend(args.iter().map(|s| (*s).to_owned()));
    want
}

fn launch_assert_clean_failure(out: &Output, sb: &Sandbox) {
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(!out.status.success(), "expected failure: {out:?}");
    assert_ne!(out.status.code(), Some(101), "panic: {stderr}");
    assert!(!stderr.contains("panicked"), "{stderr}");
    assert!(stderr.contains("tilth"), "{stderr}");
    assert!(
        !sb.log.exists(),
        "harness must not run after a cache failure"
    );
}

#[test]
fn launch_prepends_preamble() {
    let cases: &[(&str, &[&str])] = &[
        ("claude", &["-p", "hi"]),
        ("codex", &["exec", "hi"]),
        ("claude", &["-p", "explain --system-prompt please"]),
        ("claude", &["--append-system-prompt", "x"]),
        ("codex", &["-c", "other_key=x"]),
        ("codex", &["exec", "model_instructions_file=/f"]),
        (
            "claude",
            &["--version", "-h", "--help", "--json", "--scope", "src"],
        ),
        ("codex", &["--mcp", "--budget", "5", "--edit"]),
        (
            "claude",
            &[
                "",
                "a b  c",
                "h\u{e9}llo \u{2713} \u{1f9c0}",
                "-x",
                "$HOME",
                "*",
                "it's \"quoted\"",
            ],
        ),
    ];
    for (harness, args) in cases {
        let sb = Sandbox::new();
        sb.fake(harness, 0);
        let out = sb.cmd(harness, args).output().unwrap();
        assert!(out.status.success(), "{harness} {args:?}: {out:?}");
        let file = Sandbox::preamble_file(&sb.xdg.join("tilth"));
        assert_eq!(
            sb.argv(),
            launch_expected(harness, &file, args),
            "{harness} {args:?}"
        );
    }
}

#[test]
fn launch_dashdash_placements_pass_through_unchanged() {
    let cases: &[&[&str]] = &[
        &["--"],
        &["--", "--", "x"],
        &["--", "-p", "hi"],
        &["--", "claude", "--"],
        &["x", "--", "y"],
        &["claude", "codex", "--", "y"],
        &["", "--", ""],
        &["-p", "--", "claude", "--", "x"],
    ];
    for harness in ["claude", "codex"] {
        for args in cases {
            let sb = Sandbox::new();
            sb.fake(harness, 0);
            let out = sb.cmd(harness, args).output().unwrap();
            assert!(out.status.success(), "{harness} {args:?}: {out:?}");
            let file = Sandbox::preamble_file(&sb.xdg.join("tilth"));
            assert_eq!(
                sb.argv(),
                launch_expected(harness, &file, args),
                "{harness} {args:?}"
            );
        }
    }
}

#[test]
fn launch_top_level_flags_before_a_harness_are_a_usage_error() {
    let cases: &[(&str, &[&str])] = &[
        ("claude", &["--json", "claude", "-p", "hi"]),
        ("codex", &["--mcp", "codex", "exec", "hi"]),
    ];
    for (harness, argv) in cases {
        let sb = Sandbox::new();
        sb.fake(harness, 0);
        let mut cmd = Command::new(env!("CARGO_BIN_EXE_tilth"));
        cmd.args(*argv);
        let out = sb.env(&mut cmd).output().unwrap();
        assert_eq!(out.status.code(), Some(2), "{argv:?}: {out:?}");
        assert!(!sb.log.exists(), "{argv:?}: the harness must not run");
    }
}

#[test]
fn launch_dashdash_query_still_searches_the_word() {
    let sb = Sandbox::new();
    sb.fake("claude", 0);
    sb.fake("codex", 0);
    fs::write(
        sb.home.join("probe.rs"),
        "fn claude() {}\nfn codex() {}\nfn launch_marker() {}\n",
    )
    .unwrap();
    let scope = sb.home.to_str().unwrap();
    for argv in [
        &["--scope", scope, "--", "claude"][..],
        &["--scope", scope, "--", "codex"][..],
        &["--scope", scope, "launch_marker"][..],
    ] {
        let mut cmd = Command::new(env!("CARGO_BIN_EXE_tilth"));
        cmd.args(argv);
        let out = sb.env(&mut cmd).output().unwrap();
        let stdout = String::from_utf8_lossy(&out.stdout);
        assert_eq!(out.status.code(), Some(0), "{argv:?}: {out:?}");
        assert!(stdout.contains("# Search"), "{argv:?}: {stdout}");
        assert!(!sb.log.exists(), "{argv:?}: the harness must not run");
    }
}

#[test]
fn launch_caller_wins() {
    let cases: &[(&str, &[&str])] = &[
        ("claude", &["--system-prompt", "x", "-p"]),
        ("claude", &["--system-prompt-file=/f"]),
        ("codex", &["-c", "model_instructions_file=\"/f\"", "exec"]),
        (
            "codex",
            &["exec", "--config=model_instructions_file=/f", "hi"],
        ),
        ("codex", &["exec", "-cmodel_instructions_file=/f"]),
    ];
    for (harness, args) in cases {
        let sb = Sandbox::new();
        sb.fake(harness, 0);
        let out = sb.cmd(harness, args).output().unwrap();
        assert!(out.status.success(), "{harness} {args:?}: {out:?}");
        assert_eq!(sb.argv(), *args);
        assert!(!sb.xdg.exists(), "no cache dir for {args:?}");
        assert!(!sb.home.join(".cache").exists());
    }
}

#[test]
fn launch_cache_file_matches_bundled_preamble() {
    let sb = Sandbox::new();
    sb.fake("claude", 0);
    assert!(sb.cmd("claude", &["-p"]).output().unwrap().status.success());
    let file = Sandbox::preamble_file(&sb.xdg.join("tilth"));
    assert!(is_preamble_name(&file), "{file:?}");
    assert_eq!(fs::read(&file).unwrap(), PREAMBLE);

    fs::write(&file, b"junk").unwrap();
    assert!(sb.cmd("claude", &["-p"]).output().unwrap().status.success());
    assert_eq!(fs::read(&file).unwrap(), PREAMBLE, "junk must be repaired");

    let out = sb
        .cmd("claude", &["-p"])
        .env_remove("XDG_CACHE_HOME")
        .output()
        .unwrap();
    assert!(out.status.success(), "{out:?}");
    let fallback = Sandbox::preamble_file(&sb.home.join(".cache/tilth"));
    assert!(is_preamble_name(&fallback), "{fallback:?}");
    assert_eq!(fs::read(&fallback).unwrap(), PREAMBLE);
}

#[test]
fn launch_leaves_host_configs_untouched() {
    let sb = Sandbox::new();
    sb.fake("claude", 0);
    sb.fake("codex", 0);
    fs::create_dir_all(sb.home.join(".claude")).unwrap();
    fs::create_dir_all(sb.home.join(".codex")).unwrap();
    let seeds = [
        (sb.home.join(".claude.json"), "{\"sentinel\":1}"),
        (sb.home.join(".claude/settings.json"), "{\"s\":2}"),
        (sb.home.join(".codex/config.toml"), "s = 3\n"),
    ];
    for (p, c) in &seeds {
        fs::write(p, c).unwrap();
    }
    assert!(sb.cmd("claude", &["-p"]).output().unwrap().status.success());
    assert!(sb
        .cmd("codex", &["exec"])
        .output()
        .unwrap()
        .status
        .success());
    for (p, c) in &seeds {
        assert_eq!(fs::read_to_string(p).unwrap(), *c, "{p:?}");
    }
    for d in [".claude", ".codex"] {
        let n = fs::read_dir(sb.home.join(d)).unwrap().count();
        assert_eq!(n, 1, "new file under {d}");
    }
}

#[test]
fn launch_missing_binary_names_it() {
    for harness in ["claude", "codex"] {
        let sb = Sandbox::new();
        let out = sb.cmd(harness, &["x"]).output().unwrap();
        assert_eq!(out.status.code(), Some(127));
        let stderr = String::from_utf8_lossy(&out.stderr);
        assert!(stderr.contains(harness), "{stderr}");
    }
}

#[test]
fn launch_relative_or_empty_xdg_falls_back_to_home_cache() {
    for val in ["rel/dir", ""] {
        let sb = Sandbox::new();
        sb.fake("claude", 0);
        let work = sb.home.parent().unwrap().join("work");
        fs::create_dir_all(&work).unwrap();
        let out = sb
            .cmd("claude", &["-p"])
            .env("XDG_CACHE_HOME", val)
            .current_dir(&work)
            .output()
            .unwrap();
        assert!(out.status.success(), "{val:?}: {out:?}");
        let file = Sandbox::preamble_file(&sb.home.join(".cache/tilth"));
        assert_eq!(fs::read(&file).unwrap(), PREAMBLE);
        assert_eq!(
            fs::read_dir(&work).unwrap().count(),
            0,
            "relative XDG {val:?} must not create a dir under cwd"
        );
    }
}

#[test]
fn launch_cache_dir_is_a_file_fails_cleanly() {
    for harness in ["claude", "codex"] {
        let sb = Sandbox::new();
        sb.fake(harness, 0);
        fs::write(&sb.xdg, b"i am a file").unwrap();
        let out = sb.cmd(harness, &["-p"]).output().unwrap();
        launch_assert_clean_failure(&out, &sb);
    }
}

#[test]
fn launch_unwritable_cache_fails_cleanly() {
    for harness in ["claude", "codex"] {
        let sb = Sandbox::new();
        sb.fake(harness, 0);
        fs::create_dir_all(&sb.xdg).unwrap();
        fs::set_permissions(&sb.xdg, fs::Permissions::from_mode(0o555)).unwrap();
        if fs::write(sb.xdg.join("probe"), b"x").is_ok() {
            fs::set_permissions(&sb.xdg, fs::Permissions::from_mode(0o755)).unwrap();
            continue; // running as root: permission bits do not bind
        }
        let out = sb.cmd(harness, &["-p"]).output().unwrap();
        fs::set_permissions(&sb.xdg, fs::Permissions::from_mode(0o755)).unwrap();
        launch_assert_clean_failure(&out, &sb);
    }
}

#[test]
fn launch_cache_file_path_blocked_by_directory_is_clean() {
    let sb = Sandbox::new();
    sb.fake("claude", 0);
    assert!(sb.cmd("claude", &["-p"]).output().unwrap().status.success());
    let file = Sandbox::preamble_file(&sb.xdg.join("tilth"));
    fs::remove_file(&file).unwrap();
    fs::create_dir(&file).unwrap();
    fs::remove_file(&sb.log).unwrap();
    let out = sb.cmd("claude", &["-p"]).output().unwrap();
    launch_assert_clean_failure(&out, &sb);
}

#[test]
fn launch_cache_name_is_stable_and_content_addressed_shape() {
    let sb = Sandbox::new();
    sb.fake("claude", 0);
    assert!(sb.cmd("claude", &["-p"]).output().unwrap().status.success());
    let first = Sandbox::preamble_file(&sb.xdg.join("tilth"));
    assert!(sb.cmd("codex", &["exec"]).output().is_ok());
    sb.fake("codex", 0);
    assert!(sb
        .cmd("codex", &["exec"])
        .output()
        .unwrap()
        .status
        .success());
    let second = Sandbox::preamble_file(&sb.xdg.join("tilth"));
    assert_eq!(first, second, "same bytes must give the same file name");
    let name = first.file_name().unwrap().to_str().unwrap();
    let hex = name
        .strip_prefix("preamble-")
        .and_then(|n| n.strip_suffix(".md"))
        .unwrap_or("");
    assert_eq!(hex.len(), 16, "{name}");
    assert!(hex.bytes().all(|b| b.is_ascii_hexdigit()), "{name}");
}

#[test]
fn launch_concurrent_launches_never_see_a_partial_preamble() {
    let sb = Sandbox::new();
    sb.script("claude", "/usr/bin/cat \"$2\" > \"$OUT_DIR/$$\"");
    let first = sb
        .cmd("claude", &["-p"])
        .env("OUT_DIR", &sb.home)
        .output()
        .unwrap();
    assert!(first.status.success(), "{first:?}");
    let file = Sandbox::preamble_file(&sb.xdg.join("tilth"));
    for round in 0..6 {
        let out_dir = sb.home.join(format!("out{round}"));
        fs::create_dir_all(&out_dir).unwrap();
        fs::remove_file(&file).unwrap();
        std::thread::scope(|s| {
            let hs: Vec<_> = (0..16)
                .map(|_| {
                    s.spawn(|| {
                        sb.cmd("claude", &["-p"])
                            .env("OUT_DIR", &out_dir)
                            .output()
                            .unwrap()
                    })
                })
                .collect();
            for h in hs {
                let out = h.join().unwrap();
                assert!(out.status.success(), "{out:?}");
            }
        });
        let copies: Vec<_> = fs::read_dir(&out_dir).unwrap().collect();
        assert_eq!(copies.len(), 16, "round {round}");
        for c in copies {
            let p = c.unwrap().path();
            assert_eq!(fs::read(&p).unwrap(), PREAMBLE, "torn read in {p:?}");
        }
        assert_eq!(fs::read(&file).unwrap(), PREAMBLE);
    }
}

#[test]
fn launch_cache_path_with_space_and_quote_is_one_argv_element() {
    for harness in ["claude", "codex"] {
        let sb = Sandbox::new();
        sb.fake(harness, 0);
        let xdg = sb.home.parent().unwrap().join("a b\"c\\d");
        let out = sb
            .cmd(harness, &["x"])
            .env("XDG_CACHE_HOME", &xdg)
            .output()
            .unwrap();
        assert!(out.status.success(), "{out:?}");
        let file = Sandbox::preamble_file(&xdg.join("tilth"));
        assert_eq!(sb.argv(), launch_expected(harness, &file, &["x"]));
    }
}

#[test]
fn launch_exit_codes_and_signals_propagate() {
    use std::os::unix::process::ExitStatusExt;
    for harness in ["claude", "codex"] {
        for code in [0, 1, 2, 3, 7, 126, 255] {
            let sb = Sandbox::new();
            sb.fake(harness, code);
            let out = sb.cmd(harness, &["-p"]).output().unwrap();
            assert_eq!(out.status.code(), Some(code), "{harness}");
        }
        let sb = Sandbox::new();
        sb.script(harness, "kill -TERM $$");
        let out = sb.cmd(harness, &["-p"]).output().unwrap();
        assert_eq!(out.status.signal(), Some(15), "{harness}: {:?}", out.status);
    }
}

#[test]
fn launch_stdin_stdout_stderr_pass_through() {
    use std::io::Write;
    use std::process::Stdio;
    let sb = Sandbox::new();
    sb.script(
        "claude",
        "/usr/bin/cat\necho \"argc=$#\"\necho err-line >&2",
    );
    let mut child = sb
        .cmd("claude", &[])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all("h\u{e9}llo\nworld\n".as_bytes())
        .unwrap();
    let out = child.wait_with_output().unwrap();
    assert!(out.status.success(), "{out:?}");
    assert_eq!(
        String::from_utf8_lossy(&out.stdout),
        "h\u{e9}llo\nworld\nargc=2\n"
    );
    assert_eq!(String::from_utf8_lossy(&out.stderr), "err-line\n");
}

#[test]
fn launch_non_executable_harness_fails_cleanly_naming_it() {
    for harness in ["claude", "codex"] {
        let sb = Sandbox::new();
        let path = sb.bin.join(harness);
        fs::write(&path, "#!/bin/sh\nexit 0\n").unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
        let out = sb.cmd(harness, &["-p"]).output().unwrap();
        let stderr = String::from_utf8_lossy(&out.stderr);
        assert!(!out.status.success(), "{out:?}");
        assert_ne!(out.status.code(), Some(101), "{stderr}");
        assert!(stderr.contains(harness), "{stderr}");
    }
}

#[test]
fn launch_non_utf8_codex_cache_path_exits_1_before_exec() {
    use std::os::unix::ffi::OsStrExt;
    let sb = Sandbox::new();
    sb.fake("codex", 0);
    sb.fake("claude", 0);
    let bad = sb.home.parent().unwrap().join(OsStr::from_bytes(b"x\xffy"));
    let out = sb
        .cmd("codex", &["exec"])
        .env("XDG_CACHE_HOME", &bad)
        .output()
        .unwrap();
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert_eq!(out.status.code(), Some(1), "{stderr}");
    assert!(stderr.contains("UTF-8"), "{stderr}");
    assert!(!sb.log.exists(), "codex must not run");
    // Claude takes the path as raw argv bytes, so it still runs.
    let out = sb
        .cmd("claude", &["-p"])
        .env("XDG_CACHE_HOME", &bad)
        .output()
        .unwrap();
    assert!(out.status.success(), "{out:?}");
}
