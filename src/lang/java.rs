//! Java language spec.

use crate::lang::spec::{LangSpec, StdlibRule, StripFamily, DEFAULT_DEFS, DEFAULT_DEF_KINDS};

const CALLEE_QUERY: &str = "(method_invocation name: (identifier) @callee)\n";

const SIBLING_QUERY: &str = concat!(
    "(field_access object: (this) field: (identifier) @ref)\n",
    "(method_invocation object: (this) name: (identifier) @ref)\n",
);

pub(crate) const SPEC: LangSpec = LangSpec {
    display: "Java",
    extensions: &["java"],
    filenames: &[],
    grammar: Some(tree_sitter_java::LANGUAGE),
    callee_query: Some(CALLEE_QUERY),
    sibling_query: Some(SIBLING_QUERY),
    stdlib: StdlibRule::None,
    scoped_imports: false,
    manifests: &["pom.xml", "build.gradle"],
    definition_kinds: DEFAULT_DEF_KINDS,
    has_lifetimes: false,
    strip_family: Some(StripFamily::JavaKotlinCSharp),
    extract_receiver: None,
    definitions: DEFAULT_DEFS,
    definition_wrappers: crate::lang::spec::DEFAULT_DEFINITION_WRAPPERS,
    canonical_anchor: crate::lang::java::canonical_anchor,
    attach_leading_adornment: crate::lang::java::attach_leading_adornment,
    semantic_start: crate::lang::spec::embedded_semantic_start,
};

fn canonical_anchor(node: tree_sitter::Node) -> tree_sitter::Node {
    crate::lang::spec::keyword_canonical_anchor(
        node,
        &["class", "interface", "enum", "record", "module"],
    )
}

fn attach_leading_adornment(
    adornment: tree_sitter::Node,
    _definition: Option<tree_sitter::Node>,
    _lines: &[&str],
) -> bool {
    crate::lang::spec::adornment_kind(adornment, &["modifiers", "annotation", "marker_annotation"])
}
