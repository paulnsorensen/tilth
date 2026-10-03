//! C# language spec.

use crate::lang::spec::{LangSpec, StripFamily, DEFAULT_DEFS};

const CALLEE_QUERY: &str = concat!(
    "(invocation_expression function: (identifier) @callee)\n",
    "(invocation_expression function: (member_access_expression name: (identifier) @callee))\n",
);

// tree-sitter-c-sharp 0.23 parses `this` as an anonymous token.
// A call such as `this.Keep()` contains this `member_access_expression`, so one pattern covers both.
const SIBLING_QUERY: &str =
    "(member_access_expression expression: \"this\" name: (identifier) @ref)\n";

pub(crate) const SPEC: LangSpec = LangSpec {
    display: "C#",
    extensions: &["cs"],
    filenames: &[],
    grammar: Some(tree_sitter_c_sharp::LANGUAGE),
    callee_query: Some(CALLEE_QUERY),
    sibling_query: Some(SIBLING_QUERY),
    manifests: &[],
    has_lifetimes: false,
    strip_family: Some(StripFamily::JavaKotlinCSharp),
    extract_receiver: None,
    definitions: DEFAULT_DEFS,
    definition_wrappers: crate::lang::spec::DEFAULT_DEFINITION_WRAPPERS,
    canonical_anchor: crate::lang::csharp::canonical_anchor,
    attach_leading_adornment: crate::lang::csharp::attach_leading_adornment,
    policy: crate::lang::spec::LanguagePolicy {
        structural: Some(ast_grep_language::SupportLang::CSharp),
        search_priority: 9,
        search_extensions: &["cs"],
        basename_extensions: &["cs"],
        ..crate::lang::spec::DEFAULT_POLICY
    },
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
