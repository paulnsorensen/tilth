//! Per-language data table. One `LangSpec` per `Lang` variant, looked up by the
//! single `spec(lang)` match — the only surviving full-`Lang` dispatch in the
//! crate. Every former `match lang` site reads a field on `spec(lang)` instead.
//!
//! Shared, language-agnostic behavior (the default definition kinds, default
//! definition-name extraction and weight) lives once as `DEFAULT_*` items; only
//! the languages that diverge carry their own values.

use tree_sitter_language::LanguageFn;

use crate::lang::treesitter::{
    definition_weight as default_definition_weight,
    extract_definition_name as default_extract_definition_name, DEFINITION_KINDS,
};
use crate::types::Lang;

/// A language-specific predicate for a leading declaration adornment.
///
/// The definition is `None` while the shared outline walk is collecting a
/// possible adornment and `Some` while it decides whether that adornment
/// belongs to the following definition.
pub(crate) type AttachLeadingAdornment =
    fn(tree_sitter::Node, Option<tree_sitter::Node>, &[&str]) -> bool;

/// Select the semantic ownership start for a declaration.
///
/// `canonical` is the language-owned declaration keyword/display anchor.
/// Implementations may include compatible embedded annotations or modifiers
/// when they are contiguous with that anchor.
pub(crate) type SemanticStart = fn(tree_sitter::Node, tree_sitter::Node, &[&str]) -> u32;

/// Shared default for declarations whose node starts at the canonical anchor.
pub(crate) fn default_semantic_start(
    node: tree_sitter::Node,
    _canonical: tree_sitter::Node,
    _lines: &[&str],
) -> u32 {
    node.start_position().row as u32 + 1
}

/// Select a declaration keyword token when a grammar exposes one.
pub(crate) fn keyword_canonical_anchor<'tree>(
    node: tree_sitter::Node<'tree>,
    keywords: &[&str],
) -> tree_sitter::Node<'tree> {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        if keywords.contains(&child.kind()) {
            return child;
        }
    }
    named_canonical_anchor(node)
}

/// Shared semantic-start implementation for declarations whose AST node embeds
/// annotations, modifiers, or multiline type syntax before the canonical
/// declaration token.
///
/// The declaration node bounds the valid header syntax. Blank lines and parsed
/// comments split that header, so only the contiguous suffix ending at the
/// canonical token belongs to the declaration.
pub(crate) fn embedded_semantic_start(
    node: tree_sitter::Node,
    canonical: tree_sitter::Node,
    lines: &[&str],
) -> u32 {
    let node_row = node.start_position().row;
    let canonical_row = canonical.start_position().row;
    if node_row >= canonical_row {
        return canonical_row as u32 + 1;
    }

    let mut comment_rows = Vec::new();
    collect_comment_rows(node, canonical.start_byte(), &mut comment_rows);

    let mut first_row = canonical_row;
    for row in (node_row..canonical_row).rev() {
        if lines.get(row).is_none_or(|line| line.trim().is_empty())
            || comment_rows
                .iter()
                .any(|&(start, end)| (start..=end).contains(&row))
        {
            break;
        }
        first_row = row;
    }
    first_row as u32 + 1
}

fn collect_comment_rows(
    node: tree_sitter::Node,
    before_byte: usize,
    rows: &mut Vec<(usize, usize)>,
) {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        if child.start_byte() >= before_byte {
            break;
        }
        if child.kind().contains("comment") {
            rows.push((child.start_position().row, child.end_position().row));
        } else {
            collect_comment_rows(child, before_byte, rows);
        }
    }
}

/// Recognise one of a language's AST adornment node kinds.
pub(crate) fn adornment_kind(node: tree_sitter::Node, kinds: &[&str]) -> bool {
    kinds.contains(&node.kind())
}

/// Select the canonical declaration anchor for a definition node.
///
/// The semantic span may begin at an annotation/attribute/wrapper, but the
/// canonical anchor remains the declaration's named child (or the node itself
/// for grammars without a named declaration child).
pub(crate) type CanonicalAnchor = fn(tree_sitter::Node) -> tree_sitter::Node;

/// All per-language data and behavior in one record. Read via `spec(lang)`.
pub(crate) struct LangSpec {
    /// Human-readable language name (`lang_display_name`).
    pub display: &'static str,
    /// File extensions that map to this language (`detect_file_type`).
    pub extensions: &'static [&'static str],
    /// Bare filenames that map to this language (`file_type_from_name`).
    pub filenames: &'static [&'static str],
    /// Tree-sitter grammar, or `None` for languages without a shipped grammar
    /// (Dockerfile / Make). `outline_language` does `.map(Into::into)`.
    pub grammar: Option<LanguageFn>,
    /// Tree-sitter query for callee extraction (`callee_query_str`).
    pub callee_query: Option<&'static str>,
    /// Tree-sitter query for self/this sibling references (`sibling_query_str`).
    pub sibling_query: Option<&'static str>,
    /// How to recognise a stdlib import for this language (`is_stdlib`).
    pub stdlib: StdlibRule,
    /// Whether imports are resolved through scoped module roots.
    pub scoped_imports: bool,
    /// Manifest filenames used for dependency discovery.
    pub manifests: &'static [&'static str],
    /// Tree-sitter node kinds that represent definitions.
    pub definition_kinds: &'static [&'static str],
    /// Whether this language's signatures use lifetime tick stripping.
    pub has_lifetimes: bool,
    /// Comment/log stripping family.
    pub strip_family: Option<StripFamily>,
    pub extract_receiver: Option<fn(&str, &tree_sitter::Language) -> Option<String>>,
    /// Definition-name extraction + semantic weight (Elixir overrides the defaults).
    pub definitions: DefinitionOps,
    /// Transparent definition wrapper node kinds.
    pub definition_wrappers: &'static [&'static str],
    /// Canonical declaration anchor policy for this language.
    pub canonical_anchor: CanonicalAnchor,
    /// Predicate for contiguous leading declaration adornments.
    pub attach_leading_adornment: AttachLeadingAdornment,
    /// Language-owned semantic ownership start for embedded adornments.
    pub semantic_start: SemanticStart,
}

/// Shared default for declarations whose node starts at the canonical anchor.
pub(crate) fn default_canonical_anchor(node: tree_sitter::Node) -> tree_sitter::Node {
    node
}

/// Shared policy for annotation-bearing declaration nodes with a `name` field.
pub(crate) fn named_canonical_anchor(node: tree_sitter::Node) -> tree_sitter::Node {
    node.child_by_field_name("name").unwrap_or(node)
}

/// Shared default for languages without transparent wrappers or leading
/// declaration adornments.
pub(crate) const DEFAULT_DEFINITION_WRAPPERS: &[&str] = &[];

pub(crate) fn default_attach_leading_adornment(
    _adornment: tree_sitter::Node,
    _definition: Option<tree_sitter::Node>,
    _lines: &[&str],
) -> bool {
    false
}

/// How an import source is recognised as standard-library (and thus noise that
/// `tilth_deps` suppresses). Replaces the per-language `is_stdlib` match. Each
/// variant encodes one language's historical rule byte-for-byte.
pub(crate) enum StdlibRule {
    /// Language has no stdlib suppression.
    None,
    /// Source matches if it starts with any of these prefixes (Rust:
    /// `std::` / `core::` / `alloc::`).
    Prefixes(&'static [&'static str]),
    /// Source matches if its first `'.'`-delimited segment is in `set` (Python).
    PythonSegment(&'static [&'static str]),
    /// Source matches if its first `'/'`-delimited segment is in `set` (Go:
    /// the import's root path segment is a stdlib package root).
    GoRoots(&'static [&'static str]),
}

impl StdlibRule {
    /// Returns `true` if `source` names a standard-library module under this rule.
    pub(crate) fn matches(&self, source: &str) -> bool {
        match self {
            StdlibRule::None => false,
            StdlibRule::Prefixes(prefixes) => prefixes.iter().any(|p| source.starts_with(p)),
            StdlibRule::PythonSegment(set) => {
                let first = source.split('.').next().unwrap_or("");
                set.contains(&first)
            }
            StdlibRule::GoRoots(set) => set.contains(&source.split('/').next().unwrap_or(source)),
        }
    }
}

/// Coarse 6-way grouping of comment / log syntax for `strip::strip_noise`.
/// Deliberately *not* a 1:1 sibling of `Lang` (many languages share a family).
#[derive(Debug, Clone, Copy)]
pub(crate) enum StripFamily {
    Rust,
    Python,
    Go,
    JsTs,
    JavaKotlinCSharp,
    CppC,
    Bash,
}

/// Definition-name extraction + semantic weight for a language. The default
/// (`DEFAULT_DEFS`) walks standard field names; Elixir overrides both because
/// its definitions are `call` nodes.
pub(crate) struct DefinitionOps {
    /// Extract the defined symbol name from a definition node.
    pub extract_name: fn(tree_sitter::Node, &[&str]) -> Option<String>,
    /// Semantic ranking weight for a definition node.
    pub weight: fn(tree_sitter::Node, &[&str]) -> u16,
}

/// Shared default definition kinds — used by every language except Elixir.
pub(crate) const DEFAULT_DEF_KINDS: &[&str] = DEFINITION_KINDS;

/// Default weight adapter: weight is keyed on the node kind, so the node's
/// `lines` argument is unused. Kept in the `(node, lines)` shape so every
/// `DefinitionOps.weight` has one uniform signature.
fn default_defs_weight(node: tree_sitter::Node, _lines: &[&str]) -> u16 {
    default_definition_weight(node.kind())
}

/// Shared default `DefinitionOps` — referenced by every non-Elixir spec.
pub(crate) const DEFAULT_DEFS: DefinitionOps = DefinitionOps {
    extract_name: default_extract_definition_name,
    weight: default_defs_weight,
};

/// The single surviving full-`Lang` dispatch. Every other former `match lang`
/// reads a field on the returned spec.
pub(crate) fn spec(lang: Lang) -> &'static LangSpec {
    use crate::lang;
    match lang {
        Lang::Rust => &lang::rust::SPEC,
        Lang::TypeScript => &lang::typescript::SPEC,
        Lang::Tsx => &lang::tsx::SPEC,
        Lang::JavaScript => &lang::javascript::SPEC,
        Lang::Python => &lang::python::SPEC,
        Lang::Go => &lang::go::SPEC,
        Lang::Java => &lang::java::SPEC,
        Lang::Scala => &lang::scala::SPEC,
        Lang::C => &lang::c::SPEC,
        Lang::Cpp => &lang::cpp::SPEC,
        Lang::Ruby => &lang::ruby::SPEC,
        Lang::Php => &lang::php::SPEC,
        Lang::Swift => &lang::swift::SPEC,
        Lang::Kotlin => &lang::kotlin::SPEC,
        Lang::CSharp => &lang::csharp::SPEC,
        Lang::Elixir => &lang::elixir::SPEC,
        Lang::Dockerfile => &lang::dockerfile::SPEC,
        Lang::Make => &lang::make::SPEC,
        Lang::Bash => &lang::bash::SPEC,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::lang::mod_all_langs_for_test;

    // ── StdlibRule::matches — locks each variant's historical `is_stdlib` rule ──

    #[test]
    fn stdlib_none_never_matches() {
        let rule = StdlibRule::None;
        assert!(!rule.matches("std"));
        assert!(!rule.matches(""));
        assert!(!rule.matches("anything.at.all"));
    }

    #[test]
    fn stdlib_prefixes_matches_only_listed_prefixes() {
        let rule = StdlibRule::Prefixes(&["std::", "core::", "alloc::"]);
        assert!(rule.matches("std::collections::HashMap"));
        assert!(rule.matches("core::mem"));
        assert!(rule.matches("alloc::vec::Vec"));
        // Bare segment without `::` separator is not a prefix match.
        assert!(!rule.matches("std"));
        // A crate that merely starts with the letters but not the prefix.
        assert!(!rule.matches("standard::thing"));
        assert!(!rule.matches("serde::Deserialize"));
    }

    #[test]
    fn stdlib_python_segment_matches_first_dotted_segment() {
        let rule = StdlibRule::PythonSegment(&["os", "sys", "json"]);
        assert!(rule.matches("os"));
        assert!(rule.matches("os.path"));
        assert!(rule.matches("json.decoder"));
        // First segment must match exactly — a longer name sharing a prefix must not.
        assert!(!rule.matches("ossify"));
        assert!(!rule.matches("requests"));
        assert!(!rule.matches("mypackage.os"));
    }

    // Go's rule (post-PR-71): a GoRoots allowlist keyed on the first `/`-segment.
    // A multi-segment import (`net/http`) is stdlib when its root (`net`) is in the
    // set; a bare local package (`mypackage`) is NOT stdlib. These pin that rule.
    #[test]
    fn stdlib_go_root_segment_is_stdlib() {
        let rule = StdlibRule::GoRoots(&["fmt", "net", "encoding"]);
        assert!(rule.matches("fmt"), "bare stdlib root");
        assert!(rule.matches("net/http"), "multi-segment stdlib via root");
        assert!(
            rule.matches("encoding/json"),
            "multi-segment stdlib via root"
        );
    }

    #[test]
    fn stdlib_go_non_root_is_not_stdlib() {
        let rule = StdlibRule::GoRoots(&["fmt", "net"]);
        // Local/third-party roots are not in the set.
        assert!(
            !rule.matches("mypackage"),
            "bare local package is not stdlib"
        );
        assert!(!rule.matches("github.com/gin-gonic/gin"));
        assert!(!rule.matches("golang.org/x/sync"));
        // Empty string has no stdlib root.
        assert!(!rule.matches(""));
        // A root that merely shares a prefix with a stdlib root must not match.
        assert!(!rule.matches("fmtlib"));
    }

    // ── spec() table invariants — every Lang resolves and its data is coherent ──

    #[test]
    fn spec_resolves_for_every_lang() {
        // Exercises the single surviving full-`Lang` dispatch over the whole set.
        for &lang in mod_all_langs_for_test() {
            let s = spec(lang);
            assert!(!s.display.is_empty(), "{lang:?} has empty display name");
        }
    }

    #[test]
    fn only_dockerfile_and_make_lack_a_grammar() {
        for &lang in mod_all_langs_for_test() {
            let has_grammar = spec(lang).grammar.is_some();
            let expected = !matches!(lang, Lang::Dockerfile | Lang::Make);
            assert_eq!(
                has_grammar, expected,
                "{lang:?} grammar presence mismatch (Dockerfile/Make have no shipped grammar)"
            );
        }
    }

    #[test]
    fn only_rust_has_lifetimes() {
        for &lang in mod_all_langs_for_test() {
            assert_eq!(
                spec(lang).has_lifetimes,
                matches!(lang, Lang::Rust),
                "{lang:?} lifetime flag mismatch — only Rust uses `'` for lifetime ticks"
            );
        }
    }

    #[test]
    fn only_python_has_scoped_imports() {
        for &lang in mod_all_langs_for_test() {
            assert_eq!(
                spec(lang).scoped_imports,
                matches!(lang, Lang::Python),
                "{lang:?} scoped_imports mismatch — only Python resolves absolute in-scope imports"
            );
        }
    }

    #[test]
    fn only_go_extracts_a_receiver() {
        for &lang in mod_all_langs_for_test() {
            assert_eq!(
                spec(lang).extract_receiver.is_some(),
                matches!(lang, Lang::Go),
                "{lang:?} receiver-extractor presence mismatch — only Go has method receivers"
            );
        }
    }

    #[test]
    fn extensions_are_unique_across_langs() {
        // detect_file_type scans ALL_LANGS and returns the first lang whose
        // `spec.extensions` contains the ext. Overlapping extensions would make
        // detection order-dependent and silently misroute files.
        let mut seen: Vec<(&str, Lang)> = Vec::new();
        for &lang in mod_all_langs_for_test() {
            for &ext in spec(lang).extensions {
                if let Some((_, other)) = seen.iter().find(|(e, _)| *e == ext) {
                    panic!("extension {ext:?} claimed by both {other:?} and {lang:?}");
                }
                seen.push((ext, lang));
            }
        }
    }

    #[test]
    fn elixir_is_the_only_definition_override() {
        // The refactor's shared-default-plus-override design: every spec points at
        // DEFAULT_DEF_KINDS except Elixir, whose definitions are `call` nodes.
        for &lang in mod_all_langs_for_test() {
            let uses_default = spec(lang).definition_kinds == DEFAULT_DEF_KINDS;
            assert_eq!(
                uses_default,
                !matches!(lang, Lang::Elixir),
                "{lang:?} definition_kinds override mismatch — only Elixir diverges"
            );
        }
    }

    // ── Pinned per-language data the dispatch sites read ──

    #[test]
    fn pinned_spec_fields() {
        assert_eq!(spec(Lang::Rust).display, "Rust");
        assert_eq!(spec(Lang::Rust).extensions, &["rs"]);
        assert_eq!(spec(Lang::Rust).manifests, &["Cargo.toml"]);

        assert_eq!(spec(Lang::Go).extensions, &["go"]);
        assert_eq!(spec(Lang::Go).manifests, &["go.mod"]);
        assert!(spec(Lang::Go).extract_receiver.is_some());

        assert_eq!(spec(Lang::Python).extensions, &["py", "pyi"]);
        assert_eq!(
            spec(Lang::Python).manifests,
            &["pyproject.toml", "setup.py"]
        );

        assert_eq!(spec(Lang::Elixir).manifests, &["mix.exs"]);
        assert!(spec(Lang::Elixir).grammar.is_some());
    }
}
