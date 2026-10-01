//! Read-only structural matching over retained source/tree snapshots.
use std::borrow::Cow;
use std::collections::{BTreeMap, HashMap};
use std::path::Path;
use std::sync::Mutex;

use ast_grep_core::matcher::PatternBuilder;
use ast_grep_core::meta_var::MetaVariable;
use ast_grep_core::tree_sitter::StrDoc;
use ast_grep_core::{Doc, Language, Node, Pattern, PatternError};
use ast_grep_language::SupportLang;
use serde::Serialize;

use crate::cache::OutlineCache;
use crate::error::TilthError;
use crate::lang::treesitter::DocumentLanguage;
use crate::types::{FileType, Lang};

/// Byte offsets are zero-based and half-open. Lines identify both endpoints.
#[derive(Serialize)]
struct Location {
    start_byte: usize,
    end_byte: usize,
    start_line: usize,
    end_line: usize,
}

impl Location {
    fn of(node: &Node<'_, StrDoc<DocumentLanguage>>) -> Self {
        Self {
            start_byte: node.range().start,
            end_byte: node.range().end,
            start_line: node.start_pos().line() + 1,
            end_line: node.end_pos().line() + 1,
        }
    }
}

#[derive(Serialize)]
pub(crate) struct StructuralItem {
    path: String,
    range: Location,
    captures: BTreeMap<String, Vec<Location>>,
}

#[derive(Default)]
pub(crate) struct StructuralScan {
    pub(crate) items: Vec<StructuralItem>,
    pub(crate) skipped_files: usize,
    pub(crate) limited: bool,
    /// Matches per file, counted before the retention cap.
    pub(crate) file_counts: BTreeMap<String, usize>,
}

/// Bound match retention independently of the response token budget.
const MAX_MATCHES: usize = 1000;

struct CompiledPattern {
    file_language: Lang,
    pattern: Pattern,
}

/// One compilation per distinct language/pattern pair in a request.
#[derive(Default)]
pub(crate) struct StructuralPatterns(HashMap<(String, String), CompiledPattern>);

#[cfg(test)]
thread_local! {
    static COMPILATIONS: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
}

#[cfg(test)]
pub(crate) fn compilation_count() -> usize {
    COMPILATIONS.get()
}

impl StructuralPatterns {
    pub(crate) fn prepare(&mut self, language: &str, source: &str) -> Result<(), String> {
        let key = (language.to_string(), source.to_string());
        if self.0.contains_key(&key) {
            return Ok(());
        }
        let (language, file_language) = match language {
            "rust" => (SupportLang::Rust, Lang::Rust),
            "typescript" => (SupportLang::TypeScript, Lang::TypeScript),
            "python" => (SupportLang::Python, Lang::Python),
            _ => {
                return Err(
                    "unsupported structural language; use rust, typescript, or python".into(),
                )
            }
        };
        #[cfg(test)]
        COMPILATIONS.set(COMPILATIONS.get() + 1);
        let pattern = Pattern::try_new(source, PatternLanguage(language))
            .map_err(|error| format!("invalid structural pattern: {error}"))?;
        self.0.insert(
            key,
            CompiledPattern {
                file_language,
                pattern,
            },
        );
        Ok(())
    }

    pub(crate) fn search(
        &self,
        language: &str,
        source: &str,
        scope: &Path,
        glob: Option<&str>,
        cache: &OutlineCache,
    ) -> Result<StructuralScan, TilthError> {
        // The request validates and prepares every entry before execution.
        let compiled = &self.0[&(language.to_string(), source.to_string())];
        let scan = Mutex::new(StructuralScan::default());
        let kept = Mutex::new(std::collections::BinaryHeap::<Keyed>::new());
        let literal = compiled.pattern.fixed_string();
        super::walker(scope, glob)?.run(|| {
            Box::new(|entry| {
                let Ok(entry) = entry else {
                    scan.lock().unwrap().skipped_files += 1;
                    return ignore::WalkState::Continue;
                };
                if !entry.file_type().is_some_and(|kind| kind.is_file())
                    || crate::lang::detect_file_type(entry.path())
                        != FileType::Code(compiled.file_language)
                    || super::path_is_secret_file(entry.path())
                {
                    return ignore::WalkState::Continue;
                }
                let path = entry.path();
                if path
                    .file_name()
                    .and_then(|name| name.to_str())
                    .is_some_and(crate::lang::detection::is_minified_by_name)
                {
                    return ignore::WalkState::Continue;
                }
                let snapshot = if literal.is_empty() {
                    let Some(snapshot) = cache.get_or_parse(path) else {
                        scan.lock().unwrap().skipped_files += 1;
                        return ignore::WalkState::Continue;
                    };
                    if snapshot.content().len() as u64
                        >= crate::lang::detection::MINIFIED_CHECK_THRESHOLD
                        && crate::lang::detection::is_minified_by_content(
                            snapshot.content().as_bytes(),
                        )
                    {
                        return ignore::WalkState::Continue;
                    }
                    Some(snapshot)
                } else {
                    // Probe the literal on raw bytes so non-candidates never parse or enter the cache.
                    let Ok(meta) = std::fs::metadata(path) else {
                        scan.lock().unwrap().skipped_files += 1;
                        return ignore::WalkState::Continue;
                    };
                    let content = if meta.len() > super::MAX_SEARCH_FILE_SIZE {
                        None
                    } else {
                        std::fs::read_to_string(path).ok()
                    };
                    let Some(content) = content else {
                        scan.lock().unwrap().skipped_files += 1;
                        return ignore::WalkState::Continue;
                    };
                    if memchr::memmem::find(content.as_bytes(), literal.as_bytes()).is_none() {
                        return ignore::WalkState::Continue;
                    }
                    if content.len() as u64 >= crate::lang::detection::MINIFIED_CHECK_THRESHOLD
                        && crate::lang::detection::is_minified_by_content(content.as_bytes())
                    {
                        return ignore::WalkState::Continue;
                    }
                    cache.parse_with_revision(
                        path,
                        &content,
                        // `meta` predates the read, so a stale publish fails its recheck.
                        crate::util::FileRevision::from_metadata_and_bytes(
                            &meta,
                            content.as_bytes(),
                        ),
                    )
                };
                let Some(snapshot) = snapshot else {
                    scan.lock().unwrap().skipped_files += 1;
                    return ignore::WalkState::Continue;
                };
                let root = snapshot.ast();
                let rel = crate::format::rel(path, scope);
                let mut found = Vec::new();
                for matched in root.root().find_all(&compiled.pattern) {
                    let environment = matched.get_env();
                    let mut captures = BTreeMap::new();
                    for variable in environment.get_matched_variables() {
                        match variable {
                            MetaVariable::Capture(name, _) => {
                                let locations = environment
                                    .get_match(&name)
                                    .map(Location::of)
                                    .into_iter()
                                    .collect();
                                captures.insert(name, locations);
                            }
                            MetaVariable::MultiCapture(name) => {
                                let locations = environment
                                    .get_multiple_matches(&name)
                                    .iter()
                                    .map(Location::of)
                                    .collect();
                                captures.insert(name, locations);
                            }
                            MetaVariable::Dropped(_) | MetaVariable::Multiple => {}
                        }
                    }
                    found.push(Keyed(StructuralItem {
                        path: rel.clone(),
                        range: Location::of(&matched),
                        captures,
                    }));
                }
                if !found.is_empty() {
                    scan.lock().unwrap().file_counts.insert(rel, found.len());
                }
                // Keep the smallest MAX_MATCHES keys so the cap ignores thread timing.
                let mut kept = kept.lock().unwrap();
                let mut overflowed = false;
                for item in found {
                    kept.push(item);
                    if kept.len() > MAX_MATCHES {
                        kept.pop();
                        overflowed = true;
                    }
                }
                drop(kept);
                if overflowed {
                    scan.lock().unwrap().limited = true;
                }
                ignore::WalkState::Continue
            })
        });
        let mut scan = scan.into_inner().unwrap();
        scan.items = kept
            .into_inner()
            .unwrap()
            .into_sorted_vec()
            .into_iter()
            .map(|Keyed(item)| item)
            .collect();
        Ok(scan)
    }
}

/// Orders items by path then byte range for the bounded max-heap.
struct Keyed(StructuralItem);

impl Keyed {
    fn order(&self) -> (&str, usize, usize) {
        (&self.0.path, self.0.range.start_byte, self.0.range.end_byte)
    }
}

impl PartialEq for Keyed {
    fn eq(&self, other: &Self) -> bool {
        self.order() == other.order()
    }
}
impl Eq for Keyed {}
impl PartialOrd for Keyed {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for Keyed {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        self.order().cmp(&other.order())
    }
}

/// Validate the selected subtree before ast-grep removes missing nodes.
#[derive(Clone)]
struct PatternLanguage(SupportLang);

impl Language for PatternLanguage {
    fn pre_process_pattern<'q>(&self, query: &'q str) -> Cow<'q, str> {
        self.0.pre_process_pattern(query)
    }

    fn meta_var_char(&self) -> char {
        self.0.meta_var_char()
    }

    fn expando_char(&self) -> char {
        self.0.expando_char()
    }

    fn extract_meta_var(&self, source: &str) -> Option<MetaVariable> {
        self.0.extract_meta_var(source)
    }

    fn kind_to_id(&self, kind: &str) -> u16 {
        self.0.kind_to_id(kind)
    }

    fn field_to_id(&self, field: &str) -> Option<u16> {
        self.0.field_to_id(field)
    }

    fn build_pattern(&self, builder: &PatternBuilder) -> Result<Pattern, PatternError> {
        builder.build(|source| {
            // Only patterns parse here. Candidate documents retain cached trees.
            let document = StrDoc::try_new(source, self.0)?;
            let mut selected = document.root_node();
            // Mirror single_matcher in pinned ast-grep-core 0.45.3.
            // Rust expression patterns can have a missing outer semicolon.
            while selected.child_count() == 1
                || (selected.child_count() == 2
                    && selected
                        .child(1)
                        .is_some_and(|child| child.is_missing() || child.kind().is_empty()))
            {
                selected = selected.child(0).unwrap();
            }
            if selected.has_error() || selected.is_missing() {
                return Err("syntax error or missing syntax in structural pattern".into());
            }
            Ok(document)
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn owned_document_borrows_cached_bytes_and_tree() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("source.py");
        std::fs::write(&path, "wrap(value)\n").unwrap();
        let cache = OutlineCache::new();
        let snapshot = cache.get_or_parse(&path).unwrap();
        let document = snapshot.ast().root().get_doc();
        assert_eq!(
            document.tree.root_node().id(),
            snapshot.tree().root_node().id()
        );
        assert_eq!(document.src.as_ptr(), snapshot.content().as_ptr());
        let pattern = Pattern::try_new("wrap($A)", PatternLanguage(SupportLang::Python)).unwrap();
        let matched = snapshot.ast().root().find(&pattern).unwrap();
        assert!(matches!(matched.text(), Cow::Borrowed("wrap(value)")));
        assert_eq!(matched.text().as_ptr(), snapshot.content().as_ptr());
    }

    fn scan(dir: &Path, source: &str) -> StructuralScan {
        let mut patterns = StructuralPatterns::default();
        patterns.prepare("python", source).unwrap();
        let cache = OutlineCache::new();
        patterns
            .search("python", source, dir, None, &cache)
            .unwrap()
    }

    #[test]
    fn limited_scan_keeps_the_sorted_prefix_regardless_of_thread_timing() {
        let directory = tempfile::tempdir().unwrap();
        let line = "wrap(value)\n";
        for name in ["a.py", "b.py", "c.py"] {
            std::fs::write(directory.path().join(name), line.repeat(400)).unwrap();
        }
        for _ in 0..20 {
            let result = scan(directory.path(), "wrap($A)");
            assert!(result.limited);
            assert_eq!(result.skipped_files, 0);
            let keys: Vec<_> = result
                .items
                .iter()
                .map(|item| (item.path.as_str(), item.range.start_byte))
                .collect();
            let expected: Vec<_> = [("a.py", 400), ("b.py", 400), ("c.py", 200)]
                .into_iter()
                .flat_map(|(name, count)| (0..count).map(move |index| (name, index * line.len())))
                .collect();
            assert_eq!(keys, expected);
        }
    }

    #[test]
    fn literal_prefilter_never_parses_files_without_the_literal() {
        let directory = tempfile::tempdir().unwrap();
        let hit = "prefilter_literal_unique(value)\n";
        let miss = "prefilter_other_unique(value)\n";
        let hit_witness = crate::lang::treesitter::ParseWitness::new(hit);
        let miss_witness = crate::lang::treesitter::ParseWitness::new(miss);
        std::fs::write(directory.path().join("hit.py"), hit).unwrap();
        std::fs::write(directory.path().join("miss.py"), miss).unwrap();
        let result = scan(directory.path(), "prefilter_literal_unique($A)");
        assert_eq!(result.items.len(), 1);
        assert_eq!(result.items[0].path, "hit.py");
        assert_eq!(result.skipped_files, 0);
        assert!(!result.limited);
        assert_eq!(hit_witness.count(), 1);
        assert_eq!(miss_witness.count(), 0);
    }

    #[test]
    fn minified_files_are_skipped_silently() {
        let directory = tempfile::tempdir().unwrap();
        let by_name = directory.path().join("bundle.min.ts");
        assert!(crate::lang::detection::is_minified_by_name("bundle.min.ts"));
        std::fs::write(by_name, "wrap(value);\n").unwrap();
        let padding = " ".repeat(crate::lang::detection::MINIFIED_CHECK_THRESHOLD as usize);
        std::fs::write(
            directory.path().join("dense.ts"),
            format!("wrap(value);{padding}"),
        )
        .unwrap();
        let mut patterns = StructuralPatterns::default();
        patterns.prepare("typescript", "wrap($A)").unwrap();
        let result = patterns
            .search(
                "typescript",
                "wrap($A)",
                directory.path(),
                None,
                &OutlineCache::new(),
            )
            .unwrap();
        assert!(result.items.is_empty());
        assert_eq!(result.skipped_files, 0);
        assert!(!result.limited);
    }
}
