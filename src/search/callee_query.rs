//! Per-language tree-sitter query selection for caller and callee search.

use crate::types::Lang;

/// Return the tree-sitter query string for extracting callee names in the given language.
/// Each language has patterns targeting `@callee` captures on call-like expressions.
pub(super) fn callee_query_str(lang: Lang) -> Option<&'static str> {
    crate::lang::spec::spec(lang).callee_query
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn kotlin_callee_query_compiles() {
        let lang: tree_sitter::Language = tree_sitter_kotlin_ng::LANGUAGE.into();
        let query_str = callee_query_str(Lang::Kotlin).unwrap();
        tree_sitter::Query::new(&lang, query_str).expect("kotlin callee query should compile");
    }

    #[test]
    fn elixir_callee_query_compiles() {
        let lang: tree_sitter::Language = tree_sitter_elixir::LANGUAGE.into();
        let query_str = callee_query_str(Lang::Elixir).unwrap();
        tree_sitter::Query::new(&lang, query_str).expect("elixir callee query should compile");
    }

    #[test]
    fn bash_callee_query_compiles() {
        let lang: tree_sitter::Language = tree_sitter_bash::LANGUAGE.into();
        let query_str = callee_query_str(Lang::Bash).unwrap();
        tree_sitter::Query::new(&lang, query_str).expect("bash callee query should compile");
    }
}
