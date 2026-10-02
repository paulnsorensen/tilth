//! Per-language tree-sitter query selection for caller and callee search.

use crate::types::Lang;

/// Return the tree-sitter query string for extracting callee names in the given language.
/// Each language has patterns targeting `@callee` captures on call-like expressions.
pub(super) fn callee_query_str(lang: Lang) -> Option<&'static str> {
    crate::lang::spec::spec(lang).callee_query
}

#[cfg(test)]
mod tests {
    /// A query that fails to compile returns no matches with no error.
    #[test]
    fn every_call_and_member_query_compiles() {
        for lang in crate::lang::ALL_LANGS {
            let spec = crate::lang::spec::spec(*lang);
            let Some(grammar) = crate::lang::outline::outline_language(*lang) else {
                continue;
            };
            for query in [spec.callee_query, spec.sibling_query]
                .into_iter()
                .flatten()
            {
                tree_sitter::Query::new(&grammar, query)
                    .unwrap_or_else(|error| panic!("{lang:?}: {error}"));
            }
        }
    }
}
