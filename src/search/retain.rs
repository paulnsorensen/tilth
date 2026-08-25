//! Bounded, deterministic retention for symbol- and content-search walks.
//!
//! Adapted from lack435/tilth's `retain.rs` (end-state at commit `81c3a72`, never merged —
//! ported by re-implementing against this fork's `Match`/`rank` types). The donor project
//! measured a dense-match fixture (2.4M matches) driving symbol search to 1154MB peak RSS
//! under an *unbounded* retained `Vec<Match>`.
//!
//! This fork never had that unbounded path: `symbol.rs` and `content.rs` capped memory with
//! a global atomic counter checked per file callback (`found_count.load() >= threshold` →
//! `WalkState::Quit`). That is exactly the class of "count-based early quit" the donor's
//! header describes removing (their #18) and warns against: a shared counter read once per
//! file callback in a *parallel* walk makes which files get scanned before the quit fires
//! depend on thread scheduling. Two runs over an identical tree can quit after different
//! files, so on a dense-match tree the set of retained definitions (and the reported total)
//! is not stable run to run, and a real definition can be dropped in favor of one that
//! merely happened to be found first. The fix is the same shape as upstream's: never quit
//! the walk, and bound what is *retained* by each candidate's own rank score instead.
//!
//! Two properties every caller needs:
//!
//! * **Determinism.** Selection uses `rank::Scorer::selection_score`, which omits recency —
//!   the only scoring term that depends on *when* a candidate was scored during the walk
//!   rather than on the candidate itself. The bound decides using only a candidate's own key,
//!   never a shared counter, so retention cannot depend on walk scheduling.
//! * **Not serialising the walk.** Each file's matches reduce to a local top-`cap` heap
//!   off-lock, then merge into the shared heap under one acquisition. Evicted candidates are
//!   dropped after the lock releases.
//!
//! **Adapted, not ported whole.** The donor module also carries `FileOffer`/`OFFER_CHUNK`
//! streaming (so no walk thread holds a whole file's `Vec<Match>` at once) and a
//! multi-target `BoundedRetainSet`. Neither applies here: `MAX_SEARCH_FILE_SIZE` already caps
//! a single file at 500KB, so one file's `Vec<Match>` is bounded on its own, and every
//! multi-symbol query in this fork (`search_multi_symbol_expanded`) calls `symbol::search`
//! once per target, so each call already gets its own pair of sinks without a shared bucket
//! type.

use std::cmp::Ordering;
use std::collections::BinaryHeap;
use std::sync::atomic::{AtomicUsize, Ordering as AtomicOrdering};
use std::sync::Mutex;

use crate::search::rank::Scorer;
use crate::types::Match;

/// Retention ceiling per sink (one definitions sink and one usages sink per `symbol::search`
/// call; one sink per `content::search` call).
///
/// Set far above the display cap (`symbol::FULL_MAX_MATCHES` = 100) so that recency — the
/// term `selection_score` omits — can still promote a match onto the final page from within
/// the retained set. Recency is worth up to 100 points (`rank::recency`); `MAX_RETAINED` only
/// has to be deep enough that not every one of 100 points' worth of candidates piles up above
/// the cut, which a few thousand retained candidates comfortably covers for this fork's scale.
pub(crate) const MAX_RETAINED: usize = 2_000;

/// A match paired with the score that decides whether it survives.
///
/// `Ord` is **inverted** — a lower selection score compares *greater* — so a `BinaryHeap`
/// (a max-heap) has the worst retained candidate at its root and can evict in O(log n).
///
/// Tie-breaks mirror `rank::sort`'s key exactly (score desc, path asc, line asc) and are
/// **not** inverted: `rank::sort` orders path/line ascending, so among equal scores the match
/// that sorts last (largest path/line) is the one to evict, which means it must compare
/// greatest — the same direction `rank::sort` runs in.
struct Candidate {
    score: i32,
    m: Match,
}

impl PartialEq for Candidate {
    fn eq(&self, other: &Self) -> bool {
        self.cmp(other) == Ordering::Equal
    }
}
impl Eq for Candidate {}

impl Ord for Candidate {
    fn cmp(&self, other: &Self) -> Ordering {
        other
            .score
            .cmp(&self.score)
            .then_with(|| self.m.path.cmp(&other.m.path))
            .then_with(|| self.m.line.cmp(&other.m.line))
    }
}

impl PartialOrd for Candidate {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

/// Shared bounded sink for a parallel walk.
pub(crate) struct BoundedRetain {
    heap: Mutex<BinaryHeap<Candidate>>,
    cap: usize,
    /// Exact count of matches offered, whether retained or evicted — what `total_found` and
    /// per-kind counts are built from, so a bounded search never under-reports its total.
    offered: AtomicUsize,
}

impl BoundedRetain {
    pub(crate) fn new(cap: usize) -> Self {
        Self {
            heap: Mutex::new(BinaryHeap::new()),
            cap,
            offered: AtomicUsize::new(0),
        }
    }

    /// Offer one file's matches. Scores off-lock via `scorer`, then merges under one
    /// acquisition — the walk never holds the shared lock across a whole file's worth of
    /// comparisons.
    pub(crate) fn offer_file(&self, mut file_matches: Vec<Match>, scorer: &mut Scorer<'_>) {
        if file_matches.is_empty() {
            return;
        }
        self.offered
            .fetch_add(file_matches.len(), AtomicOrdering::Relaxed);

        let scored: Vec<Candidate> = file_matches
            .drain(..)
            .map(|m| Candidate {
                score: scorer.selection_score(&m),
                m,
            })
            .collect();

        let local: Vec<Candidate> = if scored.len() <= self.cap {
            scored
        } else {
            let mut heap: BinaryHeap<Candidate> = BinaryHeap::with_capacity(self.cap + 1);
            for cand in scored {
                if heap.len() < self.cap {
                    heap.push(cand);
                } else if heap.peek().is_some_and(|worst| cand < *worst) {
                    heap.pop();
                    heap.push(cand);
                }
            }
            heap.into_vec()
        };

        let mut evicted: Vec<Candidate> = Vec::new();
        {
            let mut heap = self
                .heap
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            for cand in local {
                if heap.len() < self.cap {
                    heap.push(cand);
                } else if heap.peek().is_some_and(|worst| cand < *worst) {
                    if let Some(out) = heap.pop() {
                        evicted.push(out);
                    }
                    heap.push(cand);
                } else {
                    evicted.push(cand);
                }
            }
        }
        // Freed after the guard drops, not under it.
        drop(evicted);
    }

    /// Exact number of matches offered, independent of the cap.
    pub(crate) fn offered(&self) -> usize {
        self.offered.load(AtomicOrdering::Relaxed)
    }

    /// Consume the sink and return the retained matches. Order is unspecified — every caller
    /// re-sorts through `rank::sort`.
    pub(crate) fn into_matches(self) -> Vec<Match> {
        self.heap
            .into_inner()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .into_vec()
            .into_iter()
            .map(|c| c.m)
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::{Path, PathBuf};
    use std::time::SystemTime;

    fn m(path: &str, line: u32) -> Match {
        Match {
            path: PathBuf::from(path),
            line,
            text: format!("hit at {line}"),
            is_definition: false,
            exact: true,
            file_lines: 100,
            mtime: SystemTime::UNIX_EPOCH,
            def_range: None,
            def_name: None,
            def_weight: 0,
            impl_target: None,
        }
    }

    type MatchKey = (PathBuf, u32, String);

    fn key_of(v: Vec<Match>) -> Vec<MatchKey> {
        let mut k: Vec<_> = v.into_iter().map(|x| (x.path, x.line, x.text)).collect();
        k.sort();
        k
    }

    fn scorer(scope: &Path) -> Scorer<'_> {
        Scorer::new("hit", scope, None)
    }

    /// The whole point: retention is capped however many matches arrive, across many files.
    #[test]
    fn retention_is_bounded_by_the_cap() {
        let scope = Path::new(".");
        let sink = BoundedRetain::new(10);
        let mut sc = scorer(scope);
        for f in 0..20 {
            let batch: Vec<Match> = (0..500).map(|i| m(&format!("f{f}.rs"), i)).collect();
            sink.offer_file(batch, &mut sc);
        }
        assert_eq!(sink.into_matches().len(), 10);
    }

    /// Feeding the same files in a different order must retain the same set — the property
    /// that lets a parallel walk use this at all.
    #[test]
    fn retained_set_does_not_depend_on_arrival_order() {
        let scope = Path::new(".");
        let files: Vec<Vec<Match>> = (0..8)
            .map(|f| (0..50).map(|i| m(&format!("f{f}.rs"), i)).collect())
            .collect();

        let forward = BoundedRetain::new(37);
        let mut sc = scorer(scope);
        for batch in files.clone() {
            forward.offer_file(batch, &mut sc);
        }

        let reverse = BoundedRetain::new(37);
        let mut sc = scorer(scope);
        for batch in files.into_iter().rev() {
            reverse.offer_file(batch, &mut sc);
        }

        assert_eq!(
            key_of(forward.into_matches()),
            key_of(reverse.into_matches()),
            "retained set depends on arrival order, so a parallel walk would vary run to run"
        );
    }

    /// The heap must keep the *best* candidates, not the worst.
    #[test]
    fn the_best_candidates_survive_not_the_worst() {
        let scope = Path::new("src");
        let sink = BoundedRetain::new(3);
        let mut sc = scorer(scope);

        let mut batch = vec![
            m("src/near.rs", 1),
            m("src/near.rs", 2),
            m("src/near.rs", 3),
        ];
        batch.extend((0..20).map(|i| m("src/a/b/c/d/e/far.rs", i)));
        let mut scores: Vec<i32> = batch.iter().map(|x| sc.selection_score(x)).collect();
        scores.sort_unstable();
        assert_ne!(
            scores.first(),
            scores.last(),
            "fixture does not discriminate; it cannot test selection"
        );

        sink.offer_file(batch, &mut sc);
        let kept = sink.into_matches();
        assert_eq!(kept.len(), 3);
        assert!(
            kept.iter().all(|k| k.path.ends_with("near.rs")),
            "kept the low-scoring candidates: {:?}",
            kept.iter().map(|k| k.path.clone()).collect::<Vec<_>>()
        );
    }

    #[test]
    fn empty_offer_is_a_no_op() {
        let scope = Path::new(".");
        let sink = BoundedRetain::new(5);
        let mut sc = scorer(scope);
        sink.offer_file(Vec::new(), &mut sc);
        assert!(sink.into_matches().is_empty());
    }

    /// Exact offered count is unaffected by how many candidates the cap evicts.
    #[test]
    fn offered_count_is_exact_even_when_most_are_evicted() {
        let scope = Path::new(".");
        let sink = BoundedRetain::new(5);
        let mut sc = scorer(scope);
        for f in 0..40 {
            let batch: Vec<Match> = (0..50).map(|i| m(&format!("f{f:03}.rs"), i)).collect();
            sink.offer_file(batch, &mut sc);
        }
        assert_eq!(sink.offered(), 40 * 50);
        assert_eq!(sink.into_matches().len(), 5);
    }
}
