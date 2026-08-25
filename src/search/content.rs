use std::path::Path;

use super::{accept_walk_entry, file_metadata};

use crate::error::TilthError;
use crate::search::rank;
use crate::search::retain::{BoundedRetain, MAX_RETAINED};
use crate::types::{FacetTotals, Match, SearchResult};
use grep_regex::RegexMatcher;
use grep_searcher::sinks::UTF8;
use grep_searcher::Searcher;

const MAX_MATCHES: usize = 10;
const FULL_MAX_MATCHES: usize = 100;

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

    let sink = BoundedRetain::new(MAX_RETAINED);

    let base = super::scope_base(scope);
    let walker = super::walker(scope, glob)?;

    super::run_walk(walker, || {
        let matcher = &matcher;
        let sink = &sink;
        let mut scorer = rank::Scorer::new(pattern, base, context);

        Box::new(move |entry| {
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
                sink.offer_file(file_matches, &mut scorer);
            }

            ignore::WalkState::Continue
        })
    });

    let total = sink.offered();
    let mut all_matches = sink.into_matches();

    rank::sort(&mut all_matches, pattern, base, context);
    all_matches.truncate(max_matches);

    Ok((
        SearchResult {
            query: pattern.to_string(),
            scope: base.to_path_buf(),
            walk_root: scope.to_path_buf(),
            matches: all_matches,
            total_found: total,
            definitions: 0,
            usages: total,
            facet_totals: FacetTotals::default(),
        },
        fallback_reason,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Guards the bounded-retention port: a dense-match tree that exceeds
    /// `retain::MAX_RETAINED` must still report an exact total and a stable
    /// retained/displayed order across runs — the property the old shared-counter
    /// early-quit could not guarantee (see `search::retain`'s module doc).
    #[test]
    fn dense_matches_report_exact_total_and_stable_order_under_retention_cap() {
        let dir = tempfile::tempdir().expect("tempdir");
        let scope = dir.path();

        // 30 files x 100 lines each containing the pattern = 3_000 raw matches,
        // comfortably past MAX_RETAINED (2_000) so the bound actually clips.
        const FILES: usize = 30;
        const LINES_PER_FILE: usize = 100;
        for i in 0..FILES {
            let path = scope.join(format!("file_{i:03}.rs"));
            let content: String = (0..LINES_PER_FILE)
                .map(|n| format!("let widget_{n} = load_widget();\n"))
                .collect();
            std::fs::write(&path, content).expect("write");
        }

        let (first, _) = search("widget", scope, false, None, None, false).expect("search 1");
        let (second, _) = search("widget", scope, false, None, None, false).expect("search 2");

        let expected_total = FILES * LINES_PER_FILE;
        assert_eq!(
            first.total_found, expected_total,
            "total_found must be exact even though retention bounds what is kept"
        );
        assert_eq!(second.total_found, expected_total);

        assert!(
            first.matches.len() <= MAX_MATCHES,
            "display cap must still apply: got {}",
            first.matches.len()
        );

        let key = |r: &SearchResult| -> Vec<(std::path::PathBuf, u32, String)> {
            r.matches
                .iter()
                .map(|m| (m.path.clone(), m.line, m.text.clone()))
                .collect()
        };
        assert_eq!(
            key(&first),
            key(&second),
            "retained/displayed order must be stable across runs on an identical tree"
        );
    }
}
