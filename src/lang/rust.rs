//! Rust language spec. Diverges on: lifetime ticks.

use crate::lang::spec::{LangSpec, StripFamily, DEFAULT_DEFS};

const CALLEE_QUERY: &str = concat!(
    "(call_expression function: (identifier) @callee)\n",
    "(call_expression function: (field_expression field: (field_identifier) @callee))\n",
    "(call_expression function: (scoped_identifier name: (identifier) @callee))\n",
    "(macro_invocation macro: (identifier) @callee)\n",
);

// A method call such as `self.keep()` contains this `field_expression`, so one pattern covers both.
const SIBLING_QUERY: &str = "(field_expression value: (self) field: (field_identifier) @ref)\n";

pub(crate) const SPEC: LangSpec = LangSpec {
    display: "Rust",
    extensions: &["rs"],
    filenames: &[],
    grammar: Some(tree_sitter_rust::LANGUAGE),
    callee_query: Some(CALLEE_QUERY),
    sibling_query: Some(SIBLING_QUERY),
    manifests: &["Cargo.toml"],
    has_lifetimes: true,
    strip_family: Some(StripFamily::Rust),
    extract_receiver: None,
    definitions: DEFAULT_DEFS,
    definition_wrappers: crate::lang::spec::DEFAULT_DEFINITION_WRAPPERS,
    canonical_anchor: crate::lang::spec::default_canonical_anchor,
    attach_leading_adornment: crate::lang::rust::attach_leading_adornment,
    policy: crate::lang::spec::LanguagePolicy {
        import_line,
        import_external,
        import_resolver: resolve_import,
        structural: Some(ast_grep_language::SupportLang::Rust),
        search_priority: 9,
        search_extensions: &["rs"],
        basename_extensions: &["rs"],
        ..crate::lang::spec::DEFAULT_POLICY
    },
    semantic_start: crate::lang::spec::default_semantic_start,
};

pub(crate) fn attach_leading_adornment(
    adornment: tree_sitter::Node,
    _definition: Option<tree_sitter::Node>,
    _lines: &[&str],
) -> bool {
    adornment.kind() == "attribute_item"
}

fn import_line(line: &str) -> bool {
    line.trim_start().starts_with("use ")
}

fn import_external(source: &str) -> bool {
    !(source.starts_with("crate::")
        || source.starts_with("self::")
        || source.starts_with("super::"))
}

fn resolve_import(dir: &std::path::Path, source: &str) -> Option<std::path::PathBuf> {
    if let Some(rest) = source.strip_prefix("crate::") {
        try_import_path(find_src_ancestor(dir)?, rest)
    } else if let Some(rest) = source.strip_prefix("self::") {
        try_import_path(dir, rest)
    } else if let Some(rest) = source.strip_prefix("super::") {
        try_import_path(dir.parent()?, rest)
    } else {
        None
    }
}

fn try_import_path(base: &std::path::Path, rest: &str) -> Option<std::path::PathBuf> {
    let segments: Vec<&str> = rest.split("::").collect();
    for len in (1..=segments.len()).rev() {
        let relative: std::path::PathBuf = segments[..len].iter().collect();
        let base = base.join(relative);
        let file = base.with_extension("rs");
        if file.exists() {
            return Some(file);
        }
        let module = base.join("mod.rs");
        if module.exists() {
            return Some(module);
        }
    }
    None
}

fn find_src_ancestor(start: &std::path::Path) -> Option<&std::path::Path> {
    let mut current = start;
    loop {
        if current.file_name().and_then(|name| name.to_str()) == Some("src") {
            return Some(current);
        }
        current = current.parent()?;
    }
}
