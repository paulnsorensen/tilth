//! Path suffix grammar: `path#suffix` parsing for `tilth_read` / `tilth_search`.
//!
//! Suffix forms (after a `#`):
//!   * empty       → whole-file read
//!   * `n-m`       → 1-indexed inclusive line range
//!   * `n`         → from line n to end of file
//!   * `# heading` → markdown heading anchor
//!   * `name`      → code symbol resolved via outline
//!
//! When the spec has no `#`, a trailing colon line address is also accepted so
//! a path copied with an editor/grep-style line reference still resolves:
//!   * `path:n-m`  → same as `#n-m` (1-indexed inclusive line range)
//!   * `path:n`    → same as `#n` (from line n to end of file)

use std::path::PathBuf;

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
        // No `#`: accept a trailing colon line address (`path:n` / `path:n-m`),
        // the editor/grep-style reference an agent commonly carries into a read.
        if let Some(parsed) = split_trailing_line_suffix(spec) {
            return parsed;
        }
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
    //  * a declaration form such as `fn name` in a code file → symbol
    //  * a non-empty suffix with internal whitespace → heading text
    //  * otherwise → symbol name
    if is_code_path(&path) {
        if let Some(name) = declaration_name(suffix_raw) {
            return (path, PathSuffix::Symbol(name.to_string()));
        }
    }
    if suffix_raw.contains(' ') {
        // Reinject a `# ` so it looks like an ATX heading to the resolver.
        return (path, PathSuffix::Heading(format!("# {suffix_raw}")));
    }
    (path, PathSuffix::Symbol(suffix_raw.to_string()))
}

/// Whether `path` names a code file with a grammar, where `#fn name` is a
/// symbol. In every other file `#word word` stays a heading.
fn is_code_path(path: &std::path::Path) -> bool {
    matches!(
        crate::lang::detect_file_type(path),
        crate::types::FileType::Code(_)
    )
}

/// The symbol name in a declaration form such as `fn name` or `pub async fn
/// name`: one or more declaration keywords, then one identifier.
fn declaration_name(suffix: &str) -> Option<&str> {
    // `impl` is absent on purpose: `impl Foo` names an impl block, which the
    // symbol `Foo` (a struct) is not.
    const KEYWORDS: [&str; 21] = [
        "pub",
        "async",
        "unsafe",
        "const",
        "static",
        "fn",
        "struct",
        "enum",
        "trait",
        "type",
        "mod",
        "class",
        "interface",
        "def",
        "func",
        "function",
        "abstract",
        "final",
        "export",
        "public",
        "private",
    ];
    let mut tokens = suffix.split_whitespace();
    let mut seen_keyword = false;
    let mut name = None;
    for token in &mut tokens {
        if KEYWORDS.contains(&token) {
            seen_keyword = true;
            continue;
        }
        name = Some(token);
        break;
    }
    let name = name?;
    if !seen_keyword || tokens.next().is_some() {
        return None;
    }
    let is_identifier = name
        .chars()
        .all(|c| c.is_alphanumeric() || matches!(c, '_' | '$' | ':' | '.'));
    is_identifier.then_some(name)
}

/// Try to split a trailing colon line address off `spec`: `path:n-m` →
/// [`PathSuffix::LineRange`], `path:n` → [`PathSuffix::FromLine`]. Anchors on
/// the last `:` and matches only when the tail is a bare 1-indexed line address,
/// so a colon elsewhere in the path (or a non-numeric tail) leaves the spec
/// untouched. Mirrors the `#n-m` / `#n` grammar exactly (same 1-indexed,
/// inclusive, `start <= end`, `n >= 1` rules).
fn split_trailing_line_suffix(spec: &str) -> Option<(PathBuf, PathSuffix)> {
    let colon = spec.rfind(':')?;
    let head = &spec[..colon];
    let tail = &spec[colon + 1..];
    if head.is_empty() || tail.is_empty() {
        return None;
    }

    if let Some((a, b)) = tail.split_once('-') {
        let (Ok(start), Ok(end)) = (a.parse::<usize>(), b.parse::<usize>()) else {
            return None;
        };
        if start >= 1 && end >= start {
            return Some((PathBuf::from(head), PathSuffix::LineRange(start, end)));
        }
        return None;
    }

    if let Ok(n) = tail.parse::<usize>() {
        if n >= 1 {
            return Some((PathBuf::from(head), PathSuffix::FromLine(n)));
        }
    }
    None
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
    fn parse_declaration_form_is_a_symbol() {
        for spec in ["a.rs#fn recv_ref", "a.rs#pub async fn recv_ref"] {
            let (_, s) = parse_path_with_suffix(spec);
            assert!(
                matches!(&s, PathSuffix::Symbol(name) if name == "recv_ref"),
                "{spec} -> {s:?}"
            );
        }
    }

    #[test]
    fn parse_declaration_form_in_markdown_stays_a_heading() {
        let (_, s) = parse_path_with_suffix("README.md#fn recv_ref");
        assert!(matches!(s, PathSuffix::Heading(_)), "{s:?}");
    }

    #[test]
    fn parse_prose_words_in_code_file_stay_a_heading() {
        let (_, s) = parse_path_with_suffix("a.rs#fn recv ref");
        assert!(matches!(s, PathSuffix::Heading(_)), "{s:?}");
    }

    #[test]
    fn parse_impl_form_is_not_the_type_symbol() {
        for spec in ["a.rs#impl Foo", "a.rs#impl Trait for Foo"] {
            let (_, s) = parse_path_with_suffix(spec);
            assert!(matches!(s, PathSuffix::Heading(_)), "{spec} -> {s:?}");
        }
    }

    #[test]
    fn parse_declaration_words_in_structured_file_stay_a_heading() {
        for spec in ["c.yaml#type Settings", "c.toml#fn run", "c.json#class Foo"] {
            let (_, s) = parse_path_with_suffix(spec);
            assert!(matches!(s, PathSuffix::Heading(_)), "{spec} -> {s:?}");
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
    fn parse_colon_line_range() {
        // `path:n-m` resolves the same as `path#n-m` (issue #84).
        let (p, s) = parse_path_with_suffix("src/read/mod.rs:255-275");
        assert_eq!(p, PathBuf::from("src/read/mod.rs"));
        assert!(
            matches!(s, PathSuffix::LineRange(255, 275)),
            "colon range must parse as LineRange, got {s:?}"
        );
    }

    #[test]
    fn parse_colon_from_line() {
        // `path:n` resolves the same as `path#n` (from line n to end).
        let (p, s) = parse_path_with_suffix("a.rs:42");
        assert_eq!(p, PathBuf::from("a.rs"));
        assert!(
            matches!(s, PathSuffix::FromLine(42)),
            "colon single line must parse as FromLine, got {s:?}"
        );
    }

    #[test]
    fn parse_colon_non_numeric_tail_is_plain_path() {
        // A colon not followed by a bare line address is left in the path.
        let (p, s) = parse_path_with_suffix("weird:name.rs");
        assert_eq!(p, PathBuf::from("weird:name.rs"));
        assert!(
            matches!(s, PathSuffix::None),
            "non-numeric colon tail must stay a plain path, got {s:?}"
        );
    }

    #[test]
    fn parse_colon_invalid_range_is_plain_path() {
        // `:10-5` (end < start) is not a valid range; the whole spec stays path.
        let (p, s) = parse_path_with_suffix("a.rs:10-5");
        assert_eq!(p, PathBuf::from("a.rs:10-5"));
        assert!(
            matches!(s, PathSuffix::None),
            "invalid colon range must stay a plain path, got {s:?}"
        );
    }

    #[test]
    fn parse_colon_line_zero_rejected() {
        // Line 0 is not a valid 1-indexed line; the whole spec stays path.
        let (p, s) = parse_path_with_suffix("a.rs:0");
        assert_eq!(p, PathBuf::from("a.rs:0"));
        assert!(
            matches!(s, PathSuffix::None),
            "colon line 0 must stay a plain path, got {s:?}"
        );
    }

    #[test]
    fn parse_hash_wins_over_colon() {
        // When both are present the `#` grammar is primary; the colon stays in
        // the path portion, untouched.
        let (p, s) = parse_path_with_suffix("a.rs:1#10-20");
        assert_eq!(p, PathBuf::from("a.rs:1"));
        assert!(
            matches!(s, PathSuffix::LineRange(10, 20)),
            "hash suffix must win over a colon, got {s:?}"
        );
    }
}
