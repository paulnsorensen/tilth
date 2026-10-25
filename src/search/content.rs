use std::path::Path;
use std::sync::atomic::{AtomicUsize, Ordering};

use super::{accept_walk_entry, file_metadata};

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

/// Content search using ripgrep crates. Literal by default, regex if `is_regex`.
/// Returns the result plus, when a regex pattern failed to compile and was
/// retried as an escaped literal, the parse-failure reason (`None` on the
/// literal path, which is unaffected).
pub fn search(
    pattern: &str,
    scope: &Path,
    is_regex: bool,
    context: Option<&Path>,
    glob: Option<&str>,
    full: bool,
) -> Result<(SearchResult, Option<String>), TilthError> {
    let (max_matches, early_quit) = if full {
        (FULL_MAX_MATCHES, FULL_EARLY_QUIT_THRESHOLD)
    } else {
        (MAX_MATCHES, EARLY_QUIT_THRESHOLD)
    };
    let invalid_query = |reason: String| TilthError::InvalidQuery {
        query: pattern.to_string(),
        reason,
    };
    let (matcher, fallback_reason) = if is_regex {
        match RegexMatcher::new(pattern) {
            Ok(m) => (m, None),
            Err(e) => {
                let reason = e.to_string();
                let literal = RegexMatcher::new(&regex_syntax::escape(pattern)).map_err(|e2| {
                    invalid_query(format!("{reason}; escaped-literal retry also failed: {e2}"))
                })?;
                (literal, Some(reason))
            }
        }
    } else {
        let m = RegexMatcher::new(&regex_syntax::escape(pattern))
            .map_err(|e| invalid_query(e.to_string()))?;
        (m, None)
    };

    let files_unreadable = AtomicUsize::new(0);

    // Files are scanned in sorted path order and the cap is applied between
    // batches, so the surviving matches never depend on thread timing.
    let mut all_matches = super::ordered_walk::walk_ordered(scope, glob, early_quit, |entry| {
        let Some((path, file_size, _)) = accept_walk_entry(Ok(entry)) else {
            return Vec::new();
        };
        let path = path.as_path();

        // Read the file once. Use `search_slice` instead of `search_path`
        // so the minified-check (when triggered) and the actual search
        // share a single kernel read — no double I/O, no TOCTOU window
        // between the heuristic and the search.
        let Ok(bytes) = std::fs::read(path) else {
            files_unreadable.fetch_add(1, Ordering::Relaxed);
            return Vec::new();
        };

        // Catch unmarked minified bundles in the 100KB–500KB range.
        if file_size >= crate::lang::detection::MINIFIED_CHECK_THRESHOLD
            && crate::lang::detection::is_minified_by_content(&bytes)
        {
            return Vec::new();
        }

        let (file_lines, mtime) = file_metadata(path);

        let mut file_matches = Vec::new();
        let mut searcher = Searcher::new();

        // A matching line with invalid UTF-8 makes the `UTF8` sink error
        // and drops the match. Count the file as unreadable (unless it is a
        // binary blob) so the completeness signal stays honest.
        let search_ok = searcher
            .search_slice(
                &matcher,
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
                        def_byte_range: None,
                        def_name: None,
                        def_weight: 0,
                        impl_target: None,
                    });
                    Ok(true)
                }),
            )
            .is_ok();
        if !search_ok && !crate::lang::detection::is_binary(&bytes) {
            files_unreadable.fetch_add(1, Ordering::Relaxed);
        }
        file_matches
    })?;

    let total = all_matches.len();

    rank::sort(&mut all_matches, pattern, scope, context);
    all_matches.truncate(max_matches);

    Ok((
        SearchResult {
            query: pattern.to_string(),
            scope: scope.to_path_buf(),
            matches: all_matches,
            total_found: total,
            definitions: 0,
            usages: total,
            files_unreadable: files_unreadable.load(Ordering::Relaxed),
            facet_totals: FacetTotals::default(),
        },
        fallback_reason,
    ))
}
