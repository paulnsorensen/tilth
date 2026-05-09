//! Thin wrappers around third-party utility crates.
//!
//! Centralizes external utility deps so callers reference one stable
//! internal API. Lets us swap implementations without churning call sites.

use std::path::PathBuf;

/// Levenshtein edit distance over Unicode scalar values.
///
/// Operates on `char`s, not bytes — a single CJK or emoji glyph is one
/// edit unit, not 3-4. Used by filename and heading suggestion.
pub(crate) fn edit_distance(a: &str, b: &str) -> usize {
    strsim::levenshtein(a, b)
}

/// Decode percent-encoded URI path components (e.g. `%20` → space).
///
/// Malformed `%` sequences are preserved literally rather than dropped.
pub(crate) fn percent_decode(input: &str) -> String {
    percent_encoding::percent_decode_str(input)
        .decode_utf8_lossy()
        .into_owned()
}

/// Cross-platform home directory lookup.
pub(crate) fn home_dir() -> Result<PathBuf, String> {
    home::home_dir().ok_or_else(|| "home directory not found".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Distance must rank Unicode scalars, not bytes — CJK and emoji
    /// glyphs each count as one edit unit.
    #[test]
    fn edit_distance_is_unicode_aware() {
        assert_eq!(edit_distance("设置", "設定"), 2);
        assert_eq!(edit_distance("🦀", "🐙"), 1);
        assert_eq!(edit_distance("kitten", "sitting"), 3);
    }

    /// Percent decoding must handle clean paths, multi-sequence inputs,
    /// and preserve malformed `%` literally.
    #[test]
    fn percent_decode_handles_edges() {
        assert_eq!(
            percent_decode("/Users/Jan%20Hallvard/project"),
            "/Users/Jan Hallvard/project"
        );
        assert_eq!(percent_decode("/normal/path"), "/normal/path");
        assert_eq!(percent_decode("%2F%2Fweird"), "//weird");
        assert_eq!(percent_decode("no%percent"), "no%percent");
    }
}
