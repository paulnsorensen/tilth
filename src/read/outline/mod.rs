pub mod code;
pub mod fallback;
pub mod markdown;
pub mod structured;
pub mod tabular;
pub mod test_file;

use std::path::Path;

use crate::types::FileType;

const OUTLINE_CAP: usize = 100; // max outline lines for huge files

/// Generate a smart view based on file type.
pub fn generate(
    path: &Path,
    file_type: FileType,
    content: &str,
    buf: &[u8],
    capped: bool,
) -> String {
    let max_lines = if capped { OUTLINE_CAP } else { usize::MAX };

    // Test files get special treatment regardless of language
    if crate::types::is_test_file(path) {
        if let FileType::Code(lang) = file_type {
            if let Some((outline, truncated)) = test_file::outline(content, lang, max_lines) {
                return with_omission_note(outline, truncated);
            }
        }
    }

    // Each backend reports whether it dropped entries at `max_lines`. The
    // head/tail fallbacks (log/other) carry their own elision indicator and
    // are not symbol outlines, so they never trigger the omission note.
    let (outline, truncated) = match file_type {
        FileType::Code(lang) => code::outline(content, lang, max_lines),
        FileType::Markdown => markdown::outline(buf, max_lines),
        FileType::StructuredData => structured::outline(path, content, max_lines),
        FileType::Tabular => tabular::outline(content, max_lines),
        FileType::Log => (fallback::log_view(content), false),
        FileType::Other => (fallback::head_tail(content), false),
    };
    with_omission_note(outline, truncated)
}

/// Append a note when the outline actually hit `max_lines` and more symbols
/// exist below. Without this note, agents read the outline as exhaustive
/// and miss symbols below the cap.
///
/// `truncated` is the real drop signal from the backend — set only when a
/// symbol/entry was elided because the cap was reached. This replaces the
/// former `lines().count() == max_lines` heuristic, which falsely fired on a
/// file with exactly `OUTLINE_CAP` symbols and nothing actually dropped.
fn with_omission_note(outline: String, truncated: bool) -> String {
    if !truncated {
        return outline;
    }
    format!(
        "{outline}\n\n> outline truncated — more symbols exist below the cap. \
         Use section=\"<start>-<end>\" with the line numbers shown in [...] \
         brackets above, or tilth_search \"<name>\" for a specific symbol."
    )
}

#[cfg(test)]
mod tests {
    use super::with_omission_note;
    use std::fmt::Write as _;

    #[test]
    fn note_appended_when_truncated() {
        let outline = (0..100)
            .map(|i| format!("line {i}"))
            .collect::<Vec<_>>()
            .join("\n");
        let result = with_omission_note(outline, true);
        assert!(result.contains("outline truncated"));
    }

    #[test]
    fn no_note_when_not_truncated() {
        let outline = (0..50)
            .map(|i| format!("line {i}"))
            .collect::<Vec<_>>()
            .join("\n");
        let result = with_omission_note(outline.clone(), false);
        assert_eq!(result, outline);
    }

    /// Regression: a file with exactly `OUTLINE_CAP` entries and nothing
    /// dropped must NOT get the truncation note — the old line-count
    /// heuristic falsely fired here.
    #[test]
    fn no_note_at_exact_cap_without_truncation() {
        let outline = (0..100)
            .map(|i| format!("line {i}"))
            .collect::<Vec<_>>()
            .join("\n");
        let result = with_omission_note(outline.clone(), false);
        assert_eq!(result, outline);
    }

    /// Integration test: drive the full `generate()` pipeline with a
    /// real Rust source containing more than `OUTLINE_CAP` top-level
    /// functions. Verifies the cap actually fires and that
    /// `with_omission_note` is wired into the pipeline correctly —
    /// not just exercised in isolation.
    #[test]
    fn integration_note_on_capped_code_file() {
        let mut src = String::new();
        for i in 0..150 {
            writeln!(src, "pub fn func_{i}() {{}}").unwrap();
        }
        let path = std::path::Path::new("fake.rs");
        let file_type = crate::types::FileType::Code(crate::types::Lang::Rust);
        let result = super::generate(path, file_type, &src, src.as_bytes(), true);
        assert!(
            result.contains("outline truncated"),
            "expected truncation note for 150 funcs over OUTLINE_CAP=100, got:\n{result}"
        );
    }

    /// Integration test: a small file (5 functions) must NOT produce
    /// the truncation note even when `capped=true` is passed, because
    /// the actual entry count is well below the cap.
    #[test]
    fn integration_no_note_on_small_code_file() {
        let mut src = String::new();
        for i in 0..5 {
            writeln!(src, "pub fn func_{i}() {{}}").unwrap();
        }
        let path = std::path::Path::new("fake.rs");
        let file_type = crate::types::FileType::Code(crate::types::Lang::Rust);
        let result = super::generate(path, file_type, &src, src.as_bytes(), true);
        assert!(
            !result.contains("outline truncated"),
            "spurious truncation note for 5 funcs (under OUTLINE_CAP=100):\n{result}"
        );
    }
}
