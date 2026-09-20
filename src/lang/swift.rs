//! Swift language spec.

use crate::lang::spec::{LangSpec, StdlibRule, DEFAULT_DEFS, DEFAULT_DEF_KINDS};

const CALLEE_QUERY: &str = concat!(
    "(call_expression (simple_identifier) @callee)\n",
    "(call_expression (navigation_expression suffix: (navigation_suffix suffix: (simple_identifier) @callee)))\n",
);

const SIBLING_QUERY: &str =
    "(navigation_expression target: (self_expression) suffix: (navigation_suffix suffix: (simple_identifier) @ref))\n";

pub(crate) const SPEC: LangSpec = LangSpec {
    display: "Swift",
    extensions: &["swift"],
    filenames: &[],
    grammar: Some(tree_sitter_swift::LANGUAGE),
    callee_query: Some(CALLEE_QUERY),
    sibling_query: Some(SIBLING_QUERY),
    stdlib: StdlibRule::None,
    scoped_imports: false,
    manifests: &[],
    definition_kinds: DEFAULT_DEF_KINDS,
    has_lifetimes: false,
    strip_family: None,
    extract_receiver: None,
    definitions: DEFAULT_DEFS,
    definition_wrappers: crate::lang::spec::DEFAULT_DEFINITION_WRAPPERS,
    canonical_anchor: crate::lang::swift::canonical_anchor,
    attach_leading_adornment: crate::lang::swift::attach_leading_adornment,
    semantic_start: crate::lang::spec::embedded_semantic_start,
};

pub(crate) fn canonical_anchor(node: tree_sitter::Node) -> tree_sitter::Node {
    crate::lang::spec::keyword_canonical_anchor(
        node,
        &[
            "class", "struct", "enum", "protocol", "func", "init", "deinit", "var", "let",
        ],
    )
}

pub(crate) fn attach_leading_adornment(
    adornment: tree_sitter::Node,
    _definition: Option<tree_sitter::Node>,
    _lines: &[&str],
) -> bool {
    crate::lang::spec::adornment_kind(adornment, &["attribute", "modifiers"])
}
