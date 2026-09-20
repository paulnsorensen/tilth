//! Scala language spec.

use crate::lang::spec::{LangSpec, StdlibRule, StripFamily, DEFAULT_DEFS, DEFAULT_DEF_KINDS};

const CALLEE_QUERY: &str = concat!(
    "(call_expression function: (identifier) @callee)\n",
    "(call_expression function: (field_expression field: (identifier) @callee))\n",
    "(infix_expression operator: (identifier) @callee)\n",
);

const SIBLING_QUERY: &str = concat!(
    "(field_expression (identifier) @obj (identifier) @ref)\n",
    "(call_expression function: (field_expression (identifier) @obj (identifier) @ref))\n",
);

pub(crate) const SPEC: LangSpec = LangSpec {
    display: "Scala",
    extensions: &["scala", "sc"],
    filenames: &[],
    grammar: Some(tree_sitter_scala::LANGUAGE),
    callee_query: Some(CALLEE_QUERY),
    sibling_query: Some(SIBLING_QUERY),
    stdlib: StdlibRule::None,
    scoped_imports: false,
    manifests: &["build.sbt"],
    definition_kinds: DEFAULT_DEF_KINDS,
    has_lifetimes: false,
    strip_family: Some(StripFamily::JavaKotlinCSharp),
    extract_receiver: None,
    definitions: DEFAULT_DEFS,
    definition_wrappers: crate::lang::spec::DEFAULT_DEFINITION_WRAPPERS,
    canonical_anchor: crate::lang::scala::canonical_anchor,
    attach_leading_adornment: crate::lang::scala::attach_leading_adornment,
    semantic_start: crate::lang::spec::embedded_semantic_start,
};

pub(crate) fn canonical_anchor(node: tree_sitter::Node) -> tree_sitter::Node {
    crate::lang::spec::keyword_canonical_anchor(
        node,
        &["class", "trait", "object", "enum", "def", "val", "var"],
    )
}

pub(crate) fn attach_leading_adornment(
    adornment: tree_sitter::Node,
    _definition: Option<tree_sitter::Node>,
    _lines: &[&str],
) -> bool {
    crate::lang::spec::adornment_kind(adornment, &["annotation", "modifiers", "access_modifier"])
}
