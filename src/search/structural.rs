//! Read-only structural matching over retained source/tree snapshots.
use std::borrow::Cow;
use std::collections::{BTreeMap, HashMap};
use std::ops::Range;
use std::path::Path;
use std::sync::{Arc, Mutex};

use ast_grep_core::matcher::PatternBuilder;
use ast_grep_core::meta_var::MetaVariable;
use ast_grep_core::source::{Content, Edit};
use ast_grep_core::tree_sitter::StrDoc;
use ast_grep_core::{AstGrep, Doc, Language, Node, Pattern, PatternError};
use ast_grep_language::SupportLang;
use serde::Serialize;

use crate::cache::{OutlineCache, ParsedFile};
use crate::error::TilthError;
use crate::types::{FileType, Lang};

/// The adapter retains the exact cached source allocation and tree.
#[derive(Clone)]
struct SnapshotDoc {
    snapshot: Arc<ParsedFile>,
    language: SupportLang,
}

impl Content for SnapshotDoc {
    type Underlying = u8;
    fn get_range(&self, range: Range<usize>) -> &[u8] {
        self.snapshot.content.get_range(range)
    }
    fn decode_str(source: &str) -> Cow<'_, [u8]> {
        String::decode_str(source)
    }
    fn encode_bytes(bytes: &[u8]) -> Cow<'_, str> {
        String::encode_bytes(bytes)
    }
    fn get_char_column(&self, column: usize, offset: usize) -> usize {
        self.snapshot.content.get_char_column(column, offset)
    }
}

impl Doc for SnapshotDoc {
    type Source = Self;
    type Lang = SupportLang;
    type Node<'r> = tree_sitter::Node<'r>;

    fn get_lang(&self) -> &Self::Lang {
        &self.language
    }
    fn get_source(&self) -> &Self::Source {
        self
    }
    fn root_node(&self) -> Self::Node<'_> {
        self.snapshot.tree.root_node()
    }
    fn get_node_text<'a>(&'a self, node: &Self::Node<'a>) -> Cow<'a, str> {
        Cow::Borrowed(&self.snapshot.content[node.byte_range()])
    }
    fn do_edit(&mut self, _edit: &Edit<Self::Source>) -> Result<(), String> {
        Err("structural search snapshots are read-only".into())
    }
}

/// Byte offsets are zero-based and half-open. Lines identify both endpoints.
#[derive(Serialize)]
struct Location {
    start_byte: usize,
    end_byte: usize,
    start_line: usize,
    end_line: usize,
}

impl Location {
    fn of(node: &Node<'_, SnapshotDoc>) -> Self {
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
}

/// Bound match retention independently of the response token budget.
const MAX_MATCHES: usize = 1000;

struct CompiledPattern {
    language: SupportLang,
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
                language,
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
                let Some(snapshot) = cache.get_or_parse(entry.path()) else {
                    scan.lock().unwrap().skipped_files += 1;
                    return ignore::WalkState::Continue;
                };
                let root = AstGrep::doc(SnapshotDoc {
                    snapshot,
                    language: compiled.language,
                });
                for matched in root.root().find_all(&compiled.pattern) {
                    let mut scan = scan.lock().unwrap();
                    if scan.items.len() == MAX_MATCHES {
                        scan.limited = true;
                        return ignore::WalkState::Quit;
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
                    scan.items.push(StructuralItem {
                        path: crate::format::rel(entry.path(), scope),
                        range: Location::of(&matched),
                        captures,
                    });
                }
                ignore::WalkState::Continue
            })
        });
        let mut scan = scan.into_inner().unwrap();
        scan.items.sort_by(|left, right| {
            (&left.path, left.range.start_byte, left.range.end_byte).cmp(&(
                &right.path,
                right.range.start_byte,
                right.range.end_byte,
            ))
        });
        Ok(scan)
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
    fn snapshot_adapter_borrows_cached_bytes_and_tree() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("source.py");
        std::fs::write(&path, "wrap(value)\n").unwrap();
        let cache = OutlineCache::new();
        let snapshot = cache.get_or_parse(&path).unwrap();
        let document = SnapshotDoc {
            snapshot: Arc::clone(&snapshot),
            language: SupportLang::Python,
        };
        assert!(Arc::ptr_eq(&snapshot, &document.snapshot));
        assert_eq!(document.root_node().id(), snapshot.tree.root_node().id());
        assert_eq!(document.get_range(0..4).as_ptr(), snapshot.content.as_ptr());
        let root = AstGrep::doc(document);
        let pattern = Pattern::try_new("wrap($A)", PatternLanguage(SupportLang::Python)).unwrap();
        let matched = root.root().find(&pattern).unwrap();
        assert!(matches!(matched.text(), Cow::Borrowed("wrap(value)")));
        assert_eq!(matched.text().as_ptr(), snapshot.content.as_ptr());
    }
}
