//! Read-only structural matching over retained source/tree snapshots.
use std::borrow::Cow;
use std::collections::{BTreeMap, HashMap};
use std::path::Path;
use std::sync::{Arc, Mutex};

use ast_grep_core::matcher::PatternBuilder;
use ast_grep_core::meta_var::MetaVariable;
use ast_grep_core::tree_sitter::StrDoc;
use ast_grep_core::{Language, Pattern, PatternError};
use ast_grep_language::SupportLang;
use serde::Serialize;

use crate::cache::{OutlineCache, ParsedFile};
use crate::error::TilthError;
use crate::lang::treesitter::DocumentNode;
use crate::types::{FileType, Lang};
use crate::util::FileRevision;

/// Byte offsets are zero-based and half-open. Lines are one-based and identify
/// both endpoints. Wire form: `[start_line, end_line, start_byte, end_byte]`.
#[derive(Clone, Copy)]
struct Location {
    start_byte: usize,
    end_byte: usize,
    start_line: usize,
    end_line: usize,
}

impl Location {
    fn of(node: &DocumentNode<'_>) -> Self {
        Self {
            start_byte: node.range().start,
            end_byte: node.range().end,
            start_line: node.start_pos().line() + 1,
            end_line: node.end_pos().line() + 1,
        }
    }

    fn span(self) -> [usize; 4] {
        [
            self.start_line,
            self.end_line,
            self.start_byte,
            self.end_byte,
        ]
    }
}

impl Serialize for Location {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        self.span().serialize(serializer)
    }
}

/// One match. Wire form: its span, plus a trailing captures object when any exist.
pub(crate) struct Match {
    range: Location,
    captures: BTreeMap<String, Vec<Location>>,
}

impl Serialize for Match {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        use serde::ser::SerializeSeq;
        let mut seq = serializer.serialize_seq(None)?;
        for value in self.range.span() {
            seq.serialize_element(&value)?;
        }
        if !self.captures.is_empty() {
            seq.serialize_element(&self.captures)?;
        }
        seq.end()
    }
}

/// Sorted matches of one file. Wire form: `{path, tag?, matches}`; `tag` is the
/// whole-file edit tag when the caller minted one.
#[derive(Serialize)]
pub(crate) struct FileMatches {
    path: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    tag: Option<String>,
    matches: Vec<Match>,
}

impl FileMatches {
    pub(crate) fn path(&self) -> &str {
        &self.path
    }

    pub(crate) fn set_tag(&mut self, tag: String) {
        self.tag = Some(tag);
    }
}

#[derive(Default)]
pub(crate) struct StructuralScan {
    /// Retained matches in path/byte order, grouped by file.
    pub(crate) groups: Vec<FileMatches>,
    /// Number of retained matches across `groups`.
    pub(crate) retained: usize,
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
        let Some((file_language, language)) = crate::lang::spec::structural_language(language)
        else {
            return Err(format!(
                "unsupported structural language; use {}",
                crate::lang::spec::structural_language_list()
            ));
        };
        #[cfg(test)]
        COMPILATIONS.set(COMPILATIONS.get() + 1);
        let pattern = PatternLanguage::new(file_language, language)
            .compile(source)
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
                let count_skip = || scan.lock().unwrap().skipped_files += 1;
                let Ok(entry) = entry else {
                    count_skip();
                    return ignore::WalkState::Continue;
                };
                if crate::lang::detect_file_type(entry.path())
                    != FileType::Code(compiled.file_language)
                    || super::path_is_secret_file(entry.path())
                {
                    return ignore::WalkState::Continue;
                }
                let meta = match super::classify_walk_entry(&entry) {
                    Ok(Some(meta)) => meta,
                    Ok(None) | Err(super::WalkSkip::TooLarge) => {
                        count_skip();
                        return ignore::WalkState::Continue;
                    }
                    Err(_) => return ignore::WalkState::Continue,
                };
                let path = entry.path();
                let snapshot = match load_candidate(path, &meta, &literal, cache) {
                    Loaded::Snapshot(snapshot) => snapshot,
                    Loaded::Skip => return ignore::WalkState::Continue,
                    Loaded::Unparsed => {
                        count_skip();
                        return ignore::WalkState::Continue;
                    }
                };
                let rel = crate::format::rel(path, scope);
                // The heap bound only shrinks, so a stale snapshot never drops a needed match.
                let bound = {
                    let kept = kept.lock().unwrap();
                    if kept.len() >= MAX_MATCHES {
                        kept.peek().map(Keyed::bound)
                    } else {
                        None
                    }
                };
                let found =
                    collect_matches(snapshot.ast(), &compiled.pattern, &rel, bound.as_ref());
                if found.total > 0 {
                    scan.lock().unwrap().file_counts.insert(rel, found.total);
                }
                // Keep the smallest MAX_MATCHES keys so the cap ignores thread timing.
                let mut kept = kept.lock().unwrap();
                let mut overflowed = found.overflowed;
                for item in found.items {
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
        for Keyed { path, hit } in kept.into_inner().unwrap().into_sorted_vec() {
            scan.retained += 1;
            match scan.groups.last_mut() {
                Some(group) if group.path == path => group.matches.push(hit),
                _ => scan.groups.push(FileMatches {
                    path,
                    tag: None,
                    matches: vec![hit],
                }),
            }
        }
        Ok(scan)
    }
}

enum Loaded {
    Snapshot(Arc<ParsedFile>),
    /// A candidate that cannot match: no literal, or minified content.
    Skip,
    /// A file that could not be read or parsed; the scan is partial.
    Unparsed,
}

/// Load one walk candidate. A cached revision is scanned in place. On a miss the
/// literal and minified checks run on the raw bytes before any parse, so
/// non-candidates never parse or enter the cache.
fn load_candidate(
    path: &Path,
    meta: &std::fs::Metadata,
    literal: &str,
    cache: &OutlineCache,
) -> Loaded {
    let skippable = |bytes: &[u8]| {
        (!literal.is_empty() && memchr::memmem::find(bytes, literal.as_bytes()).is_none())
            || (bytes.len() as u64 >= crate::lang::detection::MINIFIED_CHECK_THRESHOLD
                && crate::lang::detection::is_minified_by_content(bytes))
    };
    if let Some(snapshot) = FileRevision::from_metadata(path, meta)
        .and_then(|revision| cache.cached_parse(path, &revision))
    {
        return if skippable(snapshot.content().as_bytes()) {
            Loaded::Skip
        } else {
            Loaded::Snapshot(snapshot)
        };
    }
    let Ok(content) = std::fs::read_to_string(path) else {
        return Loaded::Unparsed;
    };
    if skippable(content.as_bytes()) {
        return Loaded::Skip;
    }
    // `meta` predates the read, so a stale publish fails its recheck.
    let revision = FileRevision::from_metadata_and_bytes(meta, content.as_bytes());
    cache
        .parse_with_revision(path, content, revision)
        .map_or(Loaded::Unparsed, Loaded::Snapshot)
}

struct Collected {
    /// Every match in the file, retained or not.
    total: usize,
    /// A match was dropped because the heap was already full below it.
    overflowed: bool,
    items: Vec<Keyed>,
}

/// Match `pattern` in one document. Once `bound` (the full heap's largest key)
/// is known, keys at or above it are counted without building an item.
fn collect_matches(
    root: &crate::lang::treesitter::ParsedDocument,
    pattern: &Pattern,
    rel: &str,
    bound: Option<&(String, usize, usize)>,
) -> Collected {
    let mut found = Collected {
        total: 0,
        overflowed: false,
        items: Vec::new(),
    };
    #[cfg(test)]
    {
        let src = &root.root().get_doc().src;
        crate::lang::treesitter::record_scanned_document(src, src.as_ptr() as usize);
    }
    for matched in root.root().find_all(pattern) {
        found.total += 1;
        let range = Location::of(&matched);
        if bound.is_some_and(|(path, start, end)| {
            (rel, range.start_byte, range.end_byte) >= (path.as_str(), *start, *end)
        }) {
            found.overflowed = true;
            continue;
        }
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
        found.items.push(Keyed {
            path: rel.to_string(),
            hit: Match { range, captures },
        });
    }
    found
}

/// Orders items by path then byte range for the bounded max-heap.
struct Keyed {
    path: String,
    hit: Match,
}

impl Keyed {
    fn order(&self) -> (&str, usize, usize) {
        (
            &self.path,
            self.hit.range.start_byte,
            self.hit.range.end_byte,
        )
    }

    fn bound(&self) -> (String, usize, usize) {
        (
            self.path.clone(),
            self.hit.range.start_byte,
            self.hit.range.end_byte,
        )
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

/// Parses patterns with the grammar the candidate documents use, so node kind
/// ids agree. ast-grep's own language may use a different grammar (PHP).
#[derive(Clone)]
struct PatternLanguage {
    support: SupportLang,
    grammar: tree_sitter::Language,
    preamble: &'static str,
}

impl PatternLanguage {
    fn new(lang: Lang, support: SupportLang) -> Self {
        let spec = crate::lang::spec::spec(lang);
        let grammar = spec.grammar.expect("structural languages have a grammar");
        Self {
            support,
            grammar: tree_sitter::Language::new(grammar),
            preamble: spec.policy.pattern_preamble,
        }
    }

    fn compile(&self, source: &str) -> Result<Pattern, String> {
        if self.preamble.is_empty() {
            let plain = Pattern::try_new(source, self.clone()).map_err(|error| error.to_string());
            if self.support == SupportLang::Go && self.go_needs_function_context(source) {
                if let Ok(contextual) = self.compile_in_go_function(source) {
                    return Ok(contextual);
                }
            }
            return plain;
        }
        // ast-grep selects a pattern node from the document root, which holds the
        // preamble. Select the node by kind instead.
        let processed = self.pre_process_pattern(source);
        let document = StrDoc::try_new(&processed, self.clone())?;
        let kind = self.select(&document.tree)?.kind().to_string();
        Pattern::contextual(source, &kind, self.clone()).map_err(|error| error.to_string())
    }

    /// Go parses a top-level `a.b(c)` as a type conversion, and a bare call
    /// fails to parse at all. Such patterns only parse as calls inside a body.
    fn go_needs_function_context(&self, source: &str) -> bool {
        let processed = self.pre_process_pattern(source);
        StrDoc::try_new(&processed, self.clone())
            .ok()
            .and_then(|document| {
                self.select(&document.tree)
                    .ok()
                    .map(|node| node.kind() == "type_conversion_expression")
            })
            .unwrap_or(true)
    }

    fn compile_in_go_function(&self, source: &str) -> Result<Pattern, String> {
        let context = format!("package p\nfunc _() {{\n{source}\n}}");
        let processed = self.pre_process_pattern(&context);
        let document = StrDoc::try_new(&processed, self.clone())?;
        let root = document.tree.root_node();
        let mut cursor = root.walk();
        let function = root
            .children(&mut cursor)
            .find(|child| child.kind() == "function_declaration")
            .ok_or("structural pattern must be a single node")?;
        let mut node = function
            .child_by_field_name("body")
            .ok_or("structural pattern must be a single node")?;
        if node.has_error() {
            return Err("syntax error or missing syntax in structural pattern".into());
        }
        if node.named_child_count() != 1 {
            return Err("structural pattern must be a single node".into());
        }
        node = node.named_child(0).unwrap();
        while node.child_count() == 1 {
            node = node.child(0).unwrap();
        }
        let kind = node.kind().to_string();
        Pattern::contextual(&context, &kind, self.clone()).map_err(|error| error.to_string())
    }

    /// Select the pattern node, mirroring `single_matcher` in pinned ast-grep-core
    /// 0.45.3. Rust expression patterns can have a missing outer semicolon.
    fn select<'t>(&self, tree: &'t tree_sitter::Tree) -> Result<tree_sitter::Node<'t>, String> {
        let mut selected = tree.root_node();
        if !self.preamble.is_empty() {
            let mut cursor = selected.walk();
            let body: Vec<_> = selected
                .children(&mut cursor)
                .filter(|child| child.start_byte() >= self.preamble.len())
                .collect();
            let single = body.len() == 1
                || (body.len() == 2 && (body[1].is_missing() || body[1].kind().is_empty()));
            if !single {
                return Err("structural pattern must be a single node".into());
            }
            selected = body[0];
        }
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
        Ok(selected)
    }
}

impl ast_grep_core::tree_sitter::LanguageExt for PatternLanguage {
    fn get_ts_language(&self) -> tree_sitter::Language {
        self.grammar.clone()
    }
}

impl Language for PatternLanguage {
    fn pre_process_pattern<'q>(&self, query: &'q str) -> Cow<'q, str> {
        let processed = self.support.pre_process_pattern(query);
        if self.preamble.is_empty() {
            return processed;
        }
        Cow::Owned(format!("{}{processed}", self.preamble))
    }

    fn meta_var_char(&self) -> char {
        self.support.meta_var_char()
    }

    fn expando_char(&self) -> char {
        self.support.expando_char()
    }

    fn extract_meta_var(&self, source: &str) -> Option<MetaVariable> {
        self.support.extract_meta_var(source)
    }

    fn kind_to_id(&self, kind: &str) -> u16 {
        self.grammar.id_for_node_kind(kind, true)
    }

    fn field_to_id(&self, field: &str) -> Option<u16> {
        self.grammar
            .field_id_for_name(field)
            .map(std::num::NonZeroU16::get)
    }

    fn build_pattern(&self, builder: &PatternBuilder) -> Result<Pattern, PatternError> {
        builder.build(|source| {
            // Only patterns parse here. Candidate documents retain cached trees.
            let document = StrDoc::try_new(source, self.clone())?;
            self.select(&document.tree)?;
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
        let pattern = PatternLanguage::new(Lang::Python, SupportLang::Python)
            .compile("wrap($A)")
            .unwrap();
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
            assert_eq!(result.retained, MAX_MATCHES);
            assert_eq!(result.file_counts.values().sum::<usize>(), 1200);
            assert_eq!(result.file_counts.len(), 3);
            let keys: Vec<_> = result
                .groups
                .iter()
                .flat_map(|group| {
                    group
                        .matches
                        .iter()
                        .map(|hit| (group.path.as_str(), hit.range.start_byte))
                })
                .collect();
            let expected: Vec<_> = [("a.py", 400), ("b.py", 400), ("c.py", 200)]
                .into_iter()
                .flat_map(|(name, count)| (0..count).map(move |index| (name, index * line.len())))
                .collect();
            assert_eq!(keys, expected);
            assert_eq!(result.groups.len(), 3);
        }
    }

    #[test]
    fn groups_serialize_to_the_compact_wire_form() {
        let directory = tempfile::tempdir().unwrap();
        std::fs::write(directory.path().join("w.py"), "wrap(value)\nwrap(1)\n").unwrap();
        let result = scan(directory.path(), "wrap($A)");
        assert_eq!(
            serde_json::to_value(&result.groups).unwrap(),
            serde_json::json!([{"path": "w.py", "matches": [
                [1, 1, 0, 11, {"A": [[1, 1, 5, 10]]}],
                [2, 2, 12, 19, {"A": [[2, 2, 17, 18]]}]
            ]}])
        );
        let bare = scan(directory.path(), "wrap(value)");
        assert_eq!(
            serde_json::to_value(&bare.groups).unwrap(),
            serde_json::json!([{"path": "w.py", "matches": [[1, 1, 0, 11]]}])
        );
    }

    #[test]
    fn structural_languages_come_from_the_lang_spec() {
        const NAMES: [&str; 17] = [
            "rust",
            "typescript",
            "tsx",
            "javascript",
            "python",
            "go",
            "java",
            "scala",
            "c",
            "c++",
            "ruby",
            "php",
            "swift",
            "kotlin",
            "c#",
            "elixir",
            "bash",
        ];
        assert_eq!(crate::lang::spec::structural_language_names(), NAMES);
        let list = format!("{}, or bash", NAMES[..16].join(", "));
        assert_eq!(crate::lang::spec::structural_language_list(), list);
        // Grammar-less languages have no structural entry.
        for name in ["docker", "make", "markdown"] {
            assert!(
                crate::lang::spec::structural_language(name).is_none(),
                "{name}"
            );
        }
        let mut patterns = StructuralPatterns::default();
        let error = patterns.prepare("make", "wrap($A)").unwrap_err();
        assert_eq!(
            error,
            format!("unsupported structural language; use {list}")
        );
    }

    #[test]
    fn pattern_and_document_grammars_agree_for_every_structural_language() {
        use ast_grep_core::tree_sitter::LanguageExt;
        for name in crate::lang::spec::structural_language_names() {
            let (lang, support) = crate::lang::spec::structural_language(&name).unwrap();
            let spec = crate::lang::spec::spec(lang);
            let document = tree_sitter::Language::new(spec.grammar.unwrap());
            // The grammar the pattern parses with is the document grammar.
            assert_eq!(
                PatternLanguage::new(lang, support).grammar,
                document,
                "{name}"
            );
            // Only a preamble language lets ast-grep's own grammar differ.
            if spec.policy.pattern_preamble.is_empty() {
                assert_eq!(support.get_ts_language(), document, "{name}");
            }
        }
    }

    #[test]
    fn php_patterns_match_code_after_inline_html() {
        let directory = tempfile::tempdir().unwrap();
        let source =
            "<div class=\"x\">{ it's }</div>\n<?php\n$out = wrap($this->value);\n?>\n<p>done</p>\n";
        std::fs::write(directory.path().join("page.phtml"), source).unwrap();
        let mut patterns = StructuralPatterns::default();
        patterns.prepare("php", "wrap($this->$P)").unwrap();
        let result = patterns
            .search(
                "php",
                "wrap($this->$P)",
                directory.path(),
                None,
                &OutlineCache::new(),
            )
            .unwrap();
        assert_eq!(result.retained, 1);
        assert_eq!(result.groups[0].path, "page.phtml");
        let start = source.find("wrap(").unwrap();
        let end = source.find(");").unwrap() + 1;
        assert_eq!(
            serde_json::to_value(&result.groups).unwrap(),
            serde_json::json!([{"path": "page.phtml", "matches": [
                [3, 3, start, end, {"P": [[3, 3, end - 6, end - 1]]}]
            ]}])
        );
        assert!(patterns.prepare("php", "a(); b();").is_err());
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
        assert_eq!(result.retained, 1);
        assert_eq!(result.groups[0].path, "hit.py");
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
        let dense = format!("wrap(value);{padding}");
        std::fs::write(directory.path().join("dense.ts"), &dense).unwrap();
        // The second pattern has no fixed literal, so it takes the no-literal branch.
        for pattern in ["wrap($A)", "$F($A)"] {
            let witness = crate::lang::treesitter::ParseWitness::new(&dense);
            let mut patterns = StructuralPatterns::default();
            patterns.prepare("typescript", pattern).unwrap();
            let result = patterns
                .search(
                    "typescript",
                    pattern,
                    directory.path(),
                    None,
                    &OutlineCache::new(),
                )
                .unwrap();
            assert_eq!(result.retained, 0, "{pattern}");
            assert_eq!(result.skipped_files, 0, "{pattern}");
            assert!(!result.limited);
            assert_eq!(
                witness.count(),
                0,
                "{pattern}: minified content must not parse"
            );
        }
    }

    #[test]
    fn oversized_files_mark_the_scan_partial_with_and_without_a_literal() {
        let directory = tempfile::tempdir().unwrap();
        let large = format!(
            "{}wrap(value)\n",
            "# large\n".repeat(super::super::MAX_SEARCH_FILE_SIZE as usize / 8 + 1)
        );
        std::fs::write(directory.path().join("large.py"), large).unwrap();
        for pattern in ["wrap($A)", "$F($A)"] {
            let result = scan(directory.path(), pattern);
            assert_eq!(result.skipped_files, 1, "{pattern}");
            assert_eq!(result.retained, 0, "{pattern}");
        }
    }

    #[test]
    fn a_cached_revision_is_scanned_without_reading_or_reparsing() {
        let directory = tempfile::tempdir().unwrap();
        let source = "cached_probe_unique(value)\n";
        std::fs::write(directory.path().join("c.py"), source).unwrap();
        let witness = crate::lang::treesitter::ParseWitness::new(source);
        let mut patterns = StructuralPatterns::default();
        patterns
            .prepare("python", "cached_probe_unique($A)")
            .unwrap();
        let cache = OutlineCache::new();
        for _ in 0..2 {
            let result = patterns
                .search(
                    "python",
                    "cached_probe_unique($A)",
                    directory.path(),
                    None,
                    &cache,
                )
                .unwrap();
            assert_eq!(result.retained, 1);
        }
        assert_eq!(witness.count(), 1);
    }
}
