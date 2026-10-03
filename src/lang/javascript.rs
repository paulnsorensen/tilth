//! JavaScript language spec. Shares callee/sibling queries with TS/TSX.

use crate::lang::spec::{LangSpec, StripFamily, DEFAULT_DEFS};

/// Callee query shared by JavaScript, TypeScript, and TSX.
pub(crate) const CALLEE_QUERY: &str = concat!(
    "(call_expression function: (identifier) @callee)\n",
    "(call_expression function: (member_expression property: (property_identifier) @callee))\n",
);

/// Sibling (`this.x`) query shared by JavaScript, TypeScript, and TSX.
pub(crate) const SIBLING_QUERY: &str =
    "(member_expression object: (this) property: (property_identifier) @ref)\n";

/// Transparent declaration wrappers shared by JavaScript, TypeScript, and TSX.
pub(crate) const DEFINITION_WRAPPERS: &[&str] = &["export_statement"];

pub(crate) const SPEC: LangSpec = LangSpec {
    display: "JavaScript",
    extensions: &["js", "jsx"],
    filenames: &[],
    grammar: Some(tree_sitter_javascript::LANGUAGE),
    callee_query: Some(CALLEE_QUERY),
    sibling_query: Some(SIBLING_QUERY),
    manifests: &["package.json"],
    has_lifetimes: false,
    strip_family: Some(StripFamily::JsTs),
    extract_receiver: None,
    definitions: DEFAULT_DEFS,
    definition_wrappers: crate::lang::javascript::DEFINITION_WRAPPERS,
    canonical_anchor: crate::lang::javascript::canonical_anchor,
    attach_leading_adornment: crate::lang::javascript::attach_leading_adornment,
    policy: crate::lang::spec::LanguagePolicy {
        structural: Some(ast_grep_language::SupportLang::JavaScript),
        import_line,
        import_external,
        import_resolver: resolve_import,
        search_priority: 7,
        search_extensions: &["js", "jsx", "mjs", "cjs"],
        basename_extensions: &["js", "jsx"],
        ..crate::lang::spec::DEFAULT_POLICY
    },
    semantic_start: crate::lang::spec::embedded_semantic_start,
};

pub(crate) fn canonical_anchor(node: tree_sitter::Node) -> tree_sitter::Node {
    crate::lang::spec::keyword_canonical_anchor(
        node,
        &[
            "class",
            "function",
            "interface",
            "enum",
            "const",
            "let",
            "var",
        ],
    )
}

pub(crate) fn attach_leading_adornment(
    adornment: tree_sitter::Node,
    _definition: Option<tree_sitter::Node>,
    _lines: &[&str],
) -> bool {
    crate::lang::spec::adornment_kind(adornment, &["decorator"])
}

pub(crate) fn import_line(line: &str) -> bool {
    let trimmed = line.trim_start();
    trimmed.starts_with("import ") || trimmed.starts_with("import{")
}

pub(crate) fn import_external(source: &str) -> bool {
    !(source.starts_with('.') || source.starts_with("@/") || source.starts_with("~/"))
}

pub(crate) fn resolve_import(dir: &std::path::Path, source: &str) -> Option<std::path::PathBuf> {
    let base = dir.join(source);
    if matches!(
        base.extension().and_then(|ext| ext.to_str()),
        Some("js" | "jsx")
    ) {
        if base.is_file() {
            return Some(base);
        }
        for extension in ["ts", "tsx"] {
            let candidate = base.with_extension(extension);
            if candidate.is_file() {
                return Some(candidate);
            }
        }
    }
    for extension in [".ts", ".tsx", ".js", ".jsx"] {
        let candidate = std::path::PathBuf::from(format!("{}{extension}", base.display()));
        if candidate.exists() {
            return Some(candidate);
        }
    }
    if base.exists() && base.is_file() {
        return Some(base);
    }
    for name in ["index.ts", "index.tsx", "index.js", "index.jsx"] {
        let candidate = base.join(name);
        if candidate.exists() {
            return Some(candidate);
        }
    }
    None
}
