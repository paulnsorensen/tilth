//! PHP language spec.

use crate::lang::spec::{LangSpec, StdlibRule, DEFAULT_DEFS, DEFAULT_DEF_KINDS};

const CALLEE_QUERY: &str = concat!(
    "(function_call_expression function: (name) @callee)\n",
    "(function_call_expression function: (qualified_name) @callee)\n",
    "(function_call_expression function: (relative_name) @callee)\n",
    "(member_call_expression name: (name) @callee)\n",
    "(nullsafe_member_call_expression name: (name) @callee)\n",
    "(scoped_call_expression name: (name) @callee)\n",
);

pub(crate) const SPEC: LangSpec = LangSpec {
    display: "PHP",
    extensions: &["php", "phtml"],
    filenames: &[],
    grammar: Some(tree_sitter_php::LANGUAGE_PHP),
    callee_query: Some(CALLEE_QUERY),
    sibling_query: None,
    stdlib: StdlibRule::None,
    scoped_imports: false,
    manifests: &["composer.json"],
    definition_kinds: DEFAULT_DEF_KINDS,
    has_lifetimes: false,
    strip_family: None,
    extract_receiver: None,
    definitions: DEFAULT_DEFS,
    definition_wrappers: crate::lang::spec::DEFAULT_DEFINITION_WRAPPERS,
    canonical_anchor: crate::lang::php::canonical_anchor,
    attach_leading_adornment: crate::lang::php::attach_leading_adornment,
    semantic_start: crate::lang::spec::embedded_semantic_start,
};

pub(crate) fn canonical_anchor(node: tree_sitter::Node) -> tree_sitter::Node {
    crate::lang::spec::keyword_canonical_anchor(
        node,
        &["class", "interface", "trait", "enum", "function"],
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
            "attribute",
            "abstract_modifier",
            "final_modifier",
            "readonly_modifier",
            "static_modifier",
            "var_modifier",
            "visibility_modifier",
        ],
    )
}
