//! Scala language spec.

use crate::lang::spec::{LangSpec, StdlibRule, StripFamily, DEFAULT_DEFS};

const CALLEE_QUERY: &str = concat!(
    "(call_expression function: (identifier) @callee)\n",
    "(call_expression function: (field_expression field: (identifier) @callee))\n",
    "(infix_expression operator: (identifier) @callee)\n",
);

// A call such as `this.keep()` contains this `field_expression`, so one pattern covers both.
const SIBLING_QUERY: &str = "(field_expression (identifier) @obj (identifier) @ref)\n";

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
    has_lifetimes: false,
    strip_family: Some(StripFamily::JavaKotlinCSharp),
    extract_receiver: None,
    definitions: DEFAULT_DEFS,
    definition_wrappers: crate::lang::spec::DEFAULT_DEFINITION_WRAPPERS,
    canonical_anchor: crate::lang::scala::canonical_anchor,
    attach_leading_adornment: crate::lang::scala::attach_leading_adornment,
    policy: crate::lang::spec::LanguagePolicy {
        structural: Some(ast_grep_language::SupportLang::Scala),
        import_line,
        outline_label,
        sibling_object: Some("this"),
        search_priority: 9,
        search_extensions: &["scala"],
        basename_extensions: &["scala"],
        ..crate::lang::spec::DEFAULT_POLICY
    },
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

fn import_line(line: &str) -> bool {
    line.trim_start().starts_with("import ")
}

fn outline_label(kind: crate::types::OutlineKind) -> Option<&'static str> {
    match kind {
        crate::types::OutlineKind::Function => Some("def"),
        crate::types::OutlineKind::Interface => Some("trait"),
        crate::types::OutlineKind::Variable => Some("var"),
        crate::types::OutlineKind::Module => Some("object"),
        _ => None,
    }
}
