//! `tilth claude` and `tilth codex`: start the harness with the bundled tilth preamble.

use std::ffi::OsString;
use std::path::{Path, PathBuf};

const PREAMBLE: &str = include_str!("../prompts/preamble.md");

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Harness {
    Claude,
    Codex,
}

impl Harness {
    fn binary(self) -> &'static str {
        match self {
            Self::Claude => "claude",
            Self::Codex => "codex",
        }
    }
}

/// Start the harness with the preamble prepended. Returns an exit code only on failure.
#[must_use]
pub fn run(harness: Harness, args: Vec<OsString>) -> i32 {
    let mut argv: Vec<OsString> = Vec::new();
    if !caller_overrides(harness, &args) {
        let file = match ensure_preamble_file() {
            Ok(f) => f,
            Err(e) => {
                eprintln!("tilth: cannot write the preamble file: {e}");
                return 1;
            }
        };
        match harness {
            Harness::Claude => {
                argv.push("--system-prompt-file".into());
                argv.push(file.into_os_string());
            }
            Harness::Codex => {
                if let Err(e) = utf8_path(&file) {
                    eprintln!("tilth: {e}");
                    return 1;
                }
                argv.push("-c".into());
                argv.push(codex_override(&file));
            }
        }
    }
    argv.extend(args);
    exec(harness.binary(), &argv)
}

/// True when the caller already sets the system prompt, so the caller wins.
fn caller_overrides(harness: Harness, args: &[OsString]) -> bool {
    match harness {
        Harness::Claude => args.iter().any(|a| {
            let a = a.to_string_lossy();
            matches!(a.as_ref(), "--system-prompt" | "--system-prompt-file")
                || a.starts_with("--system-prompt=")
                || a.starts_with("--system-prompt-file=")
        }),
        Harness::Codex => {
            const KEY: &str = "model_instructions_file=";
            for (i, a) in args.iter().enumerate() {
                let a = a.to_string_lossy();
                if let Some(rest) = a.strip_prefix("--config=") {
                    if rest.starts_with(KEY) {
                        return true;
                    }
                } else if matches!(a.as_ref(), "-c" | "--config") {
                    let next = args.get(i + 1).map(|n| n.to_string_lossy());
                    if next.is_some_and(|n| n.starts_with(KEY)) {
                        return true;
                    }
                } else if let Some(rest) = a.strip_prefix("-c") {
                    if rest.starts_with(KEY) {
                        return true;
                    }
                }
            }
            false
        }
    }
}

/// One argv element: `model_instructions_file=<path>` with the path as a TOML string.
/// The caller checks the path with `utf8_path` first; TOML cannot hold other bytes.
fn codex_override(file: &Path) -> OsString {
    format!(
        "model_instructions_file={}",
        toml_edit::Value::from(file.to_string_lossy().as_ref())
    )
    .into()
}

/// The path as UTF-8, or an error message that names it.
fn utf8_path(file: &Path) -> Result<&str, String> {
    file.to_str().ok_or_else(|| {
        format!(
            "the cache path {} is not valid UTF-8, so codex cannot take it in a TOML override",
            file.display()
        )
    })
}

fn cache_dir(xdg: Option<OsString>, home: Option<PathBuf>) -> Option<PathBuf> {
    let base = xdg
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
        .or_else(|| home.map(|h| h.join(".cache")))?;
    Some(base.join("tilth"))
}

fn preamble_name() -> String {
    let hash = twox_hash::XxHash3_64::oneshot(PREAMBLE.as_bytes());
    format!("preamble-{hash:016x}.md")
}

fn ensure_preamble_file() -> std::io::Result<PathBuf> {
    let dir = cache_dir(std::env::var_os("XDG_CACHE_HOME"), home::home_dir()).ok_or_else(|| {
        std::io::Error::new(std::io::ErrorKind::NotFound, "no cache directory found")
    })?;
    std::fs::create_dir_all(&dir)?;
    let file = dir.join(preamble_name());
    if std::fs::read(&file).ok().as_deref() != Some(PREAMBLE.as_bytes()) {
        crate::util::atomic_write_bytes(&file, PREAMBLE.as_bytes())?;
    }
    Ok(file)
}

#[cfg(unix)]
fn exec(binary: &str, argv: &[OsString]) -> i32 {
    use std::os::unix::process::CommandExt;

    let err = std::process::Command::new(binary).args(argv).exec();
    report_spawn_error(binary, &err)
}

#[cfg(not(unix))]
fn exec(binary: &str, argv: &[OsString]) -> i32 {
    match std::process::Command::new(binary).args(argv).status() {
        Ok(status) => status.code().unwrap_or(1),
        Err(err) => report_spawn_error(binary, &err),
    }
}

fn report_spawn_error(binary: &str, err: &std::io::Error) -> i32 {
    if err.kind() == std::io::ErrorKind::NotFound {
        eprintln!("tilth: cannot find `{binary}` on PATH: {err}");
        127
    } else {
        eprintln!("tilth: cannot start `{binary}`: {err}");
        1
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn os(args: &[&str]) -> Vec<OsString> {
        args.iter().map(OsString::from).collect()
    }

    #[test]
    fn launch_claude_scan_detects_system_prompt_forms() {
        for form in [
            &["--system-prompt", "x"][..],
            &["--system-prompt=x"],
            &["-p", "--system-prompt-file", "f"],
            &["--system-prompt-file=f"],
        ] {
            assert!(caller_overrides(Harness::Claude, &os(form)), "{form:?}");
        }
    }

    #[test]
    fn launch_claude_scan_ignores_append_and_other_args() {
        for form in [
            &["--append-system-prompt", "x"][..],
            &["--append-system-prompt=x"],
            &["--append-system-prompt-file", "F"],
            &["--system-prompt-filex=v"],
            &["--SYSTEM-PROMPT", "x"],
            &["-p", "explain --system-prompt please"],
            &["-p", "hi"],
            &[],
        ] {
            assert!(!caller_overrides(Harness::Claude, &os(form)), "{form:?}");
        }
    }

    #[test]
    fn launch_codex_scan_detects_config_forms() {
        for form in [
            &["-c", "model_instructions_file=\"/a\""][..],
            &["--config", "model_instructions_file=/a"],
            &["--config=model_instructions_file=/a"],
            &["-cmodel_instructions_file=/a"],
            &["exec", "hi", "-c", "model_instructions_file=/f"],
        ] {
            assert!(caller_overrides(Harness::Codex, &os(form)), "{form:?}");
        }
    }

    #[test]
    fn launch_codex_scan_ignores_other_config_keys() {
        for form in [
            &["-c", "model=o3"][..],
            &["-c"],
            &["exec", "model_instructions_file=/a"],
            &["-cmodel=o3"],
            &["exec", "-c", " model_instructions_file=/f"],
            &["exec", "-c", "MODEL_INSTRUCTIONS_FILE=/f"],
            &["-c", "model_instructions_files=/f"],
        ] {
            assert!(!caller_overrides(Harness::Codex, &os(form)), "{form:?}");
        }
    }

    #[test]
    fn launch_cache_dir_prefers_absolute_xdg() {
        let home = Some(PathBuf::from("/h"));
        assert_eq!(
            cache_dir(Some("/x".into()), home.clone()),
            Some(PathBuf::from("/x/tilth"))
        );
        for bad in [Some(OsString::new()), Some("rel".into()), None] {
            assert_eq!(
                cache_dir(bad, home.clone()),
                Some(PathBuf::from("/h/.cache/tilth"))
            );
        }
    }

    #[cfg(unix)]
    #[test]
    fn launch_toml_override_keeps_non_utf8_path_bytes() {
        use std::ffi::OsStr;
        use std::os::unix::ffi::OsStrExt;
        let path = Path::new(OsStr::from_bytes(b"/cache/x\xffy/p.md"));
        let err = utf8_path(path).expect_err("a non-UTF-8 path cannot be a TOML string");
        assert!(err.contains("/cache/x"), "error must name the path: {err}");
        assert!(!codex_override(Path::new("/cache/ok/p.md"))
            .to_string_lossy()
            .contains('\u{fffd}'));
    }

    #[test]
    fn launch_preamble_name_is_hash_of_bundled_bytes() {
        let want = format!(
            "preamble-{:016x}.md",
            twox_hash::XxHash3_64::oneshot(PREAMBLE.as_bytes())
        );
        assert_eq!(preamble_name(), want);
        let other = twox_hash::XxHash3_64::oneshot(b"other bytes");
        assert_ne!(preamble_name(), format!("preamble-{other:016x}.md"));
    }

    #[test]
    fn launch_scan_codex_ignores_dotted_key_and_attached_other_key() {
        for form in [
            &["-c", "profiles.x.model_instructions_file=/f"][..],
            &["--config=profiles.x.model_instructions_file=/f"],
            &["-c", "x=model_instructions_file=/f"],
            &["--config"],
        ] {
            assert!(!caller_overrides(Harness::Codex, &os(form)), "{form:?}");
        }
    }

    #[test]
    fn launch_toml_override_round_trips_through_a_toml_parser() {
        let mut inputs: Vec<String> = (0u32..=0x1f)
            .chain([0x7f, 0x80, 0x85, 0x9f, 0x2028, 0x2029, 0xfeff, 0x0010_ffff])
            .map(|c| format!("/a{}b", char::from_u32(c).unwrap()))
            .collect();
        inputs.extend(
            [
                "\\\"",
                "\"\\",
                "\\\\\"\"\\",
                "\\n",
                "\\u0041",
                "\"\"\"",
                "'''",
                "\u{1}\"\\\n\u{7f}\u{2028}",
                "",
            ]
            .map(String::from),
        );
        let mut failures = Vec::new();
        for p in &inputs {
            let el = codex_override(Path::new(p));
            let text = el.to_str().unwrap().to_owned();
            match text.parse::<toml_edit::DocumentMut>() {
                Ok(doc) => {
                    let got = doc["model_instructions_file"].as_str().map(str::to_owned);
                    if got.as_deref() != Some(p.as_str()) {
                        failures.push(format!("{p:?} -> {text:?} -> {got:?}"));
                    }
                }
                Err(e) => failures.push(format!("{p:?} -> {text:?} invalid TOML: {e}")),
            }
        }
        assert!(failures.is_empty(), "{failures:#?}");
    }
    #[test]
    fn launch_utf8_path_accepts_utf8_paths() {
        assert_eq!(utf8_path(Path::new("/a/\u{2713}")), Ok("/a/\u{2713}"));
    }
}
