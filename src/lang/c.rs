//! C language spec. Shares its callee query with C++.

use crate::lang::spec::{LangSpec, StdlibRule, StripFamily, DEFAULT_DEFS, DEFAULT_DEF_KINDS};

/// Callee query shared by C and C++.
pub(crate) const CALLEE_QUERY: &str = concat!(
    "(call_expression function: (identifier) @callee)\n",
    "(call_expression function: (field_expression field: (field_identifier) @callee))\n",
);

pub(crate) const SPEC: LangSpec = LangSpec {
    display: "C",
    extensions: &["c", "h"],
    filenames: &[],
    grammar: Some(tree_sitter_c::LANGUAGE),
    callee_query: Some(CALLEE_QUERY),
    sibling_query: None,
    stdlib: StdlibRule::None,
    scoped_imports: false,
    manifests: &[],
    definition_kinds: DEFAULT_DEF_KINDS,
    has_lifetimes: false,
    strip_family: Some(StripFamily::CppC),
    extract_receiver: None,
    definitions: DEFAULT_DEFS,
    definition_wrappers: crate::lang::spec::DEFAULT_DEFINITION_WRAPPERS,
    canonical_anchor: crate::lang::c::canonical_anchor,
    attach_leading_adornment: crate::lang::spec::default_attach_leading_adornment,
    semantic_start: crate::lang::spec::embedded_semantic_start,
};

pub(crate) fn canonical_anchor(node: tree_sitter::Node) -> tree_sitter::Node {
    if node.kind() == "function_definition" {
        node.child_by_field_name("type").unwrap_or(node)
    } else {
        node
    }
}
