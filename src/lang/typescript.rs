//! TypeScript language spec. Shares callee/sibling queries with JS/TSX.

use crate::lang::javascript::{CALLEE_QUERY, SIBLING_QUERY};
use crate::lang::spec::{LangSpec, StdlibRule, StripFamily, DEFAULT_DEFS};

pub(crate) const SPEC: LangSpec = LangSpec {
    display: "TypeScript",
    extensions: &["ts"],
    filenames: &[],
    grammar: Some(tree_sitter_typescript::LANGUAGE_TYPESCRIPT),
    callee_query: Some(CALLEE_QUERY),
    sibling_query: Some(SIBLING_QUERY),
    stdlib: StdlibRule::None,
    scoped_imports: false,
    manifests: &[],
    has_lifetimes: false,
    strip_family: Some(StripFamily::JsTs),
    extract_receiver: None,
    definitions: DEFAULT_DEFS,
    definition_wrappers: crate::lang::javascript::DEFINITION_WRAPPERS,
    canonical_anchor: crate::lang::typescript::canonical_anchor,
    attach_leading_adornment: crate::lang::typescript::attach_leading_adornment,
    policy: crate::lang::spec::LanguagePolicy {
        import_line: crate::lang::javascript::import_line,
        import_external: crate::lang::javascript::import_external,
        import_resolver: crate::lang::javascript::resolve_import,
        structural: Some(ast_grep_language::SupportLang::TypeScript),
        search_priority: 10,
        search_extensions: &["ts"],
        basename_extensions: &["ts"],
        ..crate::lang::spec::DEFAULT_POLICY
    },
    semantic_start: crate::lang::spec::embedded_semantic_start,
};

pub(crate) fn canonical_anchor(node: tree_sitter::Node) -> tree_sitter::Node {
    crate::lang::spec::keyword_canonical_anchor(
        node,
        &[
            "class",
            "interface",
            "function",
            "enum",
            "type",
            "const",
            "let",
            "var",
        ],
    )
}

pub(crate) fn attach_leading_adornment(
    adornment: tree_sitter::Node,
    _definition: Option<tree_sitter::Node>,
    _lines: &[&str],
) -> bool {
    crate::lang::spec::adornment_kind(adornment, &["decorator"])
}
