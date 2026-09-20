//! TSX language spec. Shares callee/sibling queries with JS/TS.

use crate::lang::javascript::{CALLEE_QUERY, SIBLING_QUERY};
use crate::lang::spec::{LangSpec, StdlibRule, StripFamily, DEFAULT_DEFS, DEFAULT_DEF_KINDS};

pub(crate) const SPEC: LangSpec = LangSpec {
    display: "TSX",
    extensions: &["tsx"],
    filenames: &[],
    grammar: Some(tree_sitter_typescript::LANGUAGE_TSX),
    callee_query: Some(CALLEE_QUERY),
    sibling_query: Some(SIBLING_QUERY),
    stdlib: StdlibRule::None,
    scoped_imports: false,
    manifests: &[],
    definition_kinds: DEFAULT_DEF_KINDS,
    has_lifetimes: false,
    strip_family: Some(StripFamily::JsTs),
    extract_receiver: None,
    definitions: DEFAULT_DEFS,
    definition_wrappers: crate::lang::javascript::DEFINITION_WRAPPERS,
    canonical_anchor: crate::lang::tsx::canonical_anchor,
    attach_leading_adornment: crate::lang::tsx::attach_leading_adornment,
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
