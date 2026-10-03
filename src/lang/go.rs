//! Go language spec. Diverges on: method-receiver extraction.

use crate::lang::spec::{LangSpec, StripFamily, DEFAULT_DEFS};

const CALLEE_QUERY: &str = concat!(
    "(call_expression function: (identifier) @callee)\n",
    "(call_expression function: (selector_expression field: (field_identifier) @callee))\n",
);

const SIBLING_QUERY: &str =
    "(selector_expression operand: (identifier) @recv field: (field_identifier) @ref)\n";

pub(crate) const SPEC: LangSpec = LangSpec {
    display: "Go",
    extensions: &["go"],
    filenames: &[],
    grammar: Some(tree_sitter_go::LANGUAGE),
    callee_query: Some(CALLEE_QUERY),
    sibling_query: Some(SIBLING_QUERY),
    manifests: &["go.mod"],
    has_lifetimes: false,
    strip_family: Some(StripFamily::Go),
    extract_receiver: Some(extract_go_receiver_name),
    definitions: crate::lang::spec::DefinitionOps {
        name_line: crate::lang::treesitter::go_declaration_name_line,
        ..DEFAULT_DEFS
    },
    canonical_anchor: crate::lang::spec::default_canonical_anchor,
    definition_wrappers: crate::lang::spec::DEFAULT_DEFINITION_WRAPPERS,
    attach_leading_adornment: crate::lang::spec::default_attach_leading_adornment,
    policy: crate::lang::spec::LanguagePolicy {
        structural: Some(ast_grep_language::SupportLang::Go),
        import_line,
        same_package: Some(crate::lang::spec::SamePackagePolicy {
            extension: "go",
            excluded_suffix: "_test.go",
            max_files: 20,
            max_file_size: 100_000,
        }),
        restore_grouped_name: true,
        search_priority: 9,
        search_extensions: &["go"],
        basename_extensions: &["go"],
        ..crate::lang::spec::DEFAULT_POLICY
    },
    semantic_start: crate::lang::spec::default_semantic_start,
};

/// For Go methods, extract the receiver parameter name from the first method
/// in the file. Go receiver is the first parameter in `func (r *Type) Name()`.
pub(crate) fn extract_go_receiver_name(
    content: &str,
    root: tree_sitter::Node,
    ts_lang: &tree_sitter::Language,
) -> Option<String> {
    const GO_RECV_QUERY: &str = "(method_declaration receiver: (parameter_list (parameter_declaration name: (identifier) @recv)))";

    let bytes = content.as_bytes();
    let mut receiver = None;
    crate::lang::treesitter::visit_query_captures(
        ts_lang,
        GO_RECV_QUERY,
        root,
        bytes,
        ["recv"],
        |[recv]| {
            receiver = recv.and_then(|node| node.utf8_text(bytes).ok().map(String::from));
            std::ops::ControlFlow::Break(())
        },
    );
    receiver
}

fn import_line(line: &str) -> bool {
    line.trim_start().starts_with("import ")
}
