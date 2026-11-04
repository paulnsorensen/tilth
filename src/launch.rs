//! `tilth claude` and `tilth codex`: start the harness with the bundled tilth preamble.
//! `tilth preamble`: print the preamble, or the path of its cache file, for other harnesses.

use std::ffi::OsString;
use std::io;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

const PREAMBLE: &str = include_str!("../prompts/preamble.md");

/// A cache file of another preamble revision that no launch used for this long is removed.
const STALE_AFTER: Duration = Duration::from_hours(30 * 24);

#[derive(Clone, Copy, Debug)]
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

/// Print the bundled preamble. With `path`, write the cache file and print its path.
#[must_use]
pub fn print_preamble(path: bool) -> i32 {
    let out = if path {
        match ensure_preamble_file() {
            Ok(f) => {
                let mut bytes = f.into_os_string().into_encoded_bytes();
                bytes.push(b'\n');
                bytes
            }
            Err(e) => {
                eprintln!("tilth: cannot write the preamble file: {e}");
                return 1;
            }
        }
    } else {
        PREAMBLE.as_bytes().to_vec()
    };
    let mut stdout = io::stdout().lock();
    match io::Write::write_all(&mut stdout, &out).and_then(|()| io::Write::flush(&mut stdout)) {
        Ok(()) => 0,
        // A reader such as `head` closed the pipe early. That is not an error.
        Err(e) if e.kind() == io::ErrorKind::BrokenPipe => 0,
        Err(e) => {
            eprintln!("tilth: cannot write the preamble: {e}");
            1
        }
    }
}

/// Start the harness with the bundled preamble as its system prompt.
/// On Unix, it replaces the process and returns only when exec fails.
/// Elsewhere, it returns the harness exit code.
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
                let path = match utf8_path(&file) {
                    Ok(p) => p,
                    Err(e) => {
                        eprintln!("tilth: {e}");
                        return 1;
                    }
                };
                argv.push("-c".into());
                argv.push(codex_override(path));
            }
        }
    }
    argv.extend(args);
    exec(harness.binary(), &argv)
}

/// True when the caller already sets the system prompt, so the caller wins.
/// Arguments after the first `--` are positional text, so the scan stops there.
fn caller_overrides(harness: Harness, args: &[OsString]) -> bool {
    let args = args
        .iter()
        .position(|a| a == "--")
        .map_or(args, |end| &args[..end]);
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
                let next;
                let value = if matches!(a.as_ref(), "-c" | "--config") {
                    next = args.get(i + 1).map(|n| n.to_string_lossy());
                    next.as_deref()
                } else {
                    a.strip_prefix("--config=").or_else(|| a.strip_prefix("-c"))
                };
                if value.is_some_and(|v| v.starts_with(KEY)) {
                    return true;
                }
            }
            false
        }
    }
}

/// One argv element: `model_instructions_file=<path>` with the path as a TOML string.
fn codex_override(path: &str) -> OsString {
    format!("model_instructions_file={}", toml_edit::Value::from(path)).into()
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

fn ensure_preamble_file() -> io::Result<PathBuf> {
    let dir = cache_dir(std::env::var_os("XDG_CACHE_HOME"), home::home_dir())
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "no cache directory found"))?;
    create_private_dir(&dir)?;
    let name = preamble_name();
    let file = dir.join(&name);
    if std::fs::read(&file).ok().as_deref() == Some(PREAMBLE.as_bytes()) {
        // Mark the file as in use, so another tilth version does not remove it.
        let _ = std::fs::File::options()
            .write(true)
            .open(&file)
            .and_then(|f| f.set_modified(SystemTime::now()));
    } else {
        crate::util::atomic_write_bytes(&file, PREAMBLE.as_bytes())?;
        remove_stale_preambles(&dir, &name);
    }
    Ok(file)
}

/// Remove cache files of other preamble revisions that no launch used for `STALE_AFTER`.
fn remove_stale_preambles(dir: &Path, keep: &str) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let name = entry.file_name();
        let Some(name) = name.to_str() else {
            continue;
        };
        let ours = name
            .strip_prefix("preamble-")
            .is_some_and(|n| n.strip_suffix(".md").is_some());
        if name == keep || !ours {
            continue;
        }
        let stale = entry
            .metadata()
            .and_then(|m| m.modified())
            .ok()
            .and_then(|t| t.elapsed().ok())
            .is_some_and(|age| age > STALE_AFTER);
        if stale {
            let _ = std::fs::remove_file(entry.path());
        }
    }
}

/// Create the cache dir and refuse one that another user can change.
/// Another user who can write the dir can swap the preamble before the harness reads it.
#[cfg(unix)]
fn create_private_dir(dir: &Path) -> io::Result<()> {
    use std::os::unix::fs::{DirBuilderExt, MetadataExt};

    std::fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(dir)?;
    let meta = std::fs::symlink_metadata(dir)?;
    // SAFETY: geteuid has no preconditions and cannot fail.
    let uid = unsafe { libc::geteuid() };
    // Group write is allowed: with umask 002 (Debian and Ubuntu), a dir is 0775 and its group
    // is the user's private group.
    if !meta.is_dir() || meta.uid() != uid || meta.mode() & 0o002 != 0 {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            format!(
                "{} must be a directory that you own and that other users cannot write",
                dir.display()
            ),
        ));
    }
    Ok(())
}

#[cfg(not(unix))]
fn create_private_dir(dir: &Path) -> io::Result<()> {
    std::fs::create_dir_all(dir)
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

fn report_spawn_error(binary: &str, err: &io::Error) -> i32 {
    if err.kind() == io::ErrorKind::NotFound {
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
            &["--system-prompt", "x", "--", "y"],
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
            &["--", "--system-prompt"],
            &["-p", "--", "--system-prompt-file=f"],
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
            &["-c", "model=o3", "-c", "model_instructions_file=/f"],
        ] {
            assert!(caller_overrides(Harness::Codex, &os(form)), "{form:?}");
        }
    }

    #[test]
    fn launch_codex_scan_ignores_other_config_keys() {
        for form in [
            &["-c", "model=o3"][..],
            &["-c"],
            &["--config"],
            &["exec", "model_instructions_file=/a"],
            &["-cmodel=o3"],
            &["exec", "-c", " model_instructions_file=/f"],
            &["exec", "-c", "MODEL_INSTRUCTIONS_FILE=/f"],
            &["-c", "model_instructions_files=/f"],
            &["-c", "profiles.x.model_instructions_file=/f"],
            &["--config=profiles.x.model_instructions_file=/f"],
            &["-c", "x=model_instructions_file=/f"],
            &["exec", "--", "-c", "model_instructions_file=/f"],
            &["-c", "--", "model_instructions_file=/f"],
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
    fn launch_utf8_path_rejects_non_utf8() {
        use std::ffi::OsStr;
        use std::os::unix::ffi::OsStrExt;
        let path = Path::new(OsStr::from_bytes(b"/cache/x\xffy/p.md"));
        let err = utf8_path(path).expect_err("a non-UTF-8 path cannot be a TOML string");
        assert!(err.contains("/cache/x"), "error must name the path: {err}");
    }

    #[test]
    fn launch_utf8_path_accepts_utf8_paths() {
        assert_eq!(utf8_path(Path::new("/a/\u{2713}")), Ok("/a/\u{2713}"));
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
            let el = codex_override(p);
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
}
