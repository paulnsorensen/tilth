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
const FULL_MAX_MATCHES: usize = 100;
const EARLY_QUIT_FACTOR: usize = 3;
/// Response tokens one content hit costs on average; sizes the hit cap from a budget.
const TOKENS_PER_HIT: u64 = 400;

/// Content hits to keep for a response budget in tokens: the fixed floor for
/// small budgets, growing with the budget up to the full-search ceiling.
#[must_use]
pub fn match_cap_for_budget(budget: u64) -> usize {
    let cap = usize::try_from(budget / TOKENS_PER_HIT).unwrap_or(FULL_MAX_MATCHES);
    cap.clamp(MAX_MATCHES, FULL_MAX_MATCHES)
}

/// Keep `cap` ranked matches, taking the best match of each distinct file
/// before a second match of any file, so one noisy file cannot hide the rest.
/// Survivors keep their rank order.
fn cap_with_file_coverage(ranked: Vec<Match>, cap: usize) -> Vec<Match> {
    if ranked.len() <= cap {
        return ranked;
    }
    let mut picked = vec![false; ranked.len()];
    let mut files = std::collections::HashSet::new();
    let mut count = 0;
    for (index, m) in ranked.iter().enumerate() {
        if count == cap {
            break;
        }
        if files.insert(m.path.clone()) {
            picked[index] = true;
            count += 1;
        }
    }
    for flag in &mut picked {
        if count == cap {
            break;
        }
        if !*flag {
            *flag = true;
            count += 1;
        }
    }
    let mut kept = Vec::with_capacity(cap);
    for (m, flag) in ranked.into_iter().zip(picked) {
        if flag {
            kept.push(m);
        }
    }
    kept
}

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
    let max_matches = if full { FULL_MAX_MATCHES } else { MAX_MATCHES };
    search_capped(pattern, scope, is_regex, context, glob, max_matches)
}

/// Like `search`, keeping at most `max_matches` hits.
pub fn search_capped(
    pattern: &str,
    scope: &Path,
    is_regex: bool,
    context: Option<&Path>,
    glob: Option<&str>,
    max_matches: usize,
) -> Result<(SearchResult, Option<String>), TilthError> {
    let early_quit = max_matches * EARLY_QUIT_FACTOR;
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
    let all_matches = cap_with_file_coverage(all_matches, max_matches);

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

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn hit(path: &str, line: u32) -> Match {
        Match {
            path: PathBuf::from(path),
            line,
            text: String::new(),
            is_definition: false,
            exact: false,
            file_lines: 0,
            mtime: std::time::SystemTime::UNIX_EPOCH,
            def_range: None,
            def_byte_range: None,
            def_name: None,
            def_weight: 0,
            impl_target: None,
        }
    }

    #[test]
    fn cap_grows_with_budget_between_the_floor_and_ceiling() {
        assert_eq!(match_cap_for_budget(1), MAX_MATCHES);
        assert_eq!(match_cap_for_budget(8_000), 20);
        assert_eq!(match_cap_for_budget(24_000), 60);
        assert_eq!(match_cap_for_budget(10_000_000), FULL_MAX_MATCHES);
    }

    #[test]
    fn cap_covers_distinct_files_before_repeating_one() {
        let mut ranked: Vec<Match> = (1..=6).map(|line| hit("noisy.rs", line)).collect();
        ranked.push(hit("b.rs", 1));
        ranked.push(hit("c.rs", 1));
        let kept = cap_with_file_coverage(ranked, 4);
        let labels: Vec<(String, u32)> = kept
            .iter()
            .map(|m| (m.path.to_string_lossy().into_owned(), m.line))
            .collect();
        assert_eq!(
            labels,
            vec![
                ("noisy.rs".to_string(), 1),
                ("noisy.rs".to_string(), 2),
                ("b.rs".to_string(), 1),
                ("c.rs".to_string(), 1),
            ]
        );
    }
}
