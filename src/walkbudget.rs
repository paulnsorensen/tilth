//! A ceiling on how many entries one walk may visit.
//!
//! A backstop for trees with no ignore file to rescue them. `base_walk_builder` prunes a fixed
//! `SKIP_DIRS` list and does not consult `.gitignore`, deliberately, so gitignored-but-locally-
//! relevant files stay findable. On a normal repo that is a good trade. On a tree with a huge
//! number of untracked files nobody thought to ignore, it means the walk covers the whole disk
//! and runs until the request timeout kills it.
//!
//! A timeout is the least informative way to learn any of that. It arrives after the full wait,
//! says only "too long", and discards whatever the walk had found. This trips first, keeps the
//! partial results, and says what consumed the budget.
//!
//! # Not silent
//!
//! A cap that truncates without saying so turns "I searched everything" into a lie, which is the
//! same defect class as a scope silently widening to the whole checkout. So the trip is recorded
//! and surfaced on the response, together with the directories that consumed the budget — the
//! note is the point, the early exit is just what makes it cheap.
//!
//! # Charged per walk, not per request
//!
//! One MCP tool call can run several walks: `symbol::search` runs its definition and usage
//! walks concurrently (`rayon::join`), `kind: "any"` layers content and callers walks on top of
//! that, and a batch of queries multiplies further. Each [`WalkWindow`] (opened by
//! [`begin_walk`], one per `run_walk` call) is charged against the full ceiling independently —
//! a request is not penalized for running many small walks instead of one big one, and a single
//! pathological walk still trips at the documented size regardless of how many other walks share
//! the request.
//!
//! [`report`] aggregates at the request level: it says whether *any* walk in the request
//! tripped and, if so, names the directories that consumed that walk's budget. It is not a sum
//! across walks.
//!
//! # Why a plain global rather than `cancel`'s published-token machinery
//!
//! `cancel` publishes a fresh `Arc` per request and takes care to give each walk the token of the
//! request that built it. That care buys a guarantee this does not need. `CancelToken` documents
//! that its flag "carries no data" and justifies `Relaxed` on exactly that basis; hanging a
//! counter off it would weaken a stated invariant in a module whose header opens by warning how
//! delicate it is.
//!
//! The MCP server handles requests serially, so [`reset`] at the top of a tool call and
//! [`report`] at the bottom bracket exactly one request's walks.
//!
//! An abandoned worker from a timed-out earlier request keeps walking after the next request has
//! taken over the budget. Each walk therefore captures a **generation** at its start (in its
//! [`WalkWindow`]) and its visits are discarded once that no longer matches.
//!
//! This was first written without the generation, on the reasoning that stale visits "can only
//! push the count up, so the failure mode is a spurious 'incomplete' note". That was wrong in the
//! dangerous direction, and is recorded because the reasoning is tempting: a stale visit used to
//! *return* `true`, which the caller turns into `WalkState::Quit`, so a stale worker did not
//! merely annotate the next request — it cut that request's own walk short and labelled complete
//! results INCOMPLETE. Worse, it was self-reinforcing: the request most likely to leave a worker
//! behind is one that timed out on a pathological tree, which is precisely the worker that then
//! burns the whole ceiling in seconds.
//!
//! # Why the state is a struct and not bare statics
//!
//! The counting lives on [`WalkBudget`] with one process-wide instance, rather than in
//! free-standing statics, so that tests can drive a private instance. Written with statics
//! first, the tests were **flaky**: they asserted exact counter values while the rest of the
//! suite ran real walks into the same global, so a run could report a trip that another test's
//! walk had caused. A test whose verdict depends on what else is running is worse than no test —
//! it teaches you to ignore it.
//!
//! Adapted from lack435/tilth walkbudget.rs (end-state at 81c3a72).

use std::collections::HashMap;
use std::fmt::Write as _;
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Mutex, PoisonError};

/// Entries one walk may visit before it is cut short.
///
/// Charged per walk (see module docs), not summed across the walks a single request may run —
/// each [`WalkWindow`] gets its own full ceiling. 500k is chosen to sit above one walk over a
/// real source tree and below the pathological ones: the Linux kernel is ~80k files and
/// Chromium ~400k, so neither trips a single walk.
///
/// Deliberately generous. This is a floor on the worst case, not a tuning knob: a ceiling low
/// enough to make a big tree's walk *fast* would truncate legitimate large searches, and a
/// truncated answer is worse than a slow one.
const DEFAULT_MAX_WALK: usize = 500_000;

/// Sample rate for the per-directory tally. Every visit bumps the counter; only every 64th takes
/// the map lock, which is enough to identify a directory holding a large share of the walk
/// without putting a mutex on the hot path.
const TALLY_EVERY: usize = 64;

/// `usize::MAX` reads as "no ceiling" throughout.
const UNLIMITED: usize = usize::MAX;

pub(crate) struct WalkBudget {
    /// The walk-local count captured at the moment some walk tripped, for [`WalkBudget::report`].
    /// Not a running total across concurrent walks — each [`WalkWindow`] counts independently.
    visited: AtomicUsize,
    tripped: AtomicBool,
    limit: AtomicUsize,
    /// Bumped by [`WalkBudget::start`]. Captured by [`WalkBudget::begin_walk`] into the returned
    /// [`WalkWindow`]; a window's visits are dropped once that no longer matches the live
    /// generation — see [`WalkBudget::note_walk_visit`].
    generation: AtomicUsize,
    tally: Mutex<Option<HashMap<String, usize>>>,
}

/// One walk's accounting window, opened by [`WalkBudget::begin_walk`]. Owns its own counter so
/// concurrent walks within a request are charged independently against the ceiling rather than
/// sharing one pool — see the module's "Charged per walk" section.
pub(crate) struct WalkWindow {
    generation: usize,
    visited: AtomicUsize,
}

impl WalkBudget {
    pub(crate) const fn new() -> Self {
        Self {
            visited: AtomicUsize::new(0),
            tripped: AtomicBool::new(false),
            limit: AtomicUsize::new(UNLIMITED),
            generation: AtomicUsize::new(0),
            tally: Mutex::new(None),
        }
    }

    /// Start a fresh budget at `limit`.
    pub(crate) fn start(&self, limit: usize) {
        self.visited.store(0, Ordering::Relaxed);
        self.tripped.store(false, Ordering::Relaxed);
        self.limit.store(limit, Ordering::Relaxed);
        self.generation.fetch_add(1, Ordering::Relaxed);
        *self.tally.lock().unwrap_or_else(PoisonError::into_inner) = Some(HashMap::new());
    }

    /// Open an accounting window for one walk. Captures the current generation so a walk
    /// abandoned by a timed-out earlier request is recognized as stale, and starts its own
    /// counter at zero so this walk gets the full ceiling regardless of what other walks in the
    /// same request have already visited.
    pub(crate) fn begin_walk(&self) -> WalkWindow {
        WalkWindow {
            generation: self.generation.load(Ordering::Relaxed),
            visited: AtomicUsize::new(0),
        }
    }

    /// Record one visited entry against `window`'s own counter. Returns `true` once that walk's
    /// ceiling is passed, meaning the caller should stop walking.
    ///
    /// `Relaxed` throughout for the same reason `CancelToken` uses it: nothing is published
    /// alongside these, every read drives a best-effort early exit, and a late read costs one
    /// more entry of walking.
    pub(crate) fn note_walk_visit(&self, window: &WalkWindow, path: &Path) -> bool {
        // A walk from an abandoned request must not spend a LIVE request's budget.
        //
        // The first version of this had no generation and its module doc claimed the cross-talk
        // "can only push the count up, so the failure mode is a spurious 'incomplete' note". That
        // was wrong, and wrong in the dangerous direction: a stale visit used to *return* `true`,
        // which the caller turns into `WalkState::Quit`. So an abandoned worker did not merely
        // annotate the next request — it cut that request's own walk short and mislabelled
        // correct, complete results as INCOMPLETE.
        //
        // The scenario is self-reinforcing: request N times out on a pathological tree (exactly
        // what this module exists for), its worker keeps walking and burns the whole ceiling in
        // seconds, and request N+1 — a narrow, cheap, correct search — is truncated almost
        // immediately.
        if window.generation != self.generation.load(Ordering::Relaxed) {
            return true; // stale walk: stop it, but charge it to nobody
        }
        let n = window.visited.fetch_add(1, Ordering::Relaxed) + 1;

        if n.is_multiple_of(TALLY_EVERY) {
            if let Some(key) = tally_key(path) {
                if let Ok(mut guard) = self.tally.lock() {
                    if let Some(map) = guard.as_mut() {
                        *map.entry(key).or_insert(0) += TALLY_EVERY;
                    }
                }
            }
        }

        if n > self.limit.load(Ordering::Relaxed) {
            self.tripped.store(true, Ordering::Relaxed);
            self.visited.fetch_max(n, Ordering::Relaxed);
            return true;
        }
        false
    }

    /// Did any walk in this request hit the ceiling, and if so what consumed it?
    ///
    /// `None` when every walk completed. `Some(note)` is caller-facing prose meant to be shown
    /// alongside whatever partial results were produced.
    pub(crate) fn report(&self) -> Option<String> {
        if !self.tripped.load(Ordering::Relaxed) {
            return None;
        }
        let visited = self.visited.load(Ordering::Relaxed);
        let cap = self.limit.load(Ordering::Relaxed);

        let mut heavy: Vec<(String, usize)> = self
            .tally
            .lock()
            .ok()
            .and_then(|g| g.clone())
            .unwrap_or_default()
            .into_iter()
            .collect();
        heavy.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
        heavy.truncate(3);

        let mut note = format!(
            "NOTE: a walk stopped after {visited} entries (limit {cap}), so these results \
             are INCOMPLETE."
        );
        if !heavy.is_empty() {
            note.push_str("\nMost of it went here:");
            for (dir, n) in &heavy {
                let _ = write!(note, "\n  ~{n} entries under {dir}");
            }
        }
        // "the directories above" only makes sense when there ARE directories above: the tally
        // samples every 64th visit, so a trip on a small ceiling can leave it empty.
        if heavy.is_empty() {
            note.push_str(
                "\nNarrow `scope`, or exclude generated directories. Raise with \
                 TILTH_MAX_WALK=<n>, or TILTH_MAX_WALK=none for no limit.\n\n",
            );
            return Some(note);
        }
        note.push_str(
            "\nNarrow `scope`, or exclude the directories above. Raise with \
             TILTH_MAX_WALK=<n>, or TILTH_MAX_WALK=none for no limit.\n\n",
        );
        Some(note)
    }
}

/// The directory a path is attributed to in the tally: its parent, trimmed to something a human
/// can act on. A full leaf directory would fragment the tally across thousands of siblings and
/// name none of them loudly enough to be useful.
fn tally_key(path: &Path) -> Option<String> {
    let parent = path.parent()?;
    let mut components: Vec<_> = parent
        .components()
        .filter_map(|c| c.as_os_str().to_str())
        .collect();
    // Keep the last few segments: enough to identify the offender, short enough to read.
    if components.len() > 4 {
        components = components.split_off(components.len() - 4);
    }
    Some(components.join("/"))
}

/// `TILTH_MAX_WALK` entries, or [`DEFAULT_MAX_WALK`]. `0` / `none` disables the ceiling.
fn limit_from_env() -> usize {
    match std::env::var("TILTH_MAX_WALK") {
        Ok(v) if v.trim().eq_ignore_ascii_case("none") => UNLIMITED,
        Ok(v) => match v.trim().parse::<usize>() {
            Ok(0) => UNLIMITED,
            Ok(n) => n,
            Err(_) => DEFAULT_MAX_WALK,
        },
        Err(_) => DEFAULT_MAX_WALK,
    }
}

static GLOBAL: WalkBudget = WalkBudget::new();

/// Start a fresh budget for the request about to run.
pub fn reset() {
    GLOBAL.start(limit_from_env());
}

/// Open an accounting window for one walk against the request's budget. Each `run_walk` call
/// opens its own window so concurrent walks are charged independently — see module docs.
pub(crate) fn begin_walk() -> WalkWindow {
    GLOBAL.begin_walk()
}

pub(crate) fn note_walk_visit(window: &WalkWindow, path: &Path) -> bool {
    GLOBAL.note_walk_visit(window, path)
}

#[must_use]
pub fn report() -> Option<String> {
    GLOBAL.report()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Every test drives its own [`WalkBudget`], never `GLOBAL`. See the module header: asserting
    /// on the shared instance made these tests depend on whatever else the suite was walking at
    /// the time, which is how they were first written and why they flaked.
    #[test]
    fn under_the_ceiling_nothing_is_reported() {
        let b = WalkBudget::new();
        b.start(100);
        let w = b.begin_walk();
        for i in 0..50 {
            assert!(!b.note_walk_visit(&w, Path::new(&format!("/a/b/f{i}.rs"))));
        }
        assert!(b.report().is_none(), "a completed walk must not warn");
    }

    /// The trip must SAY so. A cap that truncates silently reports "I searched everything" when
    /// it did not, which is the defect this module exists to avoid rather than commit.
    #[test]
    fn tripping_reports_and_names_the_heavy_directory() {
        let b = WalkBudget::new();
        b.start(200);
        let w = b.begin_walk();

        let mut stopped_at = None;
        for i in 0..400 {
            if b.note_walk_visit(&w, Path::new(&format!("/repo/Saved/Logs/f{i}.log"))) {
                stopped_at = Some(i);
                break;
            }
        }
        assert_eq!(
            stopped_at,
            Some(200),
            "must stop at the ceiling, not past it"
        );

        let note = b.report().expect("a truncated walk must report");
        assert!(note.contains("INCOMPLETE"), "must not be silent: {note}");
        assert!(note.contains("201"), "must say how far it got: {note}");
        assert!(
            note.contains("Saved"),
            "must name what consumed the budget: {note}"
        );
        assert!(
            note.contains("TILTH_MAX_WALK"),
            "must name the escape hatch: {note}"
        );
    }

    /// A walk that began under an earlier generation must neither spend the current request's
    /// budget nor mark its results incomplete.
    ///
    /// This is the abandoned-worker case: request N times out on a pathological tree, its worker
    /// keeps walking, request N+1 is a narrow correct search. Without the generation the stale
    /// visits both truncated N+1's walk and labelled its complete results INCOMPLETE.
    #[test]
    fn a_stale_walk_does_not_spend_the_current_budget() {
        let b = WalkBudget::new();
        b.start(1000);
        let stale = b.begin_walk();

        // The next request takes over.
        b.start(1000);
        let live = b.begin_walk();
        assert_ne!(stale.generation, live.generation);

        // The abandoned worker keeps going, far past the ceiling.
        for i in 0..5000 {
            assert!(
                b.note_walk_visit(&stale, Path::new(&format!("/old/f{i}.rs"))),
                "a stale walk must be told to stop immediately"
            );
        }
        assert!(
            b.report().is_none(),
            "a stale walk must not mark the live request's results incomplete"
        );

        // And the live request still has its full budget.
        for i in 0..1000 {
            assert!(
                !b.note_walk_visit(&live, Path::new(&format!("/new/f{i}.rs"))),
                "the live request's budget was spent by a stale walk"
            );
        }
        assert!(b.report().is_none());
    }

    /// The core of HIGH-1: a request may run several walks concurrently (definition + usage,
    /// content, callers, batched queries). Each gets its own full ceiling rather than sharing
    /// one pool, so a request whose walks *combined* exceed the limit must not trip when no
    /// single walk does.
    #[test]
    fn combined_walks_over_the_ceiling_do_not_trip_when_no_single_walk_does() {
        let b = WalkBudget::new();
        b.start(1000);
        let w1 = b.begin_walk();
        let w2 = b.begin_walk();
        let w3 = b.begin_walk();

        for i in 0..800 {
            assert!(!b.note_walk_visit(&w1, Path::new(&format!("/a/f{i}.rs"))));
        }
        for i in 0..800 {
            assert!(!b.note_walk_visit(&w2, Path::new(&format!("/b/f{i}.rs"))));
        }
        for i in 0..800 {
            assert!(!b.note_walk_visit(&w3, Path::new(&format!("/c/f{i}.rs"))));
        }

        // 2400 combined visits across three walks, each well under the 1000 ceiling on its own.
        assert!(
            b.report().is_none(),
            "no single walk exceeded the ceiling, so the request must not report a trip"
        );
    }

    /// A single walk that DOES exceed the ceiling still trips, even while other walks in the
    /// same request are running under their own windows.
    #[test]
    fn one_walk_over_the_ceiling_trips_even_with_others_under_it() {
        let b = WalkBudget::new();
        b.start(200);
        let heavy = b.begin_walk();
        let light = b.begin_walk();

        for i in 0..100 {
            assert!(!b.note_walk_visit(&light, Path::new(&format!("/light/f{i}.rs"))));
        }

        let mut tripped = false;
        for i in 0..400 {
            if b.note_walk_visit(&heavy, Path::new(&format!("/heavy/f{i}.rs"))) {
                tripped = true;
                break;
            }
        }
        assert!(tripped, "the heavy walk must trip on its own ceiling");
        assert!(b.report().is_some(), "the request must report the trip");
    }

    /// The tally samples every 64th visit, so a trip on a small ceiling can leave it empty. The
    /// advice must not then point at "the directories above", of which there are none.
    #[test]
    fn a_trip_with_no_tallied_directories_does_not_reference_a_missing_list() {
        let b = WalkBudget::new();
        b.start(5);
        let w = b.begin_walk();
        for i in 0..20 {
            if b.note_walk_visit(&w, Path::new(&format!("/a/f{i}.rs"))) {
                break;
            }
        }
        let note = b.report().expect("must report");
        assert!(!note.contains("Most of it went here"), "no tally: {note}");
        assert!(
            !note.contains("directories above"),
            "advice references a list that is not there: {note}"
        );
        assert!(
            note.contains("TILTH_MAX_WALK"),
            "escape hatch still named: {note}"
        );
    }

    #[test]
    fn an_unlimited_budget_never_trips() {
        let b = WalkBudget::new();
        b.start(UNLIMITED);
        let w = b.begin_walk();
        for i in 0..1000 {
            assert!(!b.note_walk_visit(&w, Path::new(&format!("/a/f{i}.rs"))));
        }
        assert!(b.report().is_none());
    }

    #[test]
    fn start_clears_a_previous_trip() {
        let b = WalkBudget::new();
        b.start(10);
        let w = b.begin_walk();
        for i in 0..50 {
            if b.note_walk_visit(&w, Path::new(&format!("/a/f{i}.rs"))) {
                break;
            }
        }
        assert!(b.report().is_some());
        b.start(10);
        assert!(
            b.report().is_none(),
            "a new request must not inherit the previous one's trip"
        );
    }

    /// The env parsing, which is the only part that still reads process state. Serialized
    /// against itself; it touches no counter, so it cannot disturb a concurrent walk.
    #[test]
    fn env_limit_parsing() {
        static ENV_LOCK: Mutex<()> = Mutex::new(());
        let _g = ENV_LOCK.lock().unwrap_or_else(PoisonError::into_inner);

        std::env::remove_var("TILTH_MAX_WALK");
        assert_eq!(limit_from_env(), DEFAULT_MAX_WALK);

        std::env::set_var("TILTH_MAX_WALK", "none");
        assert_eq!(limit_from_env(), UNLIMITED, "`none` disables the ceiling");

        std::env::set_var("TILTH_MAX_WALK", "0");
        assert_eq!(limit_from_env(), UNLIMITED, "`0` disables the ceiling");

        std::env::set_var("TILTH_MAX_WALK", "1234");
        assert_eq!(limit_from_env(), 1234);

        std::env::set_var("TILTH_MAX_WALK", "not-a-number");
        assert_eq!(
            limit_from_env(),
            DEFAULT_MAX_WALK,
            "an unparseable value falls back rather than disabling the ceiling"
        );

        std::env::remove_var("TILTH_MAX_WALK");
    }
}
