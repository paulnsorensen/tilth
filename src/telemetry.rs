//! Content-free search telemetry: versioned JSONL records written to a
//! size-bounded, rotating log under `$XDG_STATE_HOME/tilth/telemetry/`
//! (falls back to `~/.local/state/tilth/telemetry/` when unset).
//!
//! Records never carry source text or snippets — only routes, counts, and
//! state labels — so the sink is safe to enable unconditionally.
//!
//! Not yet wired into `Services` — that's a follow-on curd, so the public
//! surface here is unused crate-wide for now.
#![allow(dead_code)]

use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::OnceLock;
use std::time::{SystemTime, UNIX_EPOCH};

use serde::Serialize;

/// Schema version for [`SearchTelemetryRecord`]. Bump when fields change
/// in a way that breaks readers of prior records.
const SCHEMA_VERSION: u32 = 1;

/// Default per-file size cap before rotation.
const DEFAULT_MAX_BYTES: u64 = 5 * 1024 * 1024;

/// Number of rotated files retained alongside the current one; the oldest
/// rotated file is deleted once this cap is exceeded, bounding total
/// telemetry directory size to roughly `(MAX_ROTATED_FILES + 1) * max_bytes`.
const MAX_ROTATED_FILES: usize = 3;

const CURRENT_FILE_NAME: &str = "current.jsonl";

/// One search-tool invocation, content-free by construction.
#[derive(Serialize)]
pub(crate) struct SearchTelemetryRecord {
    pub verb: String,
    pub version: u32,
    pub route: String,
    pub routes_tried: Vec<String>,
    /// True on a cold first call for the session; false on a recovery /
    /// retry call following a prior route.
    pub first_call: bool,
    pub latency_ms: u64,
    pub result_tokens: u64,
    pub partial: bool,
    pub timeout: bool,
    pub dependency_coverage: f64,
    pub shard_state: String,
    pub client: String,
    pub worktree: String,
}

/// Appends content-free [`SearchTelemetryRecord`]s to a rotating JSONL log.
///
/// Owned by `Services`; writes are best-effort — failures are surfaced via
/// `Result` to the caller but must never panic the calling request.
pub(crate) struct TelemetrySink {
    dir: PathBuf,
    max_bytes: u64,
    dir_ready: OnceLock<()>,
}

impl TelemetrySink {
    /// Resolves the telemetry directory from `$XDG_STATE_HOME` else
    /// `$HOME/.local/state`. The directory is created lazily on first write.
    pub(crate) fn new() -> Self {
        Self::from_dir(resolve_dir())
    }

    fn from_dir(dir: PathBuf) -> Self {
        Self {
            dir,
            max_bytes: DEFAULT_MAX_BYTES,
            dir_ready: OnceLock::new(),
        }
    }

    #[cfg(test)]
    pub(crate) fn for_test(dir: &Path) -> Self {
        Self::from_dir(dir.to_path_buf())
    }

    /// Appends one compact JSON line for `rec`, rotating the current file
    /// first if the write would exceed `max_bytes`.
    pub(crate) fn record(&self, rec: &SearchTelemetryRecord) -> io::Result<()> {
        if self.dir_ready.get().is_none() {
            fs::create_dir_all(&self.dir)?;
            let _ = self.dir_ready.set(());
        }
        let mut line = serde_json::to_string(rec)?;
        line.push('\n');

        let current = self.dir.join(CURRENT_FILE_NAME);
        let current_len = fs::metadata(&current).map_or(0, |m| m.len());
        if current_len > 0 && current_len + line.len() as u64 > self.max_bytes {
            self.rotate(&current)?;
        }

        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&current)?;
        file.write_all(line.as_bytes())
    }

    /// Renames the current file to a timestamped name and prunes the
    /// oldest rotated file beyond `MAX_ROTATED_FILES`. Concurrent tilth
    /// processes can race this: if another writer already rotated (or
    /// pruned) the same path, the resulting `NotFound` is not an error —
    /// the desired end state (no oversized current file) already holds.
    fn rotate(&self, current: &Path) -> io::Result<()> {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let rotated = self
            .dir
            .join(format!("rotated-{nanos}-{}.jsonl", std::process::id()));
        match fs::rename(current, &rotated) {
            Ok(()) => {}
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(()),
            Err(e) => return Err(e),
        }
        self.prune_rotated()
    }

    fn prune_rotated(&self) -> io::Result<()> {
        let mut rotated: Vec<_> = fs::read_dir(&self.dir)?
            .filter_map(Result::ok)
            .map(|entry| entry.path())
            .filter(|path| {
                path.file_name()
                    .and_then(|n| n.to_str())
                    .is_some_and(|n| n.starts_with("rotated-"))
            })
            .collect();
        rotated.sort();
        while rotated.len() > MAX_ROTATED_FILES {
            let oldest = rotated.remove(0);
            match fs::remove_file(oldest) {
                Ok(()) => {}
                Err(e) if e.kind() == io::ErrorKind::NotFound => {}
                Err(e) => return Err(e),
            }
        }
        Ok(())
    }
}

fn resolve_dir() -> PathBuf {
    if let Ok(xdg) = std::env::var("XDG_STATE_HOME") {
        if !xdg.is_empty() {
            return PathBuf::from(xdg).join("tilth").join("telemetry");
        }
    }
    let home = std::env::var("HOME").unwrap_or_else(|_| "/".to_string());
    PathBuf::from(home)
        .join(".local/state")
        .join("tilth")
        .join("telemetry")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs::File;
    use std::io::BufRead;

    fn sample_record() -> SearchTelemetryRecord {
        SearchTelemetryRecord {
            verb: "search".to_string(),
            version: SCHEMA_VERSION,
            route: "symbol".to_string(),
            routes_tried: vec!["symbol".to_string()],
            first_call: true,
            latency_ms: 12,
            result_tokens: 340,
            partial: false,
            timeout: false,
            dependency_coverage: 1.0,
            shard_state: "warm".to_string(),
            client: "claude-code".to_string(),
            worktree: "searchv2".to_string(),
        }
    }

    fn sink_in(dir: &std::path::Path) -> TelemetrySink {
        TelemetrySink::for_test(dir)
    }

    fn read_lines(path: &std::path::Path) -> Vec<String> {
        let file = File::open(path).expect("file exists");
        io::BufReader::new(file)
            .lines()
            .collect::<Result<_, _>>()
            .expect("valid utf8 lines")
    }

    #[test]
    fn record_round_trips_all_fields_and_has_no_content_field() {
        let temp = tempfile::tempdir().expect("tempdir");
        let sink = sink_in(temp.path());
        sink.record(&sample_record()).expect("record succeeds");

        let lines = read_lines(&temp.path().join(CURRENT_FILE_NAME));
        assert_eq!(lines.len(), 1);
        let value: serde_json::Value = serde_json::from_str(&lines[0]).expect("valid json");
        let obj = value.as_object().expect("json object");

        for field in [
            "verb",
            "version",
            "route",
            "routes_tried",
            "first_call",
            "latency_ms",
            "result_tokens",
            "partial",
            "timeout",
            "dependency_coverage",
            "shard_state",
            "client",
            "worktree",
        ] {
            assert!(obj.contains_key(field), "missing field {field}");
        }
        assert_eq!(obj["verb"], "search");
        assert_eq!(obj["version"], SCHEMA_VERSION);

        for forbidden in ["content", "text", "snippet", "source"] {
            assert!(
                !obj.contains_key(forbidden),
                "record must not carry content field {forbidden}"
            );
        }
    }

    #[test]
    fn xdg_state_home_override_is_respected() {
        let temp = tempfile::tempdir().expect("tempdir");
        // SAFETY: test-only env mutation; no other test in this module reads
        // XDG_STATE_HOME concurrently with a differing value.
        unsafe {
            std::env::set_var("XDG_STATE_HOME", temp.path());
        }
        let sink = TelemetrySink::new();
        unsafe {
            std::env::remove_var("XDG_STATE_HOME");
        }

        assert_eq!(sink.dir, temp.path().join("tilth").join("telemetry"));
        sink.record(&sample_record()).expect("record succeeds");
        assert!(sink.dir.join(CURRENT_FILE_NAME).exists());
        assert!(temp
            .path()
            .join("tilth/telemetry")
            .join(CURRENT_FILE_NAME)
            .exists());
    }

    #[test]
    fn rotation_bounds_total_directory_size() {
        let temp = tempfile::tempdir().expect("tempdir");
        let sink = TelemetrySink {
            dir: temp.path().to_path_buf(),
            max_bytes: 512,
            dir_ready: OnceLock::new(),
        };

        for _ in 0..500 {
            sink.record(&sample_record()).expect("record succeeds");
        }

        let total: u64 = fs::read_dir(temp.path())
            .expect("read dir")
            .filter_map(Result::ok)
            .map(|entry| entry.metadata().expect("metadata").len())
            .sum();

        // Documented bound: at most MAX_ROTATED_FILES rotated files plus the
        // current file, each at most ~max_bytes plus one line's slack.
        let bound = (MAX_ROTATED_FILES as u64 + 1) * (sink.max_bytes + 512);
        assert!(
            total <= bound,
            "telemetry directory grew unbounded: {total} bytes exceeds bound {bound}"
        );

        let rotated_count = fs::read_dir(temp.path())
            .expect("read dir")
            .filter_map(Result::ok)
            .filter(|e| {
                e.file_name()
                    .to_str()
                    .is_some_and(|n| n.starts_with("rotated-"))
            })
            .count();
        assert!(
            rotated_count <= MAX_ROTATED_FILES,
            "expected at most {MAX_ROTATED_FILES} rotated files, found {rotated_count}"
        );
    }
}
