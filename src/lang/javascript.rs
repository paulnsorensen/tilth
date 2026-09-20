//! JavaScript language spec. Shares callee/sibling queries with TS/TSX.

use crate::lang::spec::{LangSpec, StdlibRule, StripFamily, DEFAULT_DEFS, DEFAULT_DEF_KINDS};

/// Callee query shared by JavaScript, TypeScript, and TSX.
pub(crate) const CALLEE_QUERY: &str = concat!(
    "(call_expression function: (identifier) @callee)\n",
    "(call_expression function: (member_expression property: (property_identifier) @callee))\n",
);

/// Sibling (`this.x`) query shared by JavaScript, TypeScript, and TSX.
pub(crate) const SIBLING_QUERY: &str =
    "(member_expression object: (this) property: (property_identifier) @ref)\n";

/// Transparent declaration wrappers shared by JavaScript, TypeScript, and TSX.
pub(crate) const DEFINITION_WRAPPERS: &[&str] = &["export_statement"];

pub(crate) const SPEC: LangSpec = LangSpec {
    display: "JavaScript",
    extensions: &["js", "jsx"],
    filenames: &[],
    grammar: Some(tree_sitter_javascript::LANGUAGE),
    callee_query: Some(CALLEE_QUERY),
    sibling_query: Some(SIBLING_QUERY),
    stdlib: StdlibRule::None,
    scoped_imports: false,
    manifests: &["package.json"],
    definition_kinds: DEFAULT_DEF_KINDS,
    has_lifetimes: false,
    strip_family: Some(StripFamily::JsTs),
    extract_receiver: None,
    definitions: DEFAULT_DEFS,
    definition_wrappers: crate::lang::javascript::DEFINITION_WRAPPERS,
    canonical_anchor: crate::lang::javascript::canonical_anchor,
    attach_leading_adornment: crate::lang::javascript::attach_leading_adornment,
    semantic_start: crate::lang::spec::embedded_semantic_start,
};

pub(crate) fn canonical_anchor(node: tree_sitter::Node) -> tree_sitter::Node {
    crate::lang::spec::keyword_canonical_anchor(
        node,
        &[
            "class",
            "function",
            "interface",
            "enum",
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
