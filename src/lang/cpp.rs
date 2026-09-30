//! C++ language spec. Shares its callee query with C.

use crate::lang::c::CALLEE_QUERY;
use crate::lang::spec::{LangSpec, StdlibRule, StripFamily, DEFAULT_DEFS};

pub(crate) const SPEC: LangSpec = LangSpec {
    display: "C++",
    extensions: &["cpp", "hpp", "cc", "cxx"],
    filenames: &[],
    grammar: Some(tree_sitter_cpp::LANGUAGE),
    callee_query: Some(CALLEE_QUERY),
    sibling_query: None,
    stdlib: StdlibRule::None,
    scoped_imports: false,
    manifests: &[],
    has_lifetimes: false,
    strip_family: Some(StripFamily::CppC),
    extract_receiver: None,
    definitions: crate::lang::spec::DefinitionOps {
        qualified_owner,
        ..DEFAULT_DEFS
    },
    definition_wrappers: crate::lang::spec::DEFAULT_DEFINITION_WRAPPERS,
    canonical_anchor: crate::lang::cpp::canonical_anchor,
    attach_leading_adornment: crate::lang::spec::default_attach_leading_adornment,
    policy: crate::lang::spec::LanguagePolicy {
        import_line: crate::lang::c::import_line,
        import_external: crate::lang::c::import_external,
        import_resolver: crate::lang::c::resolve_import,
        search_priority: 9,
        search_extensions: &["cpp"],
        basename_extensions: &["cpp"],
        ..crate::lang::spec::DEFAULT_POLICY
    },
    semantic_start: crate::lang::spec::embedded_semantic_start,
};

pub(crate) fn canonical_anchor(node: tree_sitter::Node) -> tree_sitter::Node {
    if node.kind() == "function_definition" {
        node.child_by_field_name("type").unwrap_or(node)
    } else {
        node
    }
}

fn qualified_owner(node: tree_sitter::Node, lines: &[&str]) -> Option<String> {
    let mut pending = vec![node.child_by_field_name("declarator")?];
    while let Some(current) = pending.pop() {
        if current.kind() == "qualified_identifier" {
            if let Some(name) = current.child_by_field_name("name") {
                if name.kind() == "qualified_identifier" {
                    pending.push(name);
                    continue;
                }
            }
            let scope = current.child_by_field_name("scope")?;
            return Some(crate::lang::treesitter::node_text_simple(
                scope,
                lines,
                crate::lang::treesitter::NodeTextMode::Full,
            ));
        }
        for field in ["name", "declarator"].into_iter().rev() {
            if let Some(child) = current.child_by_field_name(field) {
                pending.push(child);
            }
        }
    }
    None
}
