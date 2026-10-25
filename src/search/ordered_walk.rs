//! Deterministic capped directory walk.
//!
//! The parallel `ignore` walker yields files in thread-timing order, so an
//! early stop at a match cap kept a different subset of matches on every run.
//! This helper collects the file list first, sorts it by path, and scans it in
//! fixed-size batches. The cap is checked only between batches, so the set of
//! files scanned depends on the tree and the query, never on thread timing.

use std::path::Path;
use std::sync::Mutex;

use ignore::DirEntry;
use rayon::prelude::*;

use crate::error::TilthError;

/// Files scanned per batch. The cap is evaluated after each batch.
const BATCH_FILES: usize = 128;

/// Walk `scope` and run `scan` on each entry in sorted path order, in parallel
/// within a batch. Stop after the first batch that brings the result count to
/// `early_quit` or more. Results keep sorted file order.
pub(crate) fn walk_ordered<T, F>(
    scope: &Path,
    glob: Option<&str>,
    early_quit: usize,
    scan: F,
) -> Result<Vec<T>, TilthError>
where
    T: Send,
    F: Fn(DirEntry) -> Vec<T> + Sync,
{
    let collected: Mutex<Vec<DirEntry>> = Mutex::new(Vec::new());
    super::walker(scope, glob)?.run(|| {
        let collected = &collected;
        Box::new(move |entry| {
            if let Ok(entry) = entry {
                collected
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner)
                    .push(entry);
            }
            ignore::WalkState::Continue
        })
    });
    let mut entries = collected
        .into_inner()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    entries.sort_by(|a, b| a.path().cmp(b.path()));

    let pool = rayon::ThreadPoolBuilder::new()
        .num_threads(super::walker_threads())
        .build()
        .ok();

    let mut results: Vec<T> = Vec::new();
    let mut remaining = entries.into_iter();
    loop {
        let batch: Vec<DirEntry> = remaining.by_ref().take(BATCH_FILES).collect();
        if batch.is_empty() {
            break;
        }
        let scan_batch = || -> Vec<Vec<T>> { batch.into_par_iter().map(&scan).collect() };
        let per_file = match &pool {
            Some(pool) => pool.install(scan_batch),
            None => scan_batch(),
        };
        results.extend(per_file.into_iter().flatten());
        if results.len() >= early_quit {
            break;
        }
    }
    Ok(results)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cache::OutlineCache;

    /// More matching files than any default cap, spread over directories so
    /// the parallel walker interleaves them differently from run to run.
    fn capped_tree() -> tempfile::TempDir {
        let tmp = tempfile::tempdir().unwrap();
        for dir in 0..8 {
            let d = tmp.path().join(format!("d{dir}"));
            std::fs::create_dir_all(&d).unwrap();
            for file in 0..60 {
                std::fs::write(
                    d.join(format!("f{file:02}.rs")),
                    format!("fn helper_{dir}_{file}() {{}}\n// capsule_marker {dir} {file}\n"),
                )
                .unwrap();
            }
        }
        tmp
    }

    #[test]
    fn walk_ordered_keeps_the_sorted_prefix() {
        let tmp = capped_tree();
        let scan = |entry: DirEntry| {
            if entry.file_type().is_some_and(|ft| ft.is_file()) {
                vec![entry.into_path()]
            } else {
                Vec::new()
            }
        };
        let first = walk_ordered(tmp.path(), None, 30, scan).unwrap();
        assert!(
            (30..=BATCH_FILES).contains(&first.len()),
            "stops after the first batch: {}",
            first.len()
        );
        let mut sorted = first.clone();
        sorted.sort();
        assert_eq!(first, sorted, "results keep sorted path order");
        for _ in 0..30 {
            assert_eq!(walk_ordered(tmp.path(), None, 30, scan).unwrap(), first);
        }
    }

    #[test]
    fn capped_content_search_is_byte_identical_across_runs() {
        let tmp = capped_tree();
        let cache = OutlineCache::new();
        let run =
            || crate::search::search_content("capsule_marker", tmp.path(), &cache, None).unwrap();
        let first = run();
        for i in 0..40 {
            assert_eq!(run(), first, "content search output changed on run {i}");
        }
    }

    #[test]
    fn capped_symbol_search_is_identical_across_runs() {
        let tmp = capped_tree();
        let run = || {
            let result =
                crate::search::symbol::search("capsule_marker", tmp.path(), None, None, false)
                    .unwrap();
            let hits: Vec<_> = result
                .matches
                .iter()
                .map(|m| (m.path.clone(), m.line, m.text.clone()))
                .collect();
            (hits, result.total_found)
        };
        let first = run();
        for i in 0..40 {
            assert_eq!(run(), first, "symbol search result changed on run {i}");
        }
    }
}
