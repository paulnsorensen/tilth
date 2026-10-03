//! Kotlin language spec.

use crate::lang::spec::{DefinitionOps, LangSpec, StripFamily, DEFAULT_DEFS};
use crate::lang::treesitter::{node_text_simple, NodeTextMode};

const CALLEE_QUERY: &str = concat!(
    "(call_expression (simple_identifier) @callee)\n",
    "(call_expression (navigation_expression (navigation_suffix (simple_identifier) @callee)))\n",
);

pub(crate) const SPEC: LangSpec = LangSpec {
    display: "Kotlin",
    extensions: &["kt", "kts"],
    filenames: &[],
    grammar: Some(tree_sitter_kotlin_sg::LANGUAGE),
    callee_query: Some(CALLEE_QUERY),
    sibling_query: None,
    manifests: &[],
    has_lifetimes: false,
    strip_family: Some(StripFamily::JavaKotlinCSharp),
    extract_receiver: None,
    definitions: DefinitionOps {
        extract_name: extract_definition_name,
        ..DEFAULT_DEFS
    },
    definition_wrappers: crate::lang::spec::DEFAULT_DEFINITION_WRAPPERS,
    canonical_anchor: crate::lang::kotlin::canonical_anchor,
    attach_leading_adornment: crate::lang::kotlin::attach_leading_adornment,
    policy: crate::lang::spec::LanguagePolicy {
        structural: Some(ast_grep_language::SupportLang::Kotlin),
        import_line,
        outline_label,
        search_priority: 9,
        search_extensions: &["kt"],
        basename_extensions: &["kt"],
        // Kotlin wraps all imports in one `import_list`; outline each header.
        outline_flatten: &["import_list"],
        ..crate::lang::spec::DEFAULT_POLICY
    },
    semantic_start: crate::lang::spec::embedded_semantic_start,
};

pub(crate) fn canonical_anchor(node: tree_sitter::Node) -> tree_sitter::Node {
    crate::lang::spec::keyword_canonical_anchor(
        node,
        &["class", "interface", "object", "fun", "typealias", "enum"],
    )
}

pub(crate) fn attach_leading_adornment(
    adornment: tree_sitter::Node,
    _definition: Option<tree_sitter::Node>,
    _lines: &[&str],
) -> bool {
    crate::lang::spec::adornment_kind(adornment, &["modifiers", "annotation"])
}

fn import_line(line: &str) -> bool {
    line.trim_start().starts_with("import ")
}

fn outline_label(kind: crate::types::OutlineKind) -> Option<&'static str> {
    match kind {
        crate::types::OutlineKind::Function => Some("fun"),
        crate::types::OutlineKind::Module => Some("object"),
        _ => None,
    }
}

/// The kotlin-sg grammar has no `name` fields. A declaration names itself with a
/// direct `simple_identifier` (functions, bindings) or `type_identifier` (types)
/// child that comes before any body. A `property_declaration` has no name of its
/// own: its `variable_declaration` child defines the name, and a destructuring
/// property defines only the destructured names.
pub(crate) fn extract_definition_name(node: tree_sitter::Node, lines: &[&str]) -> Option<String> {
    if node.kind() == "property_declaration" {
        return None;
    }
    let mut cursor = node.walk();
    for child in node.named_children(&mut cursor) {
        if matches!(child.kind(), "simple_identifier" | "type_identifier") {
            return Some(node_text_simple(child, lines, NodeTextMode::Full));
        }
        if child.kind().ends_with("_body") {
            return None;
        }
    }
    None
}
