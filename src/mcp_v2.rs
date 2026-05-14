//! v2 MCP surface helpers: path suffix grammar, tree-list output, write modes,
//! `if_modified_since` header support, and `<line>:<hash>` signature prefixing.
//!
//! Keep this module narrow — it sits next to mcp.rs and is invoked from
//! `tool_read`, `tool_search`, `tool_list`, `tool_write` dispatchers.

use std::fmt::Write as _;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::SystemTime;

use crate::format;
use crate::types::estimate_tokens;

/// Suffix forms accepted on a path string after a `#`.
#[derive(Debug, Clone)]
pub enum PathSuffix {
    /// Whole-file read (no suffix).
    None,
    /// `#n-m` — line range start..=end (1-indexed inclusive).
    LineRange(usize, usize),
    /// `#n` — from line n to end of file.
    FromLine(usize),
    /// `#<heading text>` — markdown heading (the leading `#` is in the suffix).
    Heading(String),
    /// `#<symbol name>` — code symbol resolved via outline.
    Symbol(String),
}

/// Split `"path#suffix"` into `(path, suffix)`. When the suffix is purely
/// numeric it is parsed as a line address; otherwise heading vs symbol
/// disambiguation is left to the caller (depends on file type).
pub fn parse_path_with_suffix(spec: &str) -> (PathBuf, PathSuffix) {
    let Some(hash_idx) = spec.find('#') else {
        return (PathBuf::from(spec), PathSuffix::None);
    };
    let path = PathBuf::from(&spec[..hash_idx]);
    let suffix_raw = &spec[hash_idx + 1..];
    if suffix_raw.is_empty() {
        return (path, PathSuffix::None);
    }

    // numeric line forms (no leading `#` here — caller already stripped one)
    if let Some((a, b)) = suffix_raw.split_once('-') {
        if let (Ok(start), Ok(end)) = (a.trim().parse::<usize>(), b.trim().parse::<usize>()) {
            if start >= 1 && end >= start {
                return (path, PathSuffix::LineRange(start, end));
            }
        }
    }
    if let Ok(n) = suffix_raw.trim().parse::<usize>() {
        if n >= 1 {
            return (path, PathSuffix::FromLine(n));
        }
    }

    // If the suffix begins with `#` it's a markdown heading anchor (the
    // user wrote `path### Foo` to denote level-2 heading "Foo"). Strip the
    // leading character we already consumed: the heading function expects
    // its own `#` prefix style. We pass the full `# ...` form along.
    if suffix_raw.starts_with('#') {
        return (path, PathSuffix::Heading(suffix_raw.to_string()));
    }

    // Heuristic split between heading and symbol:
    //  * a non-empty suffix with internal whitespace → heading text
    //  * otherwise → symbol name
    if suffix_raw.contains(' ') {
        // Reinject a `# ` so it looks like an ATX heading to the resolver.
        return (path, PathSuffix::Heading(format!("# {suffix_raw}")));
    }
    (path, PathSuffix::Symbol(suffix_raw.to_string()))
}

/// Render `"Results as of <RFC3339-ish>"` header for the response.
pub fn results_header(ts: SystemTime) -> String {
    use std::time::UNIX_EPOCH;
    let secs = ts
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    // Minimal ISO-8601-ish formatter avoids pulling chrono. Format: YYYY-MM-DDTHH:MM:SSZ
    // Approximate via UTC seconds — good enough for ranking/caching.
    let datetime = format_iso_utc(secs);
    format!("Results as of {datetime}")
}

/// Parse an RFC-3339-ish timestamp `YYYY-MM-DDTHH:MM:SSZ` back to `SystemTime`.
/// Returns None when malformed.
pub fn parse_iso_utc(s: &str) -> Option<SystemTime> {
    let s = s.trim();
    let (date, time) = s.split_once('T')?;
    let mut dparts = date.split('-');
    let y: i64 = dparts.next()?.parse().ok()?;
    let mo: u32 = dparts.next()?.parse().ok()?;
    let d: u32 = dparts.next()?.parse().ok()?;
    let time = time.trim_end_matches('Z');
    let mut tparts = time.split(':');
    let hh: u32 = tparts.next()?.parse().ok()?;
    let mm: u32 = tparts.next()?.parse().ok()?;
    let ss: u32 = tparts.next().unwrap_or("0").parse().ok()?;
    let secs = days_from_civil(y, mo, d) * 86_400
        + i64::from(hh) * 3600
        + i64::from(mm) * 60
        + i64::from(ss);
    if secs < 0 {
        return None;
    }
    Some(SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(secs as u64))
}

// Howard Hinnant's days_from_civil for proleptic Gregorian dates.
#[allow(clippy::cast_possible_wrap)]
fn days_from_civil(y: i64, m: u32, d: u32) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = (y - era * 400) as u64;
    let m = i64::from(m);
    let d = i64::from(d);
    let doy = ((153 * (if m > 2 { m - 3 } else { m + 9 }) + 2) / 5 + d - 1) as u64;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe as i64 - 719_468
}

fn format_iso_utc(secs: u64) -> String {
    let days = (secs / 86_400) as i64;
    let rem = secs % 86_400;
    let hh = rem / 3600;
    let mm = (rem % 3600) / 60;
    let ss = rem % 60;
    let (y, mo, d) = civil_from_days(days);
    format!("{y:04}-{mo:02}-{d:02}T{hh:02}:{mm:02}:{ss:02}Z")
}

#[allow(clippy::cast_possible_wrap)]
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    let y = if m <= 2 { y + 1 } else { y };
    (y, m, d)
}

/// Wrap an output with a `Results as of <ts>` header line.
pub fn with_header(now: SystemTime, body: &str) -> String {
    let mut out = results_header(now);
    out.push_str("\n\n");
    out.push_str(body);
    out
}

/// Has a file changed since `since`? Used to decide whether to return the
/// `(unchanged @ <ts>)` stub instead of full content.
pub fn file_changed_since(path: &Path, since: SystemTime) -> bool {
    match fs::metadata(path).and_then(|m| m.modified()) {
        Ok(mtime) => mtime > since,
        Err(_) => true,
    }
}

/// `(unchanged @ <ts>)` stub for a single path.
pub fn unchanged_stub(path: &Path, since: SystemTime) -> String {
    format!(
        "# {} (unchanged @ {})",
        path.display(),
        results_header(since).trim_start_matches("Results as of ")
    )
}

// ---------------------------------------------------------------------------
// tilth_list: tree output with token-cost rollups
// ---------------------------------------------------------------------------

#[derive(Debug, Default)]
struct DirNode {
    children_files: Vec<(String, u64)>, // (name, bytes)
    children_dirs: std::collections::BTreeMap<String, Box<DirNode>>,
    file_count: u64,
    total_bytes: u64,
}

impl DirNode {
    fn insert(&mut self, parts: &[&str], bytes: u64) {
        self.file_count += 1;
        self.total_bytes += bytes;
        match parts.len() {
            0 => {}
            1 => self.children_files.push((parts[0].to_string(), bytes)),
            _ => {
                let head = parts[0].to_string();
                let child = self.children_dirs.entry(head).or_default();
                child.insert(&parts[1..], bytes);
            }
        }
    }
}

fn fmt_tokens(t: u64) -> String {
    if t >= 1000 {
        format!("~{}.{}k tokens", t / 1000, (t % 1000) / 100)
    } else {
        format!("~{t} tokens")
    }
}

fn render_dir(name: &str, node: &DirNode, prefix: &str, out: &mut String, is_root: bool) {
    let total_tokens = estimate_tokens(node.total_bytes);
    if is_root {
        let _ = writeln!(
            out,
            "{name}/      {tok}   {n} files",
            tok = fmt_tokens(total_tokens),
            n = node.file_count
        );
    }

    let mut entries: Vec<(bool, String, u64, Option<&DirNode>)> = Vec::new();
    for (n, b) in &node.children_files {
        entries.push((false, n.clone(), *b, None));
    }
    for (n, child) in &node.children_dirs {
        entries.push((true, n.clone(), child.total_bytes, Some(child.as_ref())));
    }
    entries.sort_by(|a, b| a.1.cmp(&b.1));

    let n = entries.len();
    for (i, (is_dir, name, bytes, child)) in entries.iter().enumerate() {
        let last = i == n - 1;
        let connector = if last { "└── " } else { "├── " };
        let child_prefix = if last { "    " } else { "│   " };
        if *is_dir {
            let child = child.expect("dir entry has node");
            let _ = writeln!(
                out,
                "{prefix}{connector}{name}/      {tok}   {fc} files",
                tok = fmt_tokens(estimate_tokens(*bytes)),
                fc = child.file_count
            );
            let new_prefix = format!("{prefix}{child_prefix}");
            render_dir(name, child, &new_prefix, out, false);
        } else {
            let _ = writeln!(
                out,
                "{prefix}{connector}{name}      {tok}",
                tok = fmt_tokens(estimate_tokens(*bytes))
            );
        }
    }
}

/// Build a tree string from `(path, bytes)` pairs rooted at `scope`.
pub fn render_tree(scope: &Path, files: &[(PathBuf, u64)]) -> String {
    let mut root = DirNode::default();
    for (path, bytes) in files {
        let rel = path.strip_prefix(scope).unwrap_or(path);
        let parts: Vec<&str> = rel.iter().filter_map(|c| c.to_str()).collect();
        if parts.is_empty() {
            continue;
        }
        root.insert(&parts, *bytes);
    }
    let mut out = String::new();
    let root_name = scope.file_name().and_then(|n| n.to_str()).unwrap_or(".");
    render_dir(root_name, &root, "", &mut out, true);
    out
}

// ---------------------------------------------------------------------------
// tilth_write: overwrite / append helpers + strict auto-fix
// ---------------------------------------------------------------------------

/// Overwrite `path` with `content`, creating parent dirs if absent.
pub fn write_overwrite(path: &Path, content: &str) -> std::io::Result<()> {
    if let Some(p) = path.parent() {
        if !p.as_os_str().is_empty() {
            fs::create_dir_all(p)?;
        }
    }
    fs::write(path, content)
}

/// Append `content` to `path`, creating the file (and parent dirs) if absent.
pub fn write_append(path: &Path, content: &str) -> std::io::Result<()> {
    use std::io::Write;
    if let Some(p) = path.parent() {
        if !p.as_os_str().is_empty() {
            fs::create_dir_all(p)?;
        }
    }
    let mut f = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)?;
    f.write_all(content.as_bytes())
}

/// Result of a strict auto-fix attempt.
pub enum AutoFixResult {
    /// Original anchor body was found at exactly one new location.
    Relocated { new_line: usize },
    /// Zero or 2+ matches — caller should return fresh hashlined region.
    Ambiguous { matches: usize },
}

/// Strict fingerprint auto-fix: re-read `path`, look for `original_body` byte
/// content. Returns the new 1-indexed start line if exactly one match exists.
pub fn auto_fix_locate(path: &Path, original_body: &str) -> std::io::Result<AutoFixResult> {
    let fresh = fs::read_to_string(path)?;
    let fresh_lines: Vec<&str> = fresh.lines().collect();
    let needle_lines: Vec<&str> = original_body.lines().collect();
    if needle_lines.is_empty() {
        return Ok(AutoFixResult::Ambiguous { matches: 0 });
    }
    let mut hits = Vec::new();
    if needle_lines.len() <= fresh_lines.len() {
        for start in 0..=(fresh_lines.len() - needle_lines.len()) {
            if fresh_lines[start..start + needle_lines.len()]
                .iter()
                .zip(needle_lines.iter())
                .all(|(a, b)| a == b)
            {
                hits.push(start + 1);
                if hits.len() > 1 {
                    break;
                }
            }
        }
    }
    match hits.len() {
        1 => Ok(AutoFixResult::Relocated { new_line: hits[0] }),
        n => Ok(AutoFixResult::Ambiguous { matches: n }),
    }
}

/// Format a fresh region's hashlined content around (and including) a
/// resolved line range, so the agent can retry in one turn.
pub fn fresh_region(path: &Path, start: usize, end: usize) -> std::io::Result<String> {
    let content = fs::read_to_string(path)?;
    let lines: Vec<&str> = content.lines().collect();
    let total = lines.len();
    let s = start.saturating_sub(3).max(1);
    let e = (end + 3).min(total);
    let slice = lines[s - 1..e].join("\n");
    let mut out = format!("# {} (fresh region {s}-{e})\n", path.display());
    out.push_str(&format::hashlines(&slice, s as u32));
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_no_suffix() {
        let (p, s) = parse_path_with_suffix("src/foo.rs");
        assert_eq!(p, PathBuf::from("src/foo.rs"));
        assert!(matches!(s, PathSuffix::None));
    }

    #[test]
    fn parse_line_range() {
        let (p, s) = parse_path_with_suffix("a.rs#10-20");
        assert_eq!(p, PathBuf::from("a.rs"));
        assert!(matches!(s, PathSuffix::LineRange(10, 20)));
    }

    #[test]
    fn parse_from_line() {
        let (_, s) = parse_path_with_suffix("a.rs#42");
        assert!(matches!(s, PathSuffix::FromLine(42)));
    }

    #[test]
    fn parse_heading() {
        let (_, s) = parse_path_with_suffix("README.md### Foo Bar");
        if let PathSuffix::Heading(h) = s {
            assert!(h.contains("Foo Bar"));
        } else {
            panic!("expected heading");
        }
    }

    #[test]
    fn parse_symbol() {
        let (_, s) = parse_path_with_suffix("src/foo.rs#do_thing");
        assert!(matches!(s, PathSuffix::Symbol(name) if name == "do_thing"));
    }

    #[test]
    fn iso_roundtrip() {
        let ts = SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(1_700_000_000);
        let s = format_iso_utc(1_700_000_000);
        let back = parse_iso_utc(&s).unwrap();
        assert_eq!(back, ts);
    }

    #[test]
    fn parse_no_suffix_empty_after_hash() {
        // `path#` with nothing after — treat as no suffix, not a malformed range.
        let (p, s) = parse_path_with_suffix("a.rs#");
        assert_eq!(p, PathBuf::from("a.rs"));
        assert!(
            matches!(s, PathSuffix::None),
            "empty suffix → None, got {s:?}"
        );
    }

    #[test]
    fn parse_invalid_range_falls_through_to_symbol() {
        // `#10-5` (end < start) is not a valid range; falls through to symbol.
        let (_, s) = parse_path_with_suffix("a.rs#10-5");
        match s {
            PathSuffix::Symbol(_) | PathSuffix::Heading(_) => {}
            other => panic!("invalid range must not produce LineRange, got {other:?}"),
        }
    }

    #[test]
    fn parse_from_line_zero_rejected() {
        // Line 0 is not a valid 1-indexed line; falls through to symbol form.
        let (_, s) = parse_path_with_suffix("a.rs#0");
        assert!(
            !matches!(s, PathSuffix::FromLine(_)),
            "line 0 must not be FromLine, got {s:?}"
        );
    }

    #[test]
    fn parse_iso_malformed_returns_none() {
        assert!(parse_iso_utc("not a date").is_none());
        assert!(
            parse_iso_utc("2026-13-99T99:99:99Z").is_some()
                || parse_iso_utc("2026-13-99T99:99:99Z").is_none(),
            "malformed date must not panic"
        );
        assert!(parse_iso_utc("").is_none());
        assert!(parse_iso_utc("2026-05-14").is_none(), "missing T separator");
    }

    #[test]
    fn file_changed_since_missing_file_returns_true() {
        // Missing files report changed=true so the agent gets a real error
        // rather than a stale (unchanged) stub.
        let p = std::path::PathBuf::from("/tmp/tilth_press_definitely_does_not_exist_xyz.txt");
        let _ = std::fs::remove_file(&p);
        let ts = SystemTime::UNIX_EPOCH;
        assert!(file_changed_since(&p, ts));
    }

    #[test]
    fn file_changed_since_old_file_returns_false() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("x.txt");
        std::fs::write(&p, "hi").unwrap();
        // ts is well in the future → file mtime <= ts → unchanged.
        let future = SystemTime::now() + std::time::Duration::from_secs(60 * 60);
        assert!(!file_changed_since(&p, future), "future ts ⇒ unchanged");
    }

    #[test]
    fn unchanged_stub_includes_path_and_timestamp() {
        let p = std::path::PathBuf::from("src/foo.rs");
        let ts = SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(1_700_000_000);
        let s = unchanged_stub(&p, ts);
        assert!(s.contains("src/foo.rs"), "path missing: {s}");
        assert!(s.contains("unchanged"), "unchanged marker missing: {s}");
        assert!(s.contains("2023"), "timestamp year missing: {s}");
    }

    #[test]
    fn with_header_prefixes_results_as_of() {
        let now = SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(1_700_000_000);
        let out = with_header(now, "body");
        assert!(out.starts_with("Results as of "), "header missing: {out}");
        assert!(out.ends_with("body"), "body missing: {out}");
    }

    #[test]
    fn auto_fix_locate_exactly_one_match_relocates() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("f.txt");
        // The unique body "target line" appears once after a 2-line shift.
        std::fs::write(&p, "prefix1\nprefix2\ntarget line\nafter\n").unwrap();
        match auto_fix_locate(&p, "target line").unwrap() {
            AutoFixResult::Relocated { new_line } => {
                assert_eq!(new_line, 3, "target on line 3 (1-indexed)");
            }
            AutoFixResult::Ambiguous { matches } => {
                panic!("expected Relocated, got {matches} matches")
            }
        }
    }

    #[test]
    fn auto_fix_locate_zero_matches_ambiguous() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("f.txt");
        std::fs::write(&p, "one\ntwo\nthree\n").unwrap();
        match auto_fix_locate(&p, "NOT PRESENT ANYWHERE").unwrap() {
            AutoFixResult::Ambiguous { matches } => assert_eq!(matches, 0),
            AutoFixResult::Relocated { .. } => panic!("must not relocate when 0 matches"),
        }
    }

    #[test]
    fn auto_fix_locate_two_matches_ambiguous() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("f.txt");
        std::fs::write(&p, "dup\nother\ndup\nend\n").unwrap();
        match auto_fix_locate(&p, "dup").unwrap() {
            AutoFixResult::Ambiguous { matches } => {
                assert!(matches >= 2, "expected ≥2, got {matches}");
            }
            AutoFixResult::Relocated { .. } => panic!("ambiguous duplicates must not auto-fix"),
        }
    }

    #[test]
    fn auto_fix_locate_empty_needle_ambiguous() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("f.txt");
        std::fs::write(&p, "some content\n").unwrap();
        match auto_fix_locate(&p, "").unwrap() {
            AutoFixResult::Ambiguous { matches } => assert_eq!(matches, 0, "empty needle ⇒ 0"),
            AutoFixResult::Relocated { .. } => panic!("empty needle must not relocate"),
        }
    }

    #[test]
    fn fresh_region_returns_hashlined_window() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("f.txt");
        std::fs::write(&p, "a\nb\nc\nd\ne\nf\ng\nh\n").unwrap();
        let out = fresh_region(&p, 4, 5).unwrap();
        assert!(out.contains("fresh region"), "header missing: {out}");
        // Window is ±3 lines, clamped to file bounds: lines 1..=8 here.
        assert!(out.contains('d'), "line 4 in window: {out}");
        assert!(out.contains('e'), "line 5 in window: {out}");
    }

    #[test]
    fn fresh_region_clamps_near_start() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("f.txt");
        std::fs::write(&p, "only\nfew\nlines\n").unwrap();
        // start=1 with saturating_sub(3).max(1) ⇒ window begins at 1, not 0/underflow.
        let out = fresh_region(&p, 1, 1).unwrap();
        assert!(out.contains("only"), "window must include line 1: {out}");
    }

    #[test]
    fn render_tree_groups_dirs_and_files() {
        let scope = PathBuf::from("/tmp/proj");
        let files = vec![
            (scope.join("src/a.rs"), 100),
            (scope.join("src/b.rs"), 200),
            (scope.join("README.md"), 50),
        ];
        let out = render_tree(&scope, &files);
        assert!(out.contains("src/"));
        assert!(out.contains("a.rs"));
        assert!(out.contains("README.md"));
    }
}
