use std::path::Path;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;

use super::file_metadata;

use crate::error::TilthError;
use crate::search::rank;
use crate::types::{FacetTotals, Match, SearchResult};
use grep_regex::RegexMatcher;
use grep_searcher::sinks::UTF8;
use grep_searcher::Searcher;

const MAX_MATCHES: usize = 10;
const EARLY_QUIT_THRESHOLD: usize = MAX_MATCHES * 3;
const FULL_MAX_MATCHES: usize = 100;
const FULL_EARLY_QUIT_THRESHOLD: usize = FULL_MAX_MATCHES * 3;
/// Upper bound on file size searched by content/regex walkers. Files larger
/// than this skip on stat alone. Shared so `content::search` and
/// `super::count_files_for_empty` stay aligned.
pub(crate) const MAX_SEARCH_FILE_SIZE: u64 = 500_000;

/// Shared file-walk entry filter for the content and symbol search walkers:
/// unwraps the walker result, keeps only files, skips filenames that look
/// minified (`.min.js`, `app-min.css`), and skips files over
/// `MAX_SEARCH_FILE_SIZE`. Returns the path and its stat'd size on
/// acceptance. Each caller still does its own read (byte vs string), its own
/// minified-by-content check (needs the read buffer), and its own match
/// building — those differ per caller.
pub(crate) fn accept_walk_entry(
    entry: Result<ignore::DirEntry, ignore::Error>,
) -> Option<(std::path::PathBuf, u64)> {
    let entry = entry.ok()?;

    if !entry.file_type().is_some_and(|ft| ft.is_file()) {
        return None;
    }

    let path = entry.path();

    if path
        .file_name()
        .and_then(|n| n.to_str())
        .is_some_and(crate::lang::detection::is_minified_by_name)
    {
        return None;
    }

    let file_size = match std::fs::metadata(path) {
        Ok(meta) => {
            if meta.len() > MAX_SEARCH_FILE_SIZE {
                return None;
            }
            meta.len()
        }
        Err(_) => 0,
    };

    Some((path.to_path_buf(), file_size))
}

/// Content search using ripgrep crates. Literal by default, regex if `is_regex`.
pub fn search(
    pattern: &str,
    scope: &Path,
    is_regex: bool,
    context: Option<&Path>,
    glob: Option<&str>,
    full: bool,
) -> Result<SearchResult, TilthError> {
    let (max_matches, early_quit) = if full {
        (FULL_MAX_MATCHES, FULL_EARLY_QUIT_THRESHOLD)
    } else {
        (MAX_MATCHES, EARLY_QUIT_THRESHOLD)
    };
    let matcher = if is_regex {
        RegexMatcher::new(pattern)
    } else {
        RegexMatcher::new(&regex_syntax::escape(pattern))
    }
    .map_err(|e| TilthError::InvalidQuery {
        query: pattern.to_string(),
        reason: e.to_string(),
    })?;

    let matches: Mutex<Vec<Match>> = Mutex::new(Vec::new());
    // Relaxed is correct: walker.run() joins all threads before we read the final value.
    // Early-quit checks are approximate by design — one extra iteration is harmless.
    let total_found = AtomicUsize::new(0);

    let walker = super::walker(scope, glob)?;

    walker.run(|| {
        let matcher = &matcher;
        let matches = &matches;
        let total_found = &total_found;

        Box::new(move |entry| {
            if total_found.load(Ordering::Relaxed) >= early_quit {
                return ignore::WalkState::Quit;
            }

            let Some((path, file_size)) = accept_walk_entry(entry) else {
                return ignore::WalkState::Continue;
            };
            let path = path.as_path();

            // Read the file once. Use `search_slice` instead of `search_path`
            // so the minified-check (when triggered) and the actual search
            // share a single kernel read — no double I/O, no TOCTOU window
            // between the heuristic and the search.
            let Ok(bytes) = std::fs::read(path) else {
                return ignore::WalkState::Continue;
            };

            // Catch unmarked minified bundles in the 100KB–500KB range.
            if file_size >= crate::lang::detection::MINIFIED_CHECK_THRESHOLD
                && crate::lang::detection::is_minified_by_content(&bytes)
            {
                return ignore::WalkState::Continue;
            }

            let (file_lines, mtime) = file_metadata(path);

            let mut file_matches = Vec::new();
            let mut searcher = Searcher::new();

            let _ = searcher.search_slice(
                matcher,
                &bytes,
                UTF8(|line_num, line| {
                    file_matches.push(Match {
                        path: path.to_path_buf(),
                        line: line_num as u32,
                        text: line.trim_end().to_string(),
                        is_definition: false,
                        exact: false,
                        file_lines,
                        mtime,
                        def_range: None,
                        def_name: None,
                        def_weight: 0,
                        impl_target: None,
                    });
                    Ok(true)
                }),
            );

            if !file_matches.is_empty() {
                total_found.fetch_add(file_matches.len(), Ordering::Relaxed);
                let mut all = matches
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner);
                all.extend(file_matches);
            }

            if total_found.load(Ordering::Relaxed) >= early_quit {
                ignore::WalkState::Quit
            } else {
                ignore::WalkState::Continue
            }
        })
    });

    let total = total_found.load(Ordering::Relaxed);
    let mut all_matches = matches
        .into_inner()
        .unwrap_or_else(std::sync::PoisonError::into_inner);

    rank::sort(&mut all_matches, pattern, scope, context);
    all_matches.truncate(max_matches);

    Ok(SearchResult {
        query: pattern.to_string(),
        scope: scope.to_path_buf(),
        matches: all_matches,
        total_found: total,
        definitions: 0,
        usages: total,
        facet_totals: FacetTotals::default(),
    })
}
