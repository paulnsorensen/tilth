//! Ruby language spec.

use crate::lang::spec::{LangSpec, DEFAULT_DEFS};

const CALLEE_QUERY: &str = "(call method: (identifier) @callee)\n";

pub(crate) const SPEC: LangSpec = LangSpec {
    display: "Ruby",
    extensions: &["rb"],
    filenames: &["Vagrantfile", "Rakefile"],
    grammar: Some(tree_sitter_ruby::LANGUAGE),
    callee_query: Some(CALLEE_QUERY),
    sibling_query: None,
    manifests: &[],
    has_lifetimes: false,
    strip_family: None,
    extract_receiver: None,
    definitions: DEFAULT_DEFS,
    definition_wrappers: crate::lang::spec::DEFAULT_DEFINITION_WRAPPERS,
    canonical_anchor: crate::lang::spec::default_canonical_anchor,
    attach_leading_adornment: crate::lang::spec::default_attach_leading_adornment,
    policy: crate::lang::spec::LanguagePolicy {
        structural: Some(ast_grep_language::SupportLang::Ruby),
        search_priority: 9,
        search_extensions: &["rb"],
        basename_extensions: &["rb"],
        ..crate::lang::spec::DEFAULT_POLICY
    },
    semantic_start: crate::lang::spec::default_semantic_start,
};
