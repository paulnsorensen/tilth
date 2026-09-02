//! Shared file-prefilter helper for relational queries (callers, callees,
//! deps). Reads a file, gates on size, and runs the per-file bloom prefilter
//! against any of the supplied target symbols. Returns content + mtime when
//! the file is worth deeper inspection (tree-sitter parse, outline scan).

use std::path::Path;
use std::time::SystemTime;

use crate::index::bloom::BloomFilterCache;

/// Skip files larger than this; tree-sitter parses on huge files dominate
/// query latency without surfacing useful matches.
pub(super) const MAX_FILE_SIZE: u64 = 500_000;

/// Outcome of [`read_with_bloom_check`].
#[derive(Debug, PartialEq)]
pub(super) enum BloomRead {
    /// Content and mtime of a file at least one target is bloom-positive in.
    Hit(String, SystemTime),
    /// Oversized, or bloom-negative for every target.
    Skip,
    /// Stat, read, or UTF-8 decode failed.
    Unreadable,
}

/// Read `path`, validate size, and pass through only when at least one
/// target is bloom-positive.
///
/// Bloom is probabilistic: a positive may be a false positive. Callers that
/// need a tighter pre-AST filter (e.g. memchr) should run it on the returned
/// content before paying for tree-sitter.
pub(super) fn read_with_bloom_check<I, S>(
    path: &Path,
    targets: I,
    bloom: &BloomFilterCache,
    max_size: u64,
) -> BloomRead
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    let Ok(meta) = std::fs::metadata(path) else {
        return BloomRead::Unreadable;
    };
    if meta.len() > max_size {
        return BloomRead::Skip;
    }
    let mtime = meta.modified().unwrap_or(SystemTime::UNIX_EPOCH);
    let Ok(content) = std::fs::read_to_string(path) else {
        return BloomRead::Unreadable;
    };

    if !targets
        .into_iter()
        .any(|t| bloom.contains(path, mtime, &content, t.as_ref()))
    {
        return BloomRead::Skip;
    }

    BloomRead::Hit(content, mtime)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashSet;
    use std::fs;

    #[test]
    fn returns_skip_for_oversized_file() {
        let tmp = tempfile::tempdir().unwrap();
        let p = tmp.path().join("big.rs");
        // Fill past max_size
        let payload = "fn foo() {}\n".repeat(2);
        fs::write(&p, &payload).unwrap();
        let bloom = BloomFilterCache::new();
        let targets: HashSet<String> = ["foo".to_string()].into_iter().collect();
        // max_size below file len → skip
        assert_eq!(
            read_with_bloom_check(&p, &targets, &bloom, 1),
            BloomRead::Skip
        );
    }

    #[test]
    fn returns_skip_when_no_target_is_bloom_positive() {
        let tmp = tempfile::tempdir().unwrap();
        let p = tmp.path().join("a.rs");
        fs::write(&p, "fn alpha() {}\n").unwrap();
        let bloom = BloomFilterCache::new();
        let targets: HashSet<String> = ["beta".to_string()].into_iter().collect();
        assert_eq!(
            read_with_bloom_check(&p, &targets, &bloom, MAX_FILE_SIZE),
            BloomRead::Skip
        );
    }

    #[test]
    fn returns_content_when_target_present() {
        let tmp = tempfile::tempdir().unwrap();
        let p = tmp.path().join("a.rs");
        fs::write(&p, "fn alpha() {}\n").unwrap();
        let bloom = BloomFilterCache::new();
        let targets: HashSet<String> = ["alpha".to_string()].into_iter().collect();
        let BloomRead::Hit(content, _) = read_with_bloom_check(&p, &targets, &bloom, MAX_FILE_SIZE)
        else {
            panic!("expected a bloom hit");
        };
        assert!(content.contains("alpha"));
    }

    #[test]
    fn returns_unreadable_for_missing_file() {
        let tmp = tempfile::tempdir().unwrap();
        let p = tmp.path().join("missing.rs");
        let bloom = BloomFilterCache::new();
        let targets: HashSet<&str> = ["alpha"].into_iter().collect();
        assert_eq!(
            read_with_bloom_check(&p, &targets, &bloom, MAX_FILE_SIZE),
            BloomRead::Unreadable
        );
    }

    #[test]
    fn returns_unreadable_for_non_utf8_file() {
        let tmp = tempfile::tempdir().unwrap();
        let p = tmp.path().join("latin1.rs");
        fs::write(&p, b"fn alpha() {} // caf\xe9\n").unwrap();
        let bloom = BloomFilterCache::new();
        let targets: HashSet<&str> = ["alpha"].into_iter().collect();
        assert_eq!(
            read_with_bloom_check(&p, &targets, &bloom, MAX_FILE_SIZE),
            BloomRead::Unreadable
        );
    }

    #[test]
    fn accepts_borrowed_str_targets() {
        // callees.rs holds HashSet<&str>; the helper must accept that shape
        // without forcing a String allocation per call.
        let tmp = tempfile::tempdir().unwrap();
        let p = tmp.path().join("a.rs");
        fs::write(&p, "fn alpha() {}\n").unwrap();
        let bloom = BloomFilterCache::new();
        let targets: HashSet<&str> = ["alpha"].into_iter().collect();
        let BloomRead::Hit(_, _) = read_with_bloom_check(&p, &targets, &bloom, MAX_FILE_SIZE)
        else {
            panic!("expected a bloom hit");
        };
    }
}
