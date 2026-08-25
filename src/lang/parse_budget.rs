//! A byte ceiling on tree-sitter trees held concurrently by parallel walks.
//!
//! Adapted from lack435/tilth's `parse_budget.rs` (end-state at 81c3a72). Ported for tilth
//! (this fork): our walk sites build their own `tree_sitter::Parser` inline rather than going
//! through a shared `parse_masked`, so `parse_budgeted` here owns the parse call directly and
//! takes no `Lang` (nothing in this fork's parse path branches on it).
//!
//! ## The term this bounds
//!
//! The definition/usage walks and the caller/callee/sibling walks parse a file per walk thread
//! and hold the resulting tree while they read it. A tree is a large multiple of its file's
//! bytes, so peak RSS carries a `walk_threads x tree_size` term that no existing cap touches.
//!
//! ## Why a reservation, and why sized per file
//!
//! A plain semaphore over concurrent parses cannot work. Tree sizes in one repository span
//! three orders of magnitude, so a permit count set to make the worst case fit throttles the
//! common case to a standstill, and one set for the common case does not bound the worst.
//! Admission has to be sized, which means estimating the tree before parsing it.
//!
//! ## The estimator: source bytes, not lines
//!
//! Bytes are the predictor whose bound holds by construction: tree size tracks node count,
//! node count is bounded by token count, and token count is bounded by bytes. A per-line
//! estimator does not have this property — nodes-per-line is bytes-per-line times
//! nodes-per-byte, and bytes-per-line is unbounded, so a single long-line file (e.g. a
//! minified bundle behind a preserved license banner) can under-charge by orders of magnitude
//! and leave the budget entirely inert on exactly the input it exists to bound. See the
//! upstream module (donor commits `ca6e3db`, `c8f0d7e`, `b43e07e`) for the full measurement
//! trail that moved the estimator from lines to bytes.
//!
//! **Over-estimating is the safe direction**: it costs parallelism, never correctness, because
//! a rejected parse waits rather than being skipped.
//!
//! ## What reserves, and what deliberately does not
//!
//! Seven sites reserve here: `search::symbol::find_defs_treesitter`, `search::callers`,
//! `search::callees`, `search::siblings`, `lang::outline::get_outline_entries`,
//! `diff::matching::compute_structural_hash` (the diff pair is worth naming because it runs
//! under `par_iter` while calling `get_outline_entries` twice per changed file — parallel
//! without being a file walk), and `edit::parse_check::check`, via [`try_parse_budgeted`] rather
//! than [`parse_budgeted`] — it runs synchronously on the `tilth_write` response path, not a
//! walk thread, so it must never block waiting for space. It also gates on
//! [`MAX_PARSE_FILE_SIZE`] itself before parsing at all, since it is advisory (a post-write
//! error surface) and skipping it silently is a fine outcome; the write it decorates always
//! completes independent of whether the check ran.
//!
//! `cache::OutlineCache::get_or_parse` deliberately does not reserve: it retains its tree in
//! the cache beyond the call that produced it (bounded by an LRU entry cap in this fork, not by
//! the process lifetime), so a permit held alongside it would outlive the parse that acquired
//! it and could fill the budget without a bounded release point.

use std::ops::Deref;
use std::sync::{Condvar, Mutex, OnceLock};

/// Estimated tree bytes per source byte. Upper bound, not a typical value — the ceiling holds
/// only where `estimate >= actual`. Measured upstream (donor commit `b43e07e`) across this
/// repository, a large external C++ tree, and adversarial shapes: 92.5 B per source byte was
/// the maximum found; 128 carries ~1.4x margin.
const TREE_BYTES_PER_SOURCE_BYTE: usize = 128;

/// Maximum size of a file any parsing walk will read before skipping it.
///
/// Shared by every AST gate that used to be a scattered `500_000` literal: the symbol walks
/// (`search::mod::MAX_SEARCH_FILE_SIZE`), the caller/callee/deps bloom walk
/// (`search::bloom_walk::MAX_FILE_SIZE`), the outline parse cache (`cache::get_or_parse`), and
/// the in-result outline context (`search::mod::get_outline_str`).
///
/// Raised from 500 000 to 1 MB (donor commit `8b27065`) so hand-written large source files stay
/// searchable — real code sits above the old gate (e.g. Unreal's `CharacterMovementComponent.cpp`
/// at 536 KB), while nearly everything past 1 MB is generated tables or vendored dumps where a
/// parse is pure cost.
///
/// Coupled to `DEFAULT_BUDGET_MB`: the worst per-file tree estimate is
/// `MAX_PARSE_FILE_SIZE x TREE_BYTES_PER_SOURCE_BYTE`. See there for how the budget responds
/// when this moves.
pub(crate) const MAX_PARSE_FILE_SIZE: u64 = 1_000_000;

/// Default ceiling on concurrently-held tree bytes, in MB.
///
/// Deliberately left under the derivation ("Option B", donor commit `8b27065`): at the 1 MB
/// gate the worst per-file estimate is `1_000_000 x 128 = 128 MB`, so a ceiling that always
/// admits every thread's worst case would be `768` at six threads. Left at `384` instead —
/// `reserve` always admits when nothing is in flight, so no file is ever skipped for want of
/// budget; a walk that parses more than ~3 near-gate-size files at once serialises the excess
/// instead of raising peak RSS. Override with `TILTH_PARSE_BUDGET_MB`; `0` disables accounting.
const DEFAULT_BUDGET_MB: usize = 384;

/// Process-wide budget. One instance, because the thing being bounded is process peak RSS —
/// a per-search budget would be wrong wherever two walks can run concurrently under `rayon`.
pub struct ParseBudget {
    /// `0` means unbounded — see `DEFAULT_BUDGET_MB`.
    ceiling: usize,
    /// Sum of the estimates of every parse currently admitted.
    in_flight: Mutex<usize>,
    space: Condvar,
}

impl ParseBudget {
    fn from_env() -> Self {
        let ceiling = std::env::var("TILTH_PARSE_BUDGET_MB")
            .ok()
            .and_then(|v| v.parse::<usize>().ok())
            .unwrap_or(DEFAULT_BUDGET_MB)
            .saturating_mul(1024 * 1024);
        Self {
            ceiling,
            in_flight: Mutex::new(0),
            space: Condvar::new(),
        }
    }

    #[cfg(test)]
    fn with_ceiling(bytes: usize) -> Self {
        Self {
            ceiling: bytes,
            in_flight: Mutex::new(0),
            space: Condvar::new(),
        }
    }

    /// Reserve `estimate` bytes, waiting until they fit.
    ///
    /// **Always admits when nothing else is in flight, however large the estimate.** That is
    /// the deadlock-freedom property: a single file whose estimate exceeds the whole ceiling
    /// would otherwise wait for space that only it could release. Every admitted reservation is
    /// released by `Permit::drop`, including on panic, so `in_flight` always returns to zero and
    /// some waiter always makes progress. The consequence is that the ceiling is soft by up to
    /// one file's tree — bounding it harder would mean refusing to parse a file, which changes
    /// the answer, and admission must never do that.
    fn reserve(&self, estimate: usize) -> Permit<'_> {
        if self.ceiling == 0 {
            return Permit {
                budget: self,
                estimate: 0,
            };
        }
        let mut in_flight = self
            .in_flight
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        // `*in_flight > 0` is the deadlock-freedom guard described above, not an optimisation.
        while *in_flight > 0 && *in_flight + estimate > self.ceiling {
            in_flight = self
                .space
                .wait(in_flight)
                .unwrap_or_else(std::sync::PoisonError::into_inner);
        }
        *in_flight += estimate;
        Permit {
            budget: self,
            estimate,
        }
    }

    /// Reserve `estimate` bytes without waiting: `None` if they do not currently fit.
    ///
    /// For callers that must never block — `edit::parse_check::check` runs synchronously on
    /// the `tilth_write` response path, not a walk thread, so it skips its (advisory) check
    /// rather than stalling a write behind unrelated walks' reservations. Still deadlock-free by
    /// the same `*in_flight > 0` guard as `reserve`: it always admits when nothing else is in
    /// flight.
    fn try_reserve(&self, estimate: usize) -> Option<Permit<'_>> {
        if self.ceiling == 0 {
            return Some(Permit {
                budget: self,
                estimate: 0,
            });
        }
        let mut in_flight = self
            .in_flight
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if *in_flight > 0 && *in_flight + estimate > self.ceiling {
            return None;
        }
        *in_flight += estimate;
        Some(Permit {
            budget: self,
            estimate,
        })
    }

    /// Estimated bytes currently reserved. Report-only; nothing branches on it outside `reserve`.
    #[cfg(test)]
    fn in_flight(&self) -> usize {
        *self
            .in_flight
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }
}

/// A reservation, released on drop.
struct Permit<'a> {
    budget: &'a ParseBudget,
    estimate: usize,
}

impl Drop for Permit<'_> {
    fn drop(&mut self) {
        if self.estimate == 0 {
            return;
        }
        let mut in_flight = self
            .budget
            .in_flight
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        *in_flight = in_flight.saturating_sub(self.estimate);
        // `notify_all`, not `notify_one`: waiters are waiting for *different* amounts, so the
        // one woken might not fit while another would. Every waiter re-tests its own condition.
        drop(in_flight);
        self.budget.space.notify_all();
    }
}

/// A parsed tree that holds its budget reservation for exactly as long as the tree lives.
///
/// The reservation has to outlive the tree, not the parse: releasing when `parse` returns would
/// leave each walk thread free to hold a tree with nothing accounting for it. Field order is
/// load-bearing: struct fields drop in declaration order, so `tree` is freed before `_permit`
/// un-charges it.
pub struct BudgetedTree {
    tree: tree_sitter::Tree,
    /// `'static` because the only budget a parse can reserve against is the process-wide one,
    /// which keeps this lifetime out of the public signature.
    _permit: Permit<'static>,
}

impl Deref for BudgetedTree {
    type Target = tree_sitter::Tree;
    fn deref(&self) -> &Self::Target {
        &self.tree
    }
}

fn global() -> &'static ParseBudget {
    static BUDGET: OnceLock<ParseBudget> = OnceLock::new();
    BUDGET.get_or_init(ParseBudget::from_env)
}

/// Estimated tree bytes for `content`. Free: the length is already known, so unlike a
/// line-counting estimator there is no scan of the file at all on the walk's hot path.
/// `max(1)` keeps an empty file from reserving zero and slipping the accounting.
fn estimate_bytes(content: &str) -> usize {
    content
        .len()
        .max(1)
        .saturating_mul(TREE_BYTES_PER_SOURCE_BYTE)
}

/// Parse `content` with `ts_lang`, holding a budget reservation for the tree's lifetime.
///
/// For walk-time parses whose tree is transient. See the module header for what is
/// deliberately not routed through here, and why. Returns `None` when the grammar fails to
/// load or the parse itself fails, exactly as a bare `Parser::parse` call would.
pub fn parse_budgeted(content: &str, ts_lang: &tree_sitter::Language) -> Option<BudgetedTree> {
    let permit = global().reserve(estimate_bytes(content));
    let mut parser = tree_sitter::Parser::new();
    parser.set_language(ts_lang).ok()?;
    let tree = parser.parse(content, None)?;
    Some(BudgetedTree {
        tree,
        _permit: permit,
    })
}

/// Non-blocking counterpart to [`parse_budgeted`]: `None` if the reservation does not currently
/// fit, in addition to the grammar/parse failure cases `parse_budgeted` already returns `None`
/// for. See [`ParseBudget::try_reserve`] for who needs this and why.
pub(crate) fn try_parse_budgeted(
    content: &str,
    ts_lang: &tree_sitter::Language,
) -> Option<BudgetedTree> {
    let permit = global().try_reserve(estimate_bytes(content))?;
    let mut parser = tree_sitter::Parser::new();
    parser.set_language(ts_lang).ok()?;
    let tree = parser.parse(content, None)?;
    Some(BudgetedTree {
        tree,
        _permit: permit,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;

    /// A reservation is charged while held and returned on drop.
    #[test]
    fn a_permit_charges_the_budget_and_releases_it() {
        let b = ParseBudget::with_ceiling(1000);
        assert_eq!(b.in_flight(), 0);
        {
            let _p = b.reserve(400);
            assert_eq!(b.in_flight(), 400);
            {
                let _q = b.reserve(500);
                assert_eq!(b.in_flight(), 900);
            }
            assert_eq!(b.in_flight(), 400);
        }
        assert_eq!(b.in_flight(), 0);
    }

    /// A file whose estimate exceeds the entire ceiling must still parse. Without the
    /// `in_flight > 0` guard in `reserve` this test blocks forever rather than failing.
    #[test]
    fn an_estimate_larger_than_the_ceiling_is_still_admitted() {
        let b = ParseBudget::with_ceiling(1024);
        let p = b.reserve(64 * 1024 * 1024);
        assert_eq!(b.in_flight(), 64 * 1024 * 1024);
        drop(p);
        assert_eq!(b.in_flight(), 0);
    }

    /// `0` means unbounded, and costs nothing while it is.
    #[test]
    fn a_zero_ceiling_disables_accounting() {
        let b = ParseBudget::with_ceiling(0);
        let _p = b.reserve(usize::MAX);
        assert_eq!(b.in_flight(), 0, "a disabled budget must not accumulate");
    }

    /// A reservation that does not fit waits, and proceeds only once space is returned —
    /// asserts the ordering, not just the outcome.
    #[test]
    fn a_reservation_waits_for_space_and_then_proceeds() {
        let b = Arc::new(ParseBudget::with_ceiling(1000));
        let order = Arc::new(AtomicUsize::new(0));

        let held = b.reserve(800);
        assert_eq!(b.in_flight(), 800);

        let (b2, order2) = (Arc::clone(&b), Arc::clone(&order));
        let waiter = std::thread::spawn(move || {
            // 800 + 400 > 1000, and something is in flight, so this must block.
            let _p = b2.reserve(400);
            order2.fetch_add(2, Ordering::SeqCst)
        });

        std::thread::sleep(std::time::Duration::from_millis(150));
        assert_eq!(
            order.load(Ordering::SeqCst),
            0,
            "reservation was admitted while the budget was full"
        );

        order.fetch_add(1, Ordering::SeqCst);
        drop(held);
        let seen_before = waiter.join().expect("waiter panicked");
        assert_eq!(
            seen_before, 1,
            "waiter proceeded before the holder released its reservation"
        );
        assert_eq!(b.in_flight(), 0, "budget did not return to zero");
    }

    /// Concurrent reserve/release cannot desynchronise the counter from reality: after a storm
    /// of overlapping permits, the budget must return exactly to zero.
    #[test]
    fn concurrent_reservations_return_the_budget_to_zero() {
        let b = Arc::new(ParseBudget::with_ceiling(1 << 20));
        let mut handles = Vec::new();
        for t in 0..8 {
            let b = Arc::clone(&b);
            handles.push(std::thread::spawn(move || {
                for i in 0..200 {
                    let _p = b.reserve(1 + (t * 31 + i * 17) % 4096);
                }
            }));
        }
        for h in handles {
            h.join().expect("thread panicked");
        }
        assert_eq!(b.in_flight(), 0);
    }

    /// The estimate is a function of length, and of nothing else — a per-line predictor
    /// estimates two same-length files differently, which is how it under-charges a long-line
    /// file. Same length must mean same estimate, whatever the shape.
    #[test]
    fn the_estimate_tracks_length_and_not_shape() {
        let dense = "a\n".repeat(500); // 1000 bytes, 500 lines
        let one_line = "a".repeat(1000); // 1000 bytes, 1 line
        assert_eq!(dense.len(), one_line.len());
        assert_eq!(
            estimate_bytes(&dense),
            estimate_bytes(&one_line),
            "estimate still depends on line structure, so a long-line file under-charges"
        );
        assert_eq!(estimate_bytes(&dense), 1000 * TREE_BYTES_PER_SOURCE_BYTE);
    }

    /// The worst single-file estimate is the unit the ceiling is measured in, so pin the
    /// arithmetic. If `MAX_PARSE_FILE_SIZE` or `TREE_BYTES_PER_SOURCE_BYTE` drift, re-derive.
    #[test]
    fn the_worst_single_file_estimate_bounds_the_ceiling() {
        let worst = MAX_PARSE_FILE_SIZE as usize * TREE_BYTES_PER_SOURCE_BYTE;
        assert_eq!(worst, 128_000_000, "worst per-file estimate moved");
        assert!(
            DEFAULT_BUDGET_MB * 1024 * 1024 >= 2 * worst,
            "the default ceiling ({DEFAULT_BUDGET_MB} MB) no longer admits two worst-case parses \
             ({} B), so large-file searches parse strictly serially",
            2 * worst
        );
    }

    /// An empty file still reserves something, so nothing slips the accounting at zero.
    #[test]
    fn an_empty_file_still_reserves() {
        assert_eq!(estimate_bytes(""), TREE_BYTES_PER_SOURCE_BYTE);
    }

    /// `try_reserve` never waits: it returns `None` immediately instead of blocking for space,
    /// unlike `reserve`.
    #[test]
    fn try_reserve_declines_instead_of_waiting() {
        let b = ParseBudget::with_ceiling(1000);
        let _held = b.reserve(800);
        assert_eq!(b.in_flight(), 800);

        assert!(
            b.try_reserve(400).is_none(),
            "800 + 400 > 1000 with something in flight must decline, not wait"
        );
        assert_eq!(
            b.in_flight(),
            800,
            "a declined try_reserve must not charge anything"
        );
    }

    /// Like `reserve`, `try_reserve` still always admits when nothing else is in flight, however
    /// large the estimate — the same deadlock-freedom property, just without waiting.
    #[test]
    fn try_reserve_admits_when_nothing_in_flight_however_large() {
        let b = ParseBudget::with_ceiling(1024);
        let p = b.try_reserve(64 * 1024 * 1024);
        assert!(p.is_some());
        assert_eq!(b.in_flight(), 64 * 1024 * 1024);
    }
}
