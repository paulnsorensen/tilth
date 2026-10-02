//! Bash language spec.

use crate::lang::spec::{LangSpec, StdlibRule, StripFamily, DEFAULT_DEFS};

const CALLEE_QUERY: &str = "(command name: (command_name) @callee)\n";

pub(crate) const SPEC: LangSpec = LangSpec {
    display: "Bash",
    extensions: &["sh", "bash", "bats"],
    filenames: &[".bashrc", ".bash_profile", ".bash_aliases", ".profile"],
    grammar: Some(tree_sitter_bash::LANGUAGE),
    callee_query: Some(CALLEE_QUERY),
    sibling_query: None,
    stdlib: StdlibRule::None,
    scoped_imports: false,
    manifests: &[],
    has_lifetimes: false,
    strip_family: Some(StripFamily::Bash),
    extract_receiver: None,
    definitions: DEFAULT_DEFS,
    definition_wrappers: crate::lang::spec::DEFAULT_DEFINITION_WRAPPERS,
    canonical_anchor: crate::lang::spec::default_canonical_anchor,
    attach_leading_adornment: crate::lang::spec::default_attach_leading_adornment,
    policy: crate::lang::spec::LanguagePolicy {
        structural: Some(ast_grep_language::SupportLang::Bash),
        import_line,
        import_external,
        import_resolver: resolve_import,
        import_source: Some(crate::lang::outline::bash_import_source),
        special_outline: crate::lang::outline::bash_special_outline,
        ..crate::lang::spec::DEFAULT_POLICY
    },
    semantic_start: crate::lang::spec::default_semantic_start,
};

fn import_line(line: &str) -> bool {
    let trimmed = line.trim_start();
    trimmed
        .strip_prefix("source")
        .or_else(|| trimmed.strip_prefix('.'))
        .is_some_and(|rest| rest.starts_with(char::is_whitespace))
}

fn import_external(source: &str) -> bool {
    !source.starts_with('.')
}

fn resolve_import(dir: &std::path::Path, source: &str) -> Option<std::path::PathBuf> {
    let candidate = dir.join(source);
    candidate.metadata().ok()?.is_file().then_some(candidate)
}
