//! Advisory agent-facing hint strings.
//!
//! This table holds advisory hints only: success-path guidance that a future
//! verbosity knob would gate. Corrective teaching errors (drift, fabricated
//! tags, malformed ops) stay in their `thiserror` variants where they live
//! today — they are not advisory and are not moved here. Only fully-static
//! strings live in this table; anything that interpolates a runtime value
//! stays at its call site.

/// One variant per distinct advisory string tilth emits.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Hint {
    SearchZeroMatchesAdvisory,
    SearchEmptyGlobNoFiles,
    SearchEmptyNoSymbols,
    SearchEmptyNoContent,
    SearchEmptyRegexZero,
    SearchEmptyMerged,
    CallersNameNotFound,
    CallersIndirectionMechanisms,
    CallersGlobExcludedNote,
    GrokScopeTooLarge,
    BatchTipRead,
    BatchTipSearch,
    BatchTipList,
}

impl Hint {
    pub fn text(self) -> &'static str {
        match self {
            Self::SearchZeroMatchesAdvisory => {
                "no definitions or usages; try kind=content for strings/comments, widen scope, or check spelling"
            }
            Self::SearchEmptyGlobNoFiles => "glob matched no files — broaden glob or check path",
            Self::SearchEmptyNoSymbols => "no symbols matched; try kind: content or check spelling",
            Self::SearchEmptyNoContent => "no content matches; try kind: symbol or a broader pattern",
            Self::SearchEmptyRegexZero => {
                "regex matched zero content; try kind: symbol or a broader pattern"
            }
            Self::SearchEmptyMerged => "no matches in any mode — re-check the query and glob",
            Self::CallersNameNotFound => {
                "Check the spelling, or widen scope if you expected hits outside this directory."
            }
            Self::CallersIndirectionMechanisms => {
                "tilth detects only direct, by-name calls; this symbol may still be reachable via:\n\n  \u{2022} interface / trait dispatch (Rust `dyn Trait`, Go interface, Java/Kotlin abstract method)\n  \u{2022} reflection or dynamic dispatch (`getattr`, `Method::invoke`, `eval`)\n  \u{2022} framework registration (HTTP routes, JSON-RPC, plugin systems, decorators)\n  \u{2022} function values stored in maps, structs, or passed as callbacks"
            }
            Self::CallersGlobExcludedNote => "\n  \u{2022} test files (if `glob` excluded them)",
            Self::GrokScopeTooLarge => {
                "Scope too large to fully search — narrow scope for a better match."
            }
            Self::BatchTipRead => "TIP: batch into one call — paths: [\"a.rs\", \"b.rs\"].",
            Self::BatchTipSearch => {
                "TIP: batch into one call — queries: [{\"query\":\"foo\"}, {\"query\":\"bar\"}]."
            }
            Self::BatchTipList => "TIP: batch into one call — patterns: [\"*.rs\", \"*.toml\"].",
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const ALL: &[Hint] = &[
        Hint::SearchZeroMatchesAdvisory,
        Hint::SearchEmptyGlobNoFiles,
        Hint::SearchEmptyNoSymbols,
        Hint::SearchEmptyNoContent,
        Hint::SearchEmptyRegexZero,
        Hint::SearchEmptyMerged,
        Hint::CallersNameNotFound,
        Hint::CallersIndirectionMechanisms,
        Hint::CallersGlobExcludedNote,
        Hint::GrokScopeTooLarge,
        Hint::BatchTipRead,
        Hint::BatchTipSearch,
        Hint::BatchTipList,
    ];

    #[test]
    fn every_variant_is_non_empty() {
        for hint in ALL {
            assert!(!hint.text().is_empty(), "{hint:?} text is empty");
        }
    }

    #[test]
    fn no_two_variants_share_text() {
        for (i, a) in ALL.iter().enumerate() {
            for b in &ALL[i + 1..] {
                assert_ne!(a.text(), b.text(), "{a:?} and {b:?} share text");
            }
        }
    }
}
