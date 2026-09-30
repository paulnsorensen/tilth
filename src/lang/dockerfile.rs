//! Dockerfile spec. No tree-sitter grammar shipped — outline returns `None`.

use crate::lang::spec::{LangSpec, StdlibRule, DEFAULT_DEFS};

pub(crate) const SPEC: LangSpec = LangSpec {
    display: "Docker",
    extensions: &[],
    filenames: &["Dockerfile", "Containerfile"],
    grammar: None,
    callee_query: None,
    sibling_query: None,
    stdlib: StdlibRule::None,
    scoped_imports: false,
    manifests: &[],
    has_lifetimes: false,
    strip_family: None,
    extract_receiver: None,
    definitions: DEFAULT_DEFS,
    definition_wrappers: crate::lang::spec::DEFAULT_DEFINITION_WRAPPERS,
    canonical_anchor: crate::lang::spec::default_canonical_anchor,
    attach_leading_adornment: crate::lang::spec::default_attach_leading_adornment,
    policy: crate::lang::spec::DEFAULT_POLICY,
    semantic_start: crate::lang::spec::default_semantic_start,
};
