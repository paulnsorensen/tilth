//! C# language spec.

use crate::lang::spec::{LangSpec, StdlibRule, StripFamily, DEFAULT_DEFS, DEFAULT_DEF_KINDS};

const CALLEE_QUERY: &str = concat!(
    "(invocation_expression function: (identifier) @callee)\n",
    "(invocation_expression function: (member_access_expression name: (identifier) @callee))\n",
);

const SIBLING_QUERY: &str = concat!(
    "(member_access_expression expression: (this_expression) name: (identifier) @ref)\n",
    "(invocation_expression function: (member_access_expression expression: (this_expression) name: (identifier) @ref))\n",
);

pub(crate) const SPEC: LangSpec = LangSpec {
    display: "C#",
    extensions: &["cs"],
    filenames: &[],
    grammar: Some(tree_sitter_c_sharp::LANGUAGE),
    callee_query: Some(CALLEE_QUERY),
    sibling_query: Some(SIBLING_QUERY),
    stdlib: StdlibRule::None,
    scoped_imports: false,
    manifests: &[],
    definition_kinds: DEFAULT_DEF_KINDS,
    has_lifetimes: false,
    strip_family: Some(StripFamily::JavaKotlinCSharp),
    extract_receiver: None,
    definitions: DEFAULT_DEFS,
    definition_wrappers: crate::lang::spec::DEFAULT_DEFINITION_WRAPPERS,
    canonical_anchor: crate::lang::csharp::canonical_anchor,
    attach_leading_adornment: crate::lang::csharp::attach_leading_adornment,
    semantic_start: crate::lang::spec::embedded_semantic_start,
};

pub(crate) fn canonical_anchor(node: tree_sitter::Node) -> tree_sitter::Node {
    crate::lang::spec::keyword_canonical_anchor(
        node,
        &["class", "struct", "interface", "enum", "record", "delegate"],
    )
}

pub(crate) fn attach_leading_adornment(
    adornment: tree_sitter::Node,
    _definition: Option<tree_sitter::Node>,
    _lines: &[&str],
) -> bool {
    crate::lang::spec::adornment_kind(
        adornment,
        &[
            "attribute_list",
            "modifier",
            "abstract_modifier",
            "readonly_modifier",
        ],
    )
}
