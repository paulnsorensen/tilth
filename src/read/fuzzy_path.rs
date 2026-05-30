//! Fuzzy path resolution for missing path-like queries.
//!
//! When an agent hands tilth a slightly-off path (wrong directory component,
//! missing prefix, basename-only), this resolves it to the best-matching real
//! file via subsequence path matching (`nucleo-matcher` in `match_paths` mode)
//! so the caller can auto-open it instead of only emitting a "did you mean".
//!
//! Cold path only — never invoked on a successful read or search. The matcher
//! lives entirely behind [`resolve_fuzzy_path`]; the documented step-down (swap
//! to a no-dependency basename match) is a body swap, not a rewrite.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Mutex;

use nucleo_matcher::pattern::{Atom, AtomKind, CaseMatching, Normalization};
use nucleo_matcher::{Config, Matcher, Utf32Str};

use crate::cache::OutlineCache;
use crate::error::TilthError;

// ── tuning constants ──────────────────────────────────────────────────────
// All gate thresholds live here. nucleo's `SCORE_MATCH` is 16 per matched
// char plus boundary bonuses, so a genuine path/basename match scores in the
// low hundreds while an incidental subsequence match scores far lower.

/// Upper bound on files scored. The cold-path whole-tree walk stops here and
/// the truncation is logged — never silently capped (project rule).
const MAX_FUZZY_CANDIDATES: usize = 20_000;

/// How many candidates to return as "did you mean" suggestions.
const SUGGESTION_K: usize = 3;

/// Read profile: lenient. Auto-open on a clear winner across the whole tree.
const READ_MIN_SCORE: u16 = 40;
const READ_MARGIN: f32 = 1.10;

/// Search profile: stricter. A file materializing from a *search* call is more
/// surprising, so demand a higher floor and a wider margin over the runner-up.
const SEARCH_MIN_SCORE: u16 = 80;
const SEARCH_MARGIN: f32 = 1.25;

/// A scored fuzzy candidate. `path` is scope-relative.
pub struct FuzzyHit {
    pub path: PathBuf,
    pub score: u16,
}

impl FuzzyHit {
    /// Emit an operator-log line recording an auto-open. Cold path, so the
    /// stderr trace is rare; it explains *why* a path the agent didn't ask for
    /// materialized, and surfaces the match `score` behind the decision.
    pub fn log_auto_open(&self, query: &str) {
        eprintln!(
            "tilth: fuzzy-resolved {query:?} → {:?} (score {})",
            self.path.display(),
            self.score
        );
    }
}

/// Outcome of resolving a missing path-like query.
pub enum FuzzyResolution {
    /// Gate passed — caller may auto-open this file.
    Resolved(FuzzyHit),
    /// Ambiguous or low-confidence — feed a "did you mean" list.
    Suggestions(Vec<String>),
    /// No subsequence candidate — caller keeps the unchanged `NotFound`.
    None,
}

/// Tuning profile per call site. `Read` is broad/lenient; `Search` is tight and
/// additionally requires the query to look path-like before it will auto-open.
#[derive(Clone, Copy)]
pub enum GateProfile {
    Read,
    Search,
}

/// Resolve `query` against the gitignore-pruned file tree rooted at `scope`.
///
/// Walks `search::walker(scope, None)`, scores every file's scope-relative path
/// against `query` with `nucleo-matcher`'s path-aware matcher, and applies the
/// confidence gate for `gate`. nucleo returns `None` unless `query` is a
/// subsequence of the candidate — that hard filter rejects stale/garbage paths
/// (they never auto-resolve).
#[must_use]
pub fn resolve_fuzzy_path(scope: &Path, query: &str, gate: GateProfile) -> FuzzyResolution {
    let (candidates, truncated) = collect_candidates(scope);
    if truncated {
        eprintln!(
            "tilth: fuzzy path resolution scored only the first {MAX_FUZZY_CANDIDATES} files \
             (tree larger than cap) for query {query:?} — result may miss a better match"
        );
    }

    let mut scored = score_candidates(query, &candidates);
    if scored.is_empty() {
        return FuzzyResolution::None;
    }
    // Highest score first; stable tie order keeps equal-score candidates as the
    // walker yielded them (sorted by path in `collect_candidates`).
    scored.sort_by_key(|&(score, _)| std::cmp::Reverse(score));

    apply_gate(scored, gate, query)
}

/// Read a confidently-resolved search hit and prepend the distinct search
/// auto-open header. Shared by the basic-path fallback (`lib::fuzzy_path_fallback`)
/// and the expanded MCP path so the header text never drifts between the two.
pub fn search_auto_open_body(
    scope: &Path,
    hit: &FuzzyHit,
    query: &str,
    cache: &OutlineCache,
) -> Result<String, TilthError> {
    hit.log_auto_open(query);
    let real = scope.join(&hit.path);
    let body = super::read_file(&real, None, false, cache, false)?;
    Ok(format!(
        "# {} (resolved from path-like query \"{query}\") · no search matches; closest file auto-opened\n\n{body}",
        hit.path.display()
    ))
}

/// Expanded-search entry point for the MCP `tilth_search` default path, which
/// returns an empty-result header on a miss and so never reaches the basic-path
/// `fuzzy_path_fallback`. Pre-checks [`is_path_like`] (so a normal empty symbol
/// search never walks the tree), resolves under [`GateProfile::Search`], and on
/// a confident hit returns the auto-open body. `None` ⇒ caller keeps its own
/// empty-result output unchanged.
///
/// Callers must confirm the search produced no matches before invoking this —
/// the header asserts "no search matches".
#[must_use]
pub fn auto_open_search_miss(scope: &Path, query: &str, cache: &OutlineCache) -> Option<String> {
    if !is_path_like(query) {
        return None;
    }
    match resolve_fuzzy_path(scope, query, GateProfile::Search) {
        FuzzyResolution::Resolved(hit) => search_auto_open_body(scope, &hit, query, cache).ok(),
        FuzzyResolution::Suggestions(_) | FuzzyResolution::None => None,
    }
}

/// nucleo path-matcher over collected scope-relative paths. Returns
/// `(score, relative_path)` for every subsequence match. The `PathBuf` is
/// built only for the handful of matches — non-matching candidates stay
/// `String`-only from the walk.
fn score_candidates(query: &str, candidates: &[String]) -> Vec<(u16, PathBuf)> {
    let mut matcher = Matcher::new(Config::DEFAULT.match_paths());
    let atom = Atom::new(
        query,
        CaseMatching::Smart,
        Normalization::Smart,
        AtomKind::Fuzzy,
        false,
    );
    let mut buf: Vec<char> = Vec::new();
    candidates
        .iter()
        .filter_map(|rel_str| {
            let haystack = Utf32Str::new(rel_str, &mut buf);
            atom.score(haystack, &mut matcher)
                .map(|score| (score, PathBuf::from(rel_str)))
        })
        .collect()
}

/// Apply the confidence gate to score-sorted candidates.
///
/// `Resolved` iff (for `Search`, the query is path-like AND) the top candidate
/// is the unique match, or it clears `MIN_SCORE` and beats the runner-up by
/// `MARGIN`. Otherwise the top `SUGGESTION_K` become a "did you mean" list.
fn apply_gate(scored: Vec<(u16, PathBuf)>, gate: GateProfile, query: &str) -> FuzzyResolution {
    let (min_score, margin, require_path_like) = match gate {
        GateProfile::Read => (READ_MIN_SCORE, READ_MARGIN, false),
        GateProfile::Search => (SEARCH_MIN_SCORE, SEARCH_MARGIN, true),
    };

    let path_like_ok = !require_path_like || is_path_like(query);
    let (top_score, top_path) = &scored[0];
    let unique = scored.len() == 1;
    let clears_margin = unique || f32::from(*top_score) >= f32::from(scored[1].0) * margin;
    let clears_floor = unique || *top_score >= min_score;

    if path_like_ok && clears_margin && clears_floor {
        return FuzzyResolution::Resolved(FuzzyHit {
            path: top_path.clone(),
            score: *top_score,
        });
    }

    let suggestions = scored
        .into_iter()
        .take(SUGGESTION_K)
        .map(|(_, p)| p.to_string_lossy().into_owned())
        .collect();
    FuzzyResolution::Suggestions(suggestions)
}

/// A query is path-like when it contains a path separator and the final
/// segment has a file extension. Used by the `Search` gate to refuse
/// auto-opening a bare-concept query that merely happens to fuzzy-match a file.
pub fn is_path_like(query: &str) -> bool {
    query.contains('/') && Path::new(query).extension().is_some()
}

/// Walk the gitignore-pruned tree under `scope`, collecting scope-relative path
/// strings for files only. Returns `(candidates, truncated)`; `truncated` is
/// true when the walk stopped at `MAX_FUZZY_CANDIDATES`.
fn collect_candidates(scope: &Path) -> (Vec<String>, bool) {
    let Ok(walker) = crate::search::walker(scope, None) else {
        return (Vec::new(), false);
    };

    let collected: Mutex<Vec<String>> = Mutex::new(Vec::new());
    let count = AtomicUsize::new(0);
    let truncated = AtomicBool::new(false);

    walker.run(|| {
        let collected = &collected;
        let count = &count;
        let truncated = &truncated;
        Box::new(move |entry| {
            let Ok(entry) = entry else {
                return ignore::WalkState::Continue;
            };
            if !entry.file_type().is_some_and(|ft| ft.is_file()) {
                return ignore::WalkState::Continue;
            }
            if count.fetch_add(1, Ordering::Relaxed) >= MAX_FUZZY_CANDIDATES {
                truncated.store(true, Ordering::Relaxed);
                return ignore::WalkState::Quit;
            }
            let path = entry.path();
            let rel = path.strip_prefix(scope).unwrap_or(path);
            let rel_str = rel.to_string_lossy().into_owned();
            collected
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .push(rel_str);
            ignore::WalkState::Continue
        })
    });

    let mut v = collected
        .into_inner()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    // Deterministic order so equal-score ties resolve stably (and tests are
    // reproducible regardless of walk thread interleaving).
    v.sort();
    (v, truncated.load(Ordering::Relaxed))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Build a fixture tree under a fresh temp dir from a list of relative paths.
    fn fixture(paths: &[&str]) -> tempfile::TempDir {
        let dir = tempfile::tempdir().unwrap();
        for rel in paths {
            let p = dir.path().join(rel);
            std::fs::create_dir_all(p.parent().unwrap()).unwrap();
            std::fs::write(&p, "fixture\n").unwrap();
        }
        dir
    }

    fn resolved_path(res: &FuzzyResolution) -> Option<String> {
        match res {
            FuzzyResolution::Resolved(hit) => Some(hit.path.to_string_lossy().into_owned()),
            _ => None,
        }
    }

    #[test]
    fn resolves_wrong_dir_missing_prefix() {
        // `_mcp_core.py` given, real file lives under src/milknado/.
        let dir = fixture(&[
            "src/milknado/_mcp_core.py",
            "src/other/util.py",
            "README.md",
        ]);
        let res = resolve_fuzzy_path(dir.path(), "_mcp_core.py", GateProfile::Read);
        assert_eq!(
            resolved_path(&res).as_deref(),
            Some("src/milknado/_mcp_core.py"),
            "wrong-dir/missing-prefix query should auto-resolve"
        );
    }

    #[test]
    fn resolves_partial_basename_under_read() {
        let dir = fixture(&["src/search/symbol.rs", "src/lib.rs", "src/read/mod.rs"]);
        let res = resolve_fuzzy_path(dir.path(), "symbol.rs", GateProfile::Read);
        assert_eq!(
            resolved_path(&res).as_deref(),
            Some("src/search/symbol.rs"),
            "basename-only query should resolve to the single match"
        );
    }

    #[test]
    fn resolves_deletion_typo() {
        // `serch` is missing the `a` from `search` — still a subsequence.
        let dir = fixture(&["src/search/symbol.rs", "src/lib.rs"]);
        let res = resolve_fuzzy_path(dir.path(), "serch/symbol.rs", GateProfile::Read);
        assert_eq!(
            resolved_path(&res).as_deref(),
            Some("src/search/symbol.rs"),
            "deletion typo should still subsequence-match"
        );
    }

    #[test]
    fn ambiguous_basename_suggests_not_resolves_read() {
        let dir = fixture(&["src/a/mod.rs", "src/b/mod.rs"]);
        let res = resolve_fuzzy_path(dir.path(), "mod.rs", GateProfile::Read);
        match res {
            FuzzyResolution::Suggestions(s) => {
                assert!(s.len() >= 2, "expected both mod.rs candidates: {s:?}");
                assert!(s.iter().any(|p| p.contains('a')) && s.iter().any(|p| p.contains('b')));
            }
            other => panic!("expected Suggestions for ambiguous basename, got {other:?}"),
        }
    }

    #[test]
    fn ambiguous_basename_suggests_not_resolves_search() {
        // A path-like query that is an equally-good subsequence of two files.
        let dir = fixture(&["pkg1/a/mod.rs", "pkg2/a/mod.rs"]);
        let res = resolve_fuzzy_path(dir.path(), "a/mod.rs", GateProfile::Search);
        // Two equally-good matches; even path-like, no single-winner margin.
        assert!(
            !matches!(res, FuzzyResolution::Resolved(_)),
            "ambiguous candidates must not auto-open under Search, got {res:?}"
        );
        assert!(matches!(res, FuzzyResolution::Suggestions(_)));
    }

    #[test]
    fn suggestions_capped_at_k() {
        // Five equally-good `x.rs` matches must collapse to exactly SUGGESTION_K.
        let dir = fixture(&["a/x.rs", "b/x.rs", "c/x.rs", "d/x.rs", "e/x.rs"]);
        let res = resolve_fuzzy_path(dir.path(), "x.rs", GateProfile::Read);
        match res {
            FuzzyResolution::Suggestions(s) => assert_eq!(
                s.len(),
                SUGGESTION_K,
                "ambiguous candidates must be capped at k={SUGGESTION_K}, got {s:?}"
            ),
            other => panic!("expected Suggestions for 5-way tie, got {other:?}"),
        }
    }

    #[test]
    fn garbage_path_returns_none() {
        let dir = fixture(&["src/search/symbol.rs", "src/lib.rs"]);
        let res = resolve_fuzzy_path(dir.path(), "zzqq/nonexistent_xyzzy.bin", GateProfile::Read);
        assert!(
            matches!(res, FuzzyResolution::None),
            "a non-subsequence garbage path must stay an unchanged NotFound"
        );
    }

    #[test]
    fn search_profile_refuses_non_path_like_query() {
        // `symbol` is a subsequence of src/search/symbol.rs but is NOT path-like
        // (no separator, no extension) — Search must suggest, never auto-open.
        let dir = fixture(&["src/search/symbol.rs"]);
        let res = resolve_fuzzy_path(dir.path(), "symbol", GateProfile::Search);
        assert!(
            !matches!(res, FuzzyResolution::Resolved(_)),
            "non-path-like query must not auto-open under Search"
        );
        assert!(
            matches!(res, FuzzyResolution::Suggestions(_)),
            "non-path-like query with a candidate should suggest"
        );
    }

    #[test]
    fn search_profile_resolves_path_like_single_winner() {
        let dir = fixture(&["src/search/symbol.rs", "src/lib.rs", "README.md"]);
        let res = resolve_fuzzy_path(dir.path(), "serch/symbol.rs", GateProfile::Search);
        assert_eq!(
            resolved_path(&res).as_deref(),
            Some("src/search/symbol.rs"),
            "path-like single-winner query should auto-open under Search"
        );
    }

    impl std::fmt::Debug for FuzzyResolution {
        fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            match self {
                FuzzyResolution::Resolved(h) => {
                    write!(f, "Resolved({}, score={})", h.path.display(), h.score)
                }
                FuzzyResolution::Suggestions(s) => write!(f, "Suggestions({s:?})"),
                FuzzyResolution::None => write!(f, "None"),
            }
        }
    }
}
