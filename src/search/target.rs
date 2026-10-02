//! Definition-target resolution shared by search expansion and follow continuations.

use std::fs;
use std::path::{Path, PathBuf};

use crate::cache::{OutlineCache, ParsedFile};
use crate::error::TilthError;
use crate::lang::detect_file_type;
use crate::lang::outline::find_entry_by_start_line;
use crate::types::{FileType, Lang, OutlineEntry, OutlineKind};

#[derive(Debug, Clone)]
pub(crate) enum SourceSnapshot {
    Parsed(std::sync::Arc<ParsedFile>),
    Raw(std::sync::Arc<String>),
}

impl std::ops::Deref for SourceSnapshot {
    type Target = str;

    fn deref(&self) -> &Self::Target {
        match self {
            Self::Parsed(parsed) => parsed.content(),
            Self::Raw(content) => content.as_str(),
        }
    }
}

/// The definition a search candidate or follow target resolved to.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedTarget {
    pub name: String,
    pub path: PathBuf,
    pub start_line: u32,
    pub span_start_line: u32,
    pub end_line: u32,
    pub kind: OutlineKind,
    pub signature: Option<String>,
}

/// Resolve a path/line target by its stable source-byte occurrence identity.
/// The identity prevents same-line declarations from being re-resolved by line alone.
pub(crate) fn resolve_by_path_line_occurrence(
    path: &Path,
    line: u32,
    name: &str,
    occurrence: (usize, usize),
    scope: &Path,
    cache: &OutlineCache,
) -> Result<(ResolvedTarget, SourceSnapshot, Lang), TilthError> {
    let result = super::search_symbol_raw_cached(name, scope, None, cache)?;
    let canonical = path.canonicalize().map_err(|source| TilthError::IoError {
        path: path.to_path_buf(),
        source,
    })?;
    let candidate = result.matches.iter().find(|candidate| {
        candidate.is_definition
            && candidate.path.canonicalize().ok().as_ref() == Some(&canonical)
            && candidate.line == line
            && candidate.def_name.as_deref() == Some(name)
            && candidate.def_byte_range == Some(occurrence)
    });
    let Some(candidate) = candidate else {
        return Err(TilthError::NotFound {
            path: path.to_path_buf(),
            suggestion: Some("target occurrence changed; search again".to_string()),
        });
    };
    enrich_from_outline(
        candidate.path.clone(),
        candidate.line,
        candidate.def_range.map(|(_, end)| end),
        name.to_string(),
        false,
        cache,
    )
}

/// Resolve the deepest definition whose semantic span contains `line`.
pub(crate) fn resolve_by_path_line(
    path: &Path,
    line: u32,
    cache: &OutlineCache,
) -> Result<(ResolvedTarget, SourceSnapshot, Lang), TilthError> {
    let (content, lang) = read_code_file(path, cache)?;
    let (entries, deep_entry) = cache.parse_source(path, &content).map_or_else(
        || crate::lang::outline::get_outline_entries_and_entry_at_line(&content, lang, line),
        |parsed| {
            crate::lang::outline::outline_entries_and_entry_at_line_from_tree(
                parsed.content(),
                lang,
                parsed.tree(),
                line,
            )
        },
    );
    let target = match deep_entry
        .as_ref()
        .or_else(|| find_entry_at_line(&entries, line))
    {
        Some(entry) => target_from_entry(entry, path.to_path_buf()),
        None => {
            return Err(TilthError::NotFound {
                path: path.to_path_buf(),
                suggestion: Some(format!("no definition encloses line {line}")),
            });
        }
    };
    Ok((target, content, lang))
}

/// Read `path` and detect its language. Errors if the file isn't a code file;
/// target resolution needs source-level analysis, not markdown, config, or data.
fn read_code_file(path: &Path, cache: &OutlineCache) -> Result<(SourceSnapshot, Lang), TilthError> {
    if let Some(parsed) = cache.get_or_parse(path) {
        let lang = parsed.lang;
        return Ok((SourceSnapshot::Parsed(parsed), lang));
    }
    let content = fs::read_to_string(path).map_err(|e| TilthError::IoError {
        path: path.to_path_buf(),
        source: e,
    })?;
    let FileType::Code(lang) = detect_file_type(path) else {
        return Err(TilthError::InvalidQuery {
            query: path.display().to_string(),
            reason: "not a code file — target resolution needs source code".to_string(),
        });
    };
    Ok((SourceSnapshot::Raw(std::sync::Arc::new(content)), lang))
}

/// Read the file at `path`, find the outline entry that starts at `start_line`,
/// and convert it to `ResolvedTarget`. Unique search candidates can enable
/// `resolve_moved_name` to recover their exact name from the same fresh outline.
fn enrich_from_outline(
    path: PathBuf,
    start_line: u32,
    semantic_end: Option<u32>,
    name: String,
    resolve_moved_name: bool,
    cache: &OutlineCache,
) -> Result<(ResolvedTarget, SourceSnapshot, Lang), TilthError> {
    let (content, lang) = read_code_file(&path, cache)?;
    let (entries, exact_entry) = cache.parse_source(&path, &content).map_or_else(
        || {
            crate::lang::outline::get_outline_entries_and_entry_by_name_at_start_line(
                &content,
                lang,
                &name,
                start_line,
                semantic_end,
            )
        },
        |parsed| {
            crate::lang::outline::outline_entries_and_entry_by_name_from_tree(
                parsed.content(),
                lang,
                parsed.tree(),
                &name,
                start_line,
                semantic_end,
            )
        },
    );
    let mut target = if let Some(entry) = exact_entry
        .as_ref()
        .filter(|entry| entry.name == name)
        .or_else(|| {
            find_entry_by_start_line(&entries, start_line).filter(|entry| entry.name == name)
        }) {
        target_from_entry(entry, path)
    } else {
        // The outline tree caps its nesting at one container level, so a
        // deeply-nested definition (e.g. `a::b::method`) can be absent.
        let exact_entry = exact_entry.filter(|entry| !resolve_moved_name || entry.name == name);
        if let Some(entry) = exact_entry {
            target_from_entry(&entry, path)
        } else {
            // Name recovery is only for moved/renamed definitions. Ordinary
            // resolution must not silently substitute a same-line symbol.
            let fresh_name = resolve_moved_name
                .then(|| find_named_entry(&entries, &name))
                .flatten();
            match fresh_name {
                Some(entry) => target_from_entry(entry, path),
                None => match find_entry_at_line(&entries, start_line) {
                    Some(entry) => target_from_entry(entry, path),
                    None => ResolvedTarget {
                        name: name.clone(),
                        path,
                        start_line,
                        span_start_line: start_line,
                        end_line: start_line,
                        kind: OutlineKind::Function,
                        signature: None,
                    },
                },
            }
        }
    };
    if crate::lang::spec::spec(lang).policy.restore_grouped_name
        && target.name != name
        && (target.kind == OutlineKind::Constant || target.kind == OutlineKind::Variable)
    {
        target.name = name;
    }
    Ok((target, content, lang))
}

pub(crate) fn resolve_candidate_with_source(
    path: &Path,
    start_line: u32,
    semantic_end: Option<u32>,
    name: &str,
    cache: &OutlineCache,
) -> Result<(ResolvedTarget, SourceSnapshot, Lang), TilthError> {
    enrich_from_outline(
        path.to_path_buf(),
        start_line,
        semantic_end,
        name.to_string(),
        true,
        cache,
    )
}

pub(crate) fn resolve_candidate_with_source_occurrence(
    path: &Path,
    start_line: u32,
    semantic_end: Option<u32>,
    name: &str,
    occurrence: (usize, usize),
    cache: &OutlineCache,
) -> Result<(ResolvedTarget, SourceSnapshot, Lang), TilthError> {
    let scope = path.parent().unwrap_or_else(|| Path::new("."));
    let result = super::search_symbol_raw_cached(name, scope, None, cache)?;
    let canonical = path.canonicalize().map_err(|source| TilthError::IoError {
        path: path.to_path_buf(),
        source,
    })?;
    let candidate = result.matches.iter().find(|candidate| {
        candidate.is_definition
            && candidate.path.canonicalize().ok().as_ref() == Some(&canonical)
            && candidate.line == start_line
            && candidate.def_name.as_deref() == Some(name)
            && candidate.def_byte_range == Some(occurrence)
    });
    let Some(candidate) = candidate else {
        return Err(TilthError::NotFound {
            path: path.to_path_buf(),
            suggestion: Some("target occurrence changed; search again".to_string()),
        });
    };
    enrich_from_outline(
        candidate.path.clone(),
        candidate.line,
        semantic_end.or_else(|| candidate.def_range.map(|(_, end)| end)),
        name.to_string(),
        false,
        cache,
    )
}

fn target_from_entry(entry: &OutlineEntry, path: PathBuf) -> ResolvedTarget {
    ResolvedTarget {
        name: entry.name.clone(),
        path,
        start_line: entry.start_line,
        span_start_line: entry.span_start_line,
        end_line: entry.end_line,
        kind: entry.kind,
        signature: entry.signature.clone(),
    }
}

/// Walk the outline tree and return the deepest entry whose semantic ownership
/// range contains `line`.
fn find_entry_at_line(entries: &[OutlineEntry], line: u32) -> Option<&OutlineEntry> {
    let mut best: Option<&OutlineEntry> = None;
    for e in entries {
        if line >= e.span_start_line && line <= e.end_line {
            if let Some(deeper) = find_entry_at_line(&e.children, line) {
                return Some(deeper);
            }
            best = Some(e);
        }
    }
    best
}

fn find_named_entry<'a>(entries: &'a [OutlineEntry], name: &str) -> Option<&'a OutlineEntry> {
    for entry in entries {
        if entry.name == name {
            return Some(entry);
        }
        if let Some(found) = find_named_entry(&entry.children, name) {
            return Some(found);
        }
    }
    None
}

/// A sibling definition (peer method on the same parent, or peer top-level def
/// in the same file). Signature only — never the body.
#[derive(Debug, Clone)]
pub struct SiblingEntry {
    pub name: String,
    pub kind: OutlineKind,
    pub start_line: u32,
    pub end_line: u32,
    pub signature: Option<String>,
}

/// Collect siblings of the target: peer methods if the target is a method,
/// otherwise peer top-level definitions in the same file.
///
/// Skips imports/exports (noise) and the target itself. Sorted by:
/// functions/methods first, then alphabetical.
fn entry_matches_target(entry: &OutlineEntry, target: &ResolvedTarget) -> bool {
    entry.start_line == target.start_line && entry.name == target.name
}

fn find_parent<'a>(
    entries: &'a [OutlineEntry],
    target: &ResolvedTarget,
) -> Option<&'a OutlineEntry> {
    for entry in entries {
        if entry
            .children
            .iter()
            .any(|child| entry_matches_target(child, target))
        {
            return Some(entry);
        }
        if let Some(parent) = find_parent(&entry.children, target) {
            return Some(parent);
        }
    }
    None
}

pub(crate) fn collect_siblings(
    entries: &[OutlineEntry],
    target: &ResolvedTarget,
) -> Vec<SiblingEntry> {
    // A top-level target keeps the complete top-level sibling set. For nested
    // targets, walk the full outline tree to find the actual immediate parent.
    // A missing target has no siblings; never substitute unrelated top-level entries.
    let candidates: Vec<&OutlineEntry> = if entries.iter().any(|e| entry_matches_target(e, target))
    {
        entries.iter().collect()
    } else {
        find_parent(entries, target)
            .map(|parent| parent.children.iter().collect())
            .unwrap_or_default()
    };

    let mut out: Vec<SiblingEntry> = candidates
        .into_iter()
        .filter(|e| !matches!(e.kind, OutlineKind::Import | OutlineKind::Export))
        .filter(|e| !(e.start_line == target.start_line && e.name == target.name))
        .map(|e| SiblingEntry {
            name: e.name.clone(),
            kind: e.kind,
            start_line: e.start_line,
            end_line: e.end_line,
            signature: e.signature.clone(),
        })
        .collect();

    out.sort_by(|a, b| {
        let a_fn = matches!(a.kind, OutlineKind::Function);
        let b_fn = matches!(b.kind, OutlineKind::Function);
        b_fn.cmp(&a_fn).then_with(|| a.name.cmp(&b.name))
    });
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn make_entry(kind: OutlineKind, name: &str, start: u32, end: u32) -> OutlineEntry {
        OutlineEntry {
            kind,
            name: name.to_string(),
            start_line: start,
            span_start_line: start,
            end_line: end,
            signature: None,
            children: Vec::new(),
            doc: None,
        }
    }

    #[test]
    fn line_lookup_finds_top_level_entry() {
        let entries = vec![
            make_entry(OutlineKind::Function, "foo", 10, 20),
            make_entry(OutlineKind::Function, "bar", 30, 40),
        ];
        let hit = find_entry_at_line(&entries, 15).expect("expected entry at line 15");
        assert_eq!(hit.name, "foo");
    }

    #[test]
    fn line_lookup_prefers_deepest_match() {
        let mut class = make_entry(OutlineKind::Class, "MyClass", 1, 50);
        class
            .children
            .push(make_entry(OutlineKind::Function, "method", 10, 25));
        let entries = vec![class];
        let hit = find_entry_at_line(&entries, 12).expect("expected method match");
        assert_eq!(hit.name, "method", "should pick child over parent");
    }

    #[test]
    fn line_lookup_returns_parent_when_no_child_match() {
        let mut class = make_entry(OutlineKind::Class, "MyClass", 1, 50);
        class
            .children
            .push(make_entry(OutlineKind::Function, "method", 10, 25));
        let entries = vec![class];
        let hit = find_entry_at_line(&entries, 30).expect("expected class match");
        assert_eq!(hit.name, "MyClass");
    }

    #[test]
    fn line_lookup_miss_returns_none() {
        let entries = vec![make_entry(OutlineKind::Function, "foo", 10, 20)];
        assert!(find_entry_at_line(&entries, 100).is_none());
    }

    // -- find_by_start_line ----------------------------------------------

    #[test]
    fn start_line_lookup_matches_exact_start() {
        let mut class = make_entry(OutlineKind::Class, "Outer", 1, 50);
        class
            .children
            .push(make_entry(OutlineKind::Function, "inner", 10, 25));
        let entries = vec![class];
        let hit = find_entry_by_start_line(&entries, 10).expect("expected inner");
        assert_eq!(hit.name, "inner");
    }

    #[test]
    fn start_line_lookup_no_match_returns_none() {
        let entries = vec![make_entry(OutlineKind::Function, "foo", 10, 20)];
        assert!(find_entry_by_start_line(&entries, 11).is_none());
    }

    // -- resolve_by_path_line — integration via tempdir ------------------

    fn write_fixture(dir: &Path, rel: &str, body: &str) -> PathBuf {
        let path = dir.join(rel);
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).unwrap();
        }
        let mut f = fs::File::create(&path).unwrap();
        f.write_all(body.as_bytes()).unwrap();
        path
    }

    #[test]
    fn resolve_by_path_line_finds_enclosing_function() {
        let tmp = tempfile::tempdir().unwrap();
        let body = "fn alpha() {\n    let x = 1;\n}\n\nfn beta() {\n    let y = 2;\n}\n";
        let path = write_fixture(tmp.path(), "src/a.rs", body);

        let (target, content, lang) = resolve_by_path_line(&path, 2, &OutlineCache::new()).unwrap();
        assert_eq!(target.name, "alpha");
        assert_eq!(target.kind, OutlineKind::Function);
        assert_eq!(target.start_line, 1);
        assert!(
            content.contains("fn alpha"),
            "content should be the file body"
        );
        assert_eq!(lang, Lang::Rust);
    }

    #[test]
    fn resolve_by_path_line_prefers_deepest_same_line_definition() {
        let tmp = tempfile::tempdir().unwrap();
        let path = write_fixture(tmp.path(), "src/a.rs", "mod outer { fn inner() {} }\n");

        let (target, _, _) = resolve_by_path_line(&path, 1, &OutlineCache::new()).unwrap();
        assert_eq!(target.name, "inner");
        assert_eq!(target.kind, OutlineKind::Function);
    }

    #[test]
    fn resolve_by_path_line_reaches_deep_semantic_span() {
        let tmp = tempfile::tempdir().unwrap();
        let body = "mod a {\n    mod b {\n        #[inline]\n        fn method() {\n            work();\n        }\n    }\n}\n";
        let path = write_fixture(tmp.path(), "src/deep.rs", body);

        for line in [3, 4, 5] {
            let (target, _, lang) =
                resolve_by_path_line(&path, line, &OutlineCache::new()).unwrap();
            assert_eq!(target.name, "method");
            assert_eq!(target.start_line, 4);
            assert_eq!(target.span_start_line, 3);
            assert_eq!(lang, Lang::Rust);
        }
    }

    #[test]
    fn resolve_by_path_line_prefers_decorated_python_method_over_enclosing_class() {
        let tmp = tempfile::tempdir().unwrap();
        let body = "class Handler:\n    @logged\n    async \\\n    def blocked(self) -> bool:\n        return True\n";
        let path = write_fixture(tmp.path(), "producer.py", body);

        for line in [2, 3, 4, 5] {
            let (target, _, lang) =
                resolve_by_path_line(&path, line, &OutlineCache::new()).unwrap();
            assert_eq!(target.name, "blocked");
            assert_eq!(target.kind, OutlineKind::Function);
            assert_eq!(target.start_line, 4);
            assert_eq!(lang, Lang::Python);
        }
    }

    #[test]
    fn resolve_by_path_line_keeps_import_inside_enclosing_function() {
        let tmp = tempfile::tempdir().unwrap();
        let body = "def load():\n    import os\n    return os.getcwd()\n";
        let path = write_fixture(tmp.path(), "loader.py", body);

        let (target, _, _) = resolve_by_path_line(&path, 2, &OutlineCache::new()).unwrap();
        assert_eq!(target.name, "load");
        assert_eq!(target.kind, OutlineKind::Function);
    }

    #[test]
    fn resolve_by_path_line_returns_not_found_outside_any_entry() {
        let tmp = tempfile::tempdir().unwrap();
        let body = "fn alpha() {}\n";
        let path = write_fixture(tmp.path(), "src/a.rs", body);
        let err = resolve_by_path_line(&path, 99, &OutlineCache::new()).unwrap_err();
        assert!(matches!(err, TilthError::NotFound { .. }));
    }

    #[test]
    fn resolve_by_path_line_rejects_non_code_file() {
        let tmp = tempfile::tempdir().unwrap();
        let path = write_fixture(tmp.path(), "notes.txt", "hello\n");
        let err = resolve_by_path_line(&path, 1, &OutlineCache::new()).unwrap_err();
        assert!(matches!(err, TilthError::InvalidQuery { .. }));
    }

    #[test]
    fn resolve_by_path_line_missing_file_is_io_error() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("nope.rs");
        let err = resolve_by_path_line(&path, 1, &OutlineCache::new()).unwrap_err();
        assert!(matches!(err, TilthError::IoError { .. }));
    }

    #[test]
    fn resolve_by_path_line_keeps_grammarless_fallback() {
        let tmp = tempfile::tempdir().unwrap();
        for (file, source) in [
            ("Dockerfile", "FROM scratch\n"),
            ("Makefile", "all:\n\t@true\n"),
        ] {
            let path = write_fixture(tmp.path(), file, source);
            let err = resolve_by_path_line(&path, 1, &OutlineCache::new()).unwrap_err();
            let message = err.to_string();
            assert!(
                message.contains("no definition encloses line 1"),
                "{file} must reach the grammarless fallback: {message}"
            );
            assert!(
                !message.contains("source could not be parsed"),
                "{file}: {message}"
            );
        }
    }
    #[test]
    fn named_enrichment_distinguishes_same_name_same_line_declarations() {
        let tmp = tempfile::tempdir().unwrap();
        let body = "function run() { function run() {}\n  return 1;\n}\n";
        let path = write_fixture(tmp.path(), "nested.js", body);

        let (inner, _, _) = enrich_from_outline(
            path.clone(),
            1,
            Some(1),
            "run".into(),
            false,
            &OutlineCache::new(),
        )
        .unwrap();
        let (outer, _, _) =
            enrich_from_outline(path, 1, Some(3), "run".into(), false, &OutlineCache::new())
                .unwrap();

        assert_eq!((inner.span_start_line, inner.end_line), (1, 1));
        assert_eq!((outer.span_start_line, outer.end_line), (1, 3));
    }

    #[test]
    fn multiline_method_headers_resolve_by_prefix_line_and_name() {
        let tmp = tempfile::tempdir().unwrap();
        let cases = [
            (
                "Worker.java",
                "class Worker {\n    public\n    Task<String>\n    run() {}\n}\n",
                "run",
            ),
            (
                "Worker.cs",
                "class Worker {\n    public\n    Task<string>\n    Run() {}\n}\n",
                "Run",
            ),
        ];
        for (file, source, name) in cases {
            let path = write_fixture(tmp.path(), file, source);
            let (by_line, _, _) = resolve_by_path_line(&path, 2, &OutlineCache::new()).unwrap();
            assert_eq!(by_line.name, name);
            assert_eq!(by_line.start_line, 4);
            assert_eq!(by_line.span_start_line, 2);
        }
    }

    #[test]
    fn multiline_export_wrapper_resolves_one_semantic_span() {
        let tmp = tempfile::tempdir().unwrap();
        let path = write_fixture(
            tmp.path(),
            "run.ts",
            "export default\nfunction run() { return 1; }\n",
        );
        for line in [1, 2] {
            let (target, _, _) = resolve_by_path_line(&path, line, &OutlineCache::new()).unwrap();
            assert_eq!(target.name, "run");
            assert_eq!(target.start_line, 2);
            assert_eq!(target.span_start_line, 1);
        }
    }

    // -- combined outline and deep semantic-entry parse --------------------

    #[test]
    fn combined_outline_parse_reaches_doubly_nested_method() {
        let code = "pub mod a {\n    pub mod b {\n        pub fn method() {}\n    }\n}\n";
        let (_, entry) = crate::lang::outline::get_outline_entries_and_entry_by_name_at_start_line(
            code,
            Lang::Rust,
            "method",
            3,
            None,
        );
        let entry = entry.expect("AST fallback must find the doubly-nested method at line 3");
        assert_eq!(entry.name, "method");
        assert_eq!(entry.start_line, 3);
        assert!(entry.signature.is_some(), "fn entry must carry a signature");
    }

    #[test]
    fn combined_outline_parse_keeps_nested_rust_attribute_span() {
        let code = "pub mod a {\n    pub mod b {\n        #[inline]\n        pub fn method() {}\n    }\n}\n";
        let (entries, exact) =
            crate::lang::outline::get_outline_entries_and_entry_by_name_at_start_line(
                code,
                Lang::Rust,
                "method",
                4,
                None,
            );
        let entry = exact.expect("nested Rust method should resolve exactly");
        assert_eq!(entry.name, "method");
        assert_eq!(entry.start_line, 4);
        assert_eq!(entry.span_start_line, 3);
        assert!(
            crate::lang::outline::find_entry_by_name(&entries, "method").is_none(),
            "display outline must retain its nesting cap"
        );
    }

    #[test]
    fn combined_outline_parse_keeps_nested_elixir_metadata_span() {
        let code = "defmodule A do\n  @doc \"run\"\n  def run, do: :ok\nend\n";
        let (_, exact) = crate::lang::outline::get_outline_entries_and_entry_by_name_at_start_line(
            code,
            Lang::Elixir,
            "run",
            3,
            None,
        );
        let entry = exact.expect("nested Elixir function should resolve exactly");
        assert_eq!(entry.name, "run");
        assert_eq!(entry.start_line, 3);
        assert_eq!(entry.span_start_line, 2);
    }

    #[test]
    fn combined_outline_parse_has_no_exact_entry_off_declaration_line() {
        let code = "pub mod a {\n    pub fn method() {}\n}\n";
        let (_, entry) = crate::lang::outline::get_outline_entries_and_entry_by_name_at_start_line(
            code,
            Lang::Rust,
            "method",
            4,
            None,
        );
        assert!(entry.is_none());
    }

    fn target_in_file(name: &str, start: u32, end: u32, path: &str) -> ResolvedTarget {
        ResolvedTarget {
            name: name.to_string(),
            path: PathBuf::from(path),
            start_line: start,
            span_start_line: start,
            end_line: end,
            kind: OutlineKind::Function,
            signature: None,
        }
    }

    #[test]
    fn siblings_top_level_skips_target_and_imports() {
        let entries = vec![
            make_entry(OutlineKind::Import, "std::fs", 1, 1),
            make_entry(OutlineKind::Function, "target", 5, 10),
            make_entry(OutlineKind::Function, "alpha", 12, 15),
            make_entry(OutlineKind::Function, "beta", 17, 20),
        ];
        let target = target_in_file("target", 5, 10, "src/a.rs");
        let sibs = collect_siblings(&entries, &target);
        let names: Vec<&str> = sibs.iter().map(|s| s.name.as_str()).collect();
        assert_eq!(names, vec!["alpha", "beta"]);
    }

    #[test]
    fn siblings_methods_uses_parent_children() {
        let mut class = make_entry(OutlineKind::Class, "MyStruct", 1, 50);
        class
            .children
            .push(make_entry(OutlineKind::Function, "target", 5, 10));
        class
            .children
            .push(make_entry(OutlineKind::Function, "peer_a", 12, 15));
        class
            .children
            .push(make_entry(OutlineKind::Function, "peer_b", 17, 20));
        let entries = vec![
            class,
            make_entry(OutlineKind::Function, "unrelated_top_level", 60, 65),
        ];
        let target = target_in_file("target", 5, 10, "src/a.rs");
        let sibs = collect_siblings(&entries, &target);
        let names: Vec<&str> = sibs.iter().map(|s| s.name.as_str()).collect();
        assert_eq!(
            names,
            vec!["peer_a", "peer_b"],
            "should pick parent children, not top-level"
        );
    }

    #[test]
    fn siblings_doubly_nested_target_uses_immediate_parent() {
        let mut outer = make_entry(OutlineKind::Module, "outer", 1, 50);
        let mut inner = make_entry(OutlineKind::Class, "inner", 2, 20);
        inner
            .children
            .push(make_entry(OutlineKind::Function, "target", 3, 6));
        inner
            .children
            .push(make_entry(OutlineKind::Function, "peer", 8, 11));
        outer.children.push(inner);
        let entries = vec![
            outer,
            make_entry(OutlineKind::Function, "unrelated_top_level", 60, 65),
        ];
        let target = target_in_file("target", 3, 6, "src/a.rs");

        let sibs = collect_siblings(&entries, &target);
        let names: Vec<&str> = sibs.iter().map(|s| s.name.as_str()).collect();
        assert_eq!(names, vec!["peer"]);
    }

    #[test]
    fn siblings_missing_target_do_not_fall_back_to_top_level() {
        let entries = vec![make_entry(
            OutlineKind::Function,
            "unrelated_top_level",
            60,
            65,
        )];
        let target = target_in_file("missing", 3, 6, "src/a.rs");

        assert!(collect_siblings(&entries, &target).is_empty());
    }

    #[test]
    fn siblings_functions_before_fields() {
        let entries = vec![
            make_entry(OutlineKind::Function, "target", 5, 10),
            make_entry(OutlineKind::Variable, "field_a", 12, 12),
            make_entry(OutlineKind::Function, "peer", 14, 18),
        ];
        let target = target_in_file("target", 5, 10, "src/a.rs");
        let sibs = collect_siblings(&entries, &target);
        let kinds: Vec<&str> = sibs
            .iter()
            .map(|s| match s.kind {
                OutlineKind::Function => "fn",
                OutlineKind::Variable => "var",
                _ => "other",
            })
            .collect();
        assert_eq!(kinds, vec!["fn", "var"], "functions sort first");
    }
}
