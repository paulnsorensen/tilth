//! Fuzzy, suggest-only symbol names for unresolved plain grok targets.

use std::collections::HashSet;
use std::fs;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use nucleo_matcher::pattern::{Atom, AtomKind, CaseMatching, Normalization};
use nucleo_matcher::{Config, Matcher, Utf32Str};

use crate::lang::detect_file_type;
use crate::lang::outline::get_outline_entries;
use crate::types::{FileType, OutlineEntry, OutlineKind};

const MAX_FUZZY_CANDIDATES: usize = 20_000;
const SUGGESTION_K: usize = 3;

pub(crate) fn suggestions(scope: &Path, query: &str) -> Option<Vec<String>> {
    let (candidates, truncated) = collect_candidates(scope);
    if truncated {
        eprintln!(
            "tilth: fuzzy symbol resolution scored only the first {MAX_FUZZY_CANDIDATES} definitions \
             (scope larger than cap) for query {query:?} — result may miss a better match"
        );
    }

    let mut scored = score_candidates(query, &candidates);
    if scored.is_empty() {
        return None;
    }
    scored.sort_by_key(|&(score, _)| std::cmp::Reverse(score));
    Some(
        scored
            .into_iter()
            .take(SUGGESTION_K)
            .map(|(_, name)| name)
            .collect(),
    )
}

fn score_candidates(query: &str, candidates: &[String]) -> Vec<(u16, String)> {
    let mut matcher = Matcher::new(Config::DEFAULT);
    let atom = Atom::new(
        query,
        CaseMatching::Smart,
        Normalization::Smart,
        AtomKind::Fuzzy,
        false,
    );
    let mut buf = Vec::new();
    candidates
        .iter()
        .filter_map(|name| {
            let haystack = Utf32Str::new(name, &mut buf);
            atom.score(haystack, &mut matcher)
                .map(|score| (score, name.clone()))
        })
        .collect()
}

fn collect_candidates(scope: &Path) -> (Vec<String>, bool) {
    let Ok(walker) = crate::search::walker(scope, None) else {
        return (Vec::new(), false);
    };

    let candidates = Mutex::new(HashSet::new());
    let truncated = AtomicBool::new(false);

    walker.run(|| {
        let candidates = &candidates;
        let truncated = &truncated;
        Box::new(move |entry| {
            let Ok(entry) = entry else {
                return ignore::WalkState::Continue;
            };
            if !entry.file_type().is_some_and(|ft| ft.is_file()) {
                return ignore::WalkState::Continue;
            }
            let FileType::Code(lang) = detect_file_type(entry.path()) else {
                return ignore::WalkState::Continue;
            };
            let Ok(content) = fs::read_to_string(entry.path()) else {
                return ignore::WalkState::Continue;
            };
            let entries = get_outline_entries(&content, lang);
            let mut candidates = candidates
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            if !collect_entry_names(&entries, &mut candidates) {
                truncated.store(true, Ordering::Relaxed);
                return ignore::WalkState::Quit;
            }
            ignore::WalkState::Continue
        })
    });

    let mut candidates: Vec<_> = candidates
        .into_inner()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
        .into_iter()
        .collect();
    candidates.sort_unstable();
    (candidates, truncated.load(Ordering::Relaxed))
}

fn collect_entry_names(entries: &[OutlineEntry], candidates: &mut HashSet<String>) -> bool {
    for entry in entries {
        let is_definition = !matches!(entry.kind, OutlineKind::Import | OutlineKind::Export)
            && !entry.name.is_empty()
            && !entry.name.starts_with('<')
            && !entry.name.starts_with("impl ");
        if is_definition && !candidates.contains(&entry.name) {
            if candidates.len() == MAX_FUZZY_CANDIDATES {
                return false;
            }
            candidates.insert(entry.name.clone());
        }
        if !collect_entry_names(&entry.children, candidates) {
            return false;
        }
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::OutlineKind;

    #[test]
    fn candidate_collection_hard_caps_and_reports_truncation() {
        let entries: Vec<_> = (0..=MAX_FUZZY_CANDIDATES)
            .map(|i| OutlineEntry {
                kind: OutlineKind::Function,
                name: format!("candidate_{i}"),
                start_line: 1,
                end_line: 1,
                signature: None,
                children: Vec::new(),
                doc: None,
            })
            .collect();
        let mut candidates = HashSet::new();

        assert!(!collect_entry_names(&entries, &mut candidates));
        assert_eq!(candidates.len(), MAX_FUZZY_CANDIDATES);
    }

    #[test]
    fn candidate_collection_filters_non_definition_names() {
        let entries = vec![
            OutlineEntry {
                kind: OutlineKind::Import,
                name: "imported".to_string(),
                start_line: 1,
                end_line: 1,
                signature: None,
                children: vec![OutlineEntry {
                    kind: OutlineKind::Function,
                    name: "nested_real".to_string(),
                    start_line: 1,
                    end_line: 1,
                    signature: None,
                    children: Vec::new(),
                    doc: None,
                }],
                doc: None,
            },
            OutlineEntry {
                kind: OutlineKind::Export,
                name: "exported".to_string(),
                start_line: 1,
                end_line: 1,
                signature: None,
                children: Vec::new(),
                doc: None,
            },
            OutlineEntry {
                kind: OutlineKind::Function,
                name: "<top-level>".to_string(),
                start_line: 1,
                end_line: 1,
                signature: None,
                children: Vec::new(),
                doc: None,
            },
            OutlineEntry {
                kind: OutlineKind::Function,
                name: "impl Foo".to_string(),
                start_line: 1,
                end_line: 1,
                signature: None,
                children: Vec::new(),
                doc: None,
            },
            OutlineEntry {
                kind: OutlineKind::Function,
                name: "real_symbol".to_string(),
                start_line: 1,
                end_line: 1,
                signature: None,
                children: Vec::new(),
                doc: None,
            },
        ];
        let mut candidates = HashSet::new();

        assert!(collect_entry_names(&entries, &mut candidates));
        assert_eq!(
            candidates,
            HashSet::from(["nested_real".to_string(), "real_symbol".to_string()])
        );
    }
}
