//! Per-language tree-sitter query selection for caller and callee search.

use crate::types::Lang;

/// Return the tree-sitter query string for extracting callee names in the given language.
/// Each language has patterns targeting `@callee` captures on call-like expressions.
pub(super) fn callee_query_str(lang: Lang) -> Option<&'static str> {
    crate::lang::spec::spec(lang).callee_query
}

#[cfg(test)]
mod tests {
    use tree_sitter::{CaptureQuantifier, Query};

    /// A query that does not compile returns no matches with no error.
    /// A pattern without a capture that search reads drops its matches with no error.
    /// `query_captures` keeps only the first node of each capture, so the test refuses a repeated capture.
    #[test]
    fn every_call_and_member_query_compiles() {
        for lang in crate::lang::ALL_LANGS {
            let spec = crate::lang::spec::spec(*lang);
            let Some(grammar) = crate::lang::outline::outline_language(*lang) else {
                continue;
            };
            let mut sibling_names = vec!["ref"];
            if spec.policy.sibling_object.is_some() {
                sibling_names.push("obj");
            }
            if spec.extract_receiver.is_some() {
                sibling_names.push("recv");
            }
            let queries = [
                (spec.callee_query, vec!["callee"]),
                (spec.sibling_query, sibling_names),
            ];
            for (source, required) in queries {
                let Some(source) = source else {
                    continue;
                };
                let query = Query::new(&grammar, source)
                    .unwrap_or_else(|error| panic!("{lang:?}: {error}"));
                for pattern in 0..query.pattern_count() {
                    let quantifiers = query.capture_quantifiers(pattern);
                    for name in &required {
                        let index = query
                            .capture_index_for_name(name)
                            .unwrap_or_else(|| panic!("{lang:?}: no @{name} capture"));
                        assert_eq!(
                            quantifiers[index as usize],
                            CaptureQuantifier::One,
                            "{lang:?} pattern {pattern}: @{name}"
                        );
                    }
                    for quantifier in quantifiers {
                        assert!(
                            matches!(
                                quantifier,
                                CaptureQuantifier::Zero
                                    | CaptureQuantifier::One
                                    | CaptureQuantifier::ZeroOrOne
                            ),
                            "{lang:?} pattern {pattern}: repeated capture"
                        );
                    }
                }
            }
        }
    }
}
