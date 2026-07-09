//! ISO-8601-ish UTC timestamp helpers for the JSON cache-token header and
//! `if_modified_since` handling. Avoids pulling chrono — uses Howard
//! Hinnant's date algorithms for proleptic Gregorian conversion.

use std::fs;
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

/// Render the bare `YYYY-MM-DDTHH:MM:SSZ` string. Callers wrap this into
/// the JSON cache-token line or whatever surface they need.
pub fn iso_ts(ts: SystemTime) -> String {
    let secs = ts.duration_since(UNIX_EPOCH).map_or(0, |d| d.as_secs());
    format_iso_utc(secs)
}

/// Parse an RFC-3339-ish timestamp back to `SystemTime`. Accepts the bare
/// `YYYY-MM-DDTHH:MM:SSZ` form, optional fractional seconds (`SS.sss` — the
/// fraction is parsed but discarded), and a trailing numeric UTC offset
/// (`+HH:MM`, `-HH:MM`, `+HHMM`, `-HHMM`) in place of `Z`. Returns None when
/// malformed.
pub fn parse_iso_utc(s: &str) -> Option<SystemTime> {
    let s = s.trim();
    let (date, time) = s.split_once('T')?;
    let mut dparts = date.split('-');
    let y: i64 = dparts.next()?.parse().ok()?;
    let mo: u32 = dparts.next()?.parse().ok()?;
    let d: u32 = dparts.next()?.parse().ok()?;
    let (time, offset_secs) = split_utc_offset(time)?;
    let mut tparts = time.split(':');
    let hh: u32 = tparts.next()?.parse().ok()?;
    let mm: u32 = tparts.next()?.parse().ok()?;
    let ss_field = tparts.next().unwrap_or("0");
    let ss_str = ss_field.split('.').next()?;
    let ss: u32 = ss_str.parse().ok()?;
    if mo == 0 || mo > 12 || d == 0 || d > days_in_month(y, mo) || hh > 23 || mm > 59 || ss > 59 {
        return None;
    }
    let secs = days_from_civil(y, mo, d) * 86_400
        + i64::from(hh) * 3600
        + i64::from(mm) * 60
        + i64::from(ss)
        - offset_secs;
    if secs < 0 {
        return None;
    }
    Some(SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(secs as u64))
}

/// Split a trailing `Z` or numeric UTC offset off the time-of-day portion.
/// Returns `(bare_time, offset_seconds)` where `offset_seconds` is what to
/// subtract from the local wall-clock seconds to get UTC (i.e. local time is
/// `UTC + offset`, per RFC-3339 sign convention). `Z` or no suffix at all
/// yields an offset of 0.
fn split_utc_offset(time: &str) -> Option<(&str, i64)> {
    if let Some(bare) = time.strip_suffix('Z') {
        return Some((bare, 0));
    }
    let Some(idx) = time.find(['+', '-']) else {
        return Some((time, 0));
    };
    if idx == 0 {
        return None;
    }
    let (bare, off) = time.split_at(idx);
    let sign: i64 = if off.starts_with('-') { -1 } else { 1 };
    let off = &off[1..];
    let (oh, om): (u32, u32) = if let Some((h, m)) = off.split_once(':') {
        (h.parse().ok()?, m.parse().ok()?)
    } else if off.len() == 4 {
        (off[0..2].parse().ok()?, off[2..4].parse().ok()?)
    } else {
        return None;
    };
    if oh > 23 || om > 59 {
        return None;
    }
    Some((bare, sign * (i64::from(oh) * 3600 + i64::from(om) * 60)))
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

// Calendar length of a month, leap-year aware. `m` is assumed 1..=12
// (callers range-check first).
fn days_in_month(y: i64, m: u32) -> u32 {
    match m {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if (y % 4 == 0 && y % 100 != 0) || y % 400 == 0 => 29,
        2 => 28,
        _ => 0,
    }
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

/// Wrap an output with a leading JSON header line that combines the cache
/// token (when `now` is `Some`) with view-shape metadata (when `meta` is a
/// JSON object). Both are merged into a single first-line object so agents
/// pattern-match one line for all structured signals.
///
/// `meta` is expected to be an `Object`; any other shape (including `Null`)
/// is treated as no extra fields. When the merged header is empty —
/// neither timestamp nor meta provided — the body is returned unchanged
/// so this function is a safe drop-in for paths that conditionally emit a
/// header.
pub fn with_meta_header(
    now: Option<SystemTime>,
    mut header: serde_json::Map<String, serde_json::Value>,
    body: &str,
) -> String {
    if let Some(ts) = now {
        header.insert(
            "if_modified_since".into(),
            serde_json::Value::String(iso_ts(ts)),
        );
    }
    if header.is_empty() {
        return body.to_string();
    }
    let header_str = serde_json::Value::Object(header).to_string();
    format!("{header_str}\n\n{body}")
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
    format!("# {} (unchanged @ {})", path.display(), iso_ts(since))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn iso_roundtrip() {
        let ts = SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(1_700_000_000);
        let s = format_iso_utc(1_700_000_000);
        let back = parse_iso_utc(&s).unwrap();
        assert_eq!(back, ts);
    }

    #[test]
    fn parse_iso_malformed_returns_none() {
        assert!(parse_iso_utc("not a date").is_none());
        // Out-of-range fields must be rejected, not silently wrapped.
        assert!(
            parse_iso_utc("2026-13-99T99:99:99Z").is_none(),
            "out-of-range month/day/time must return None"
        );
        assert!(parse_iso_utc("").is_none());
        assert!(parse_iso_utc("2026-05-14").is_none(), "missing T separator");
        // Impossible calendar dates must be rejected, not normalized.
        assert!(
            parse_iso_utc("2026-02-31T00:00:00Z").is_none(),
            "Feb 31 must return None, not normalize into March"
        );
        assert!(
            parse_iso_utc("2025-02-29T00:00:00Z").is_none(),
            "Feb 29 in a non-leap year must return None"
        );
        assert!(
            parse_iso_utc("2024-02-29T00:00:00Z").is_some(),
            "Feb 29 in a leap year must parse"
        );
        assert!(
            parse_iso_utc("2026-04-31T00:00:00Z").is_none(),
            "April 31 must return None"
        );
    }

    #[test]
    fn parse_iso_fractional_seconds_and_offsets() {
        let reference = parse_iso_utc("2026-01-01T00:00:00Z").unwrap();
        assert_eq!(
            parse_iso_utc("2026-01-01T00:00:00.000Z").unwrap(),
            reference,
            "fractional seconds must be parsed (integer part) and ignored, not rejected"
        );
        assert_eq!(
            parse_iso_utc("2026-01-01T00:00:00+00:00").unwrap(),
            reference,
            "a zero UTC offset must parse to the same instant as Z"
        );
        // A positive offset moves local wall-clock time ahead of UTC: local
        // 05:00 at +05:00 is UTC 00:00 (local - offset).
        assert_eq!(
            parse_iso_utc("2026-01-01T05:00:00+05:00").unwrap(),
            reference,
            "+05:00 offset must convert local 05:00 to UTC 00:00"
        );
        // +HHMM compact form.
        assert_eq!(
            parse_iso_utc("2026-01-01T05:00:00+0500").unwrap(),
            reference,
            "compact +HHMM offset must parse the same as +HH:MM"
        );
        // A negative offset moves local time behind UTC: local 00:00 at
        // -05:00 is UTC 05:00 (local - offset = 0 - (-5h) = 5h).
        assert_eq!(
            parse_iso_utc("2026-01-01T00:00:00-05:00").unwrap(),
            parse_iso_utc("2026-01-01T05:00:00Z").unwrap(),
            "-05:00 offset must convert local 00:00 to UTC 05:00"
        );
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
        let future = SystemTime::now() + std::time::Duration::from_hours(1);
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
    fn with_meta_header_merges_cache_token_and_meta() {
        let now = SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(1_700_000_000);
        let meta = serde_json::json!({"view": "outline", "original_line_count": 1500})
            .as_object()
            .unwrap()
            .clone();
        let out = with_meta_header(Some(now), meta, "body");
        let first_line = out.lines().next().expect("at least one line");
        let parsed: serde_json::Value =
            serde_json::from_str(first_line).expect("first line must be valid JSON");
        assert!(parsed.get("if_modified_since").is_some(), "{out}");
        assert_eq!(parsed.get("view").and_then(|v| v.as_str()), Some("outline"));
        assert_eq!(
            parsed
                .get("original_line_count")
                .and_then(serde_json::Value::as_u64),
            Some(1500)
        );
        assert!(out.ends_with("body"), "body trailing: {out}");
    }

    #[test]
    fn with_meta_header_meta_only_no_timestamp() {
        let meta = serde_json::json!({"view": "signature", "next_view": "full"})
            .as_object()
            .unwrap()
            .clone();
        let out = with_meta_header(None, meta, "body");
        let first_line = out.lines().next().expect("at least one line");
        let parsed: serde_json::Value =
            serde_json::from_str(first_line).expect("first line must be valid JSON");
        assert!(parsed.get("if_modified_since").is_none(), "no ts: {out}");
        assert_eq!(
            parsed.get("view").and_then(|v| v.as_str()),
            Some("signature")
        );
        assert_eq!(
            parsed.get("next_view").and_then(|v| v.as_str()),
            Some("full")
        );
    }

    #[test]
    fn with_meta_header_empty_returns_bare_body() {
        let out = with_meta_header(None, serde_json::Map::new(), "body");
        assert_eq!(out, "body", "empty header drops the prefix entirely");
    }
}
