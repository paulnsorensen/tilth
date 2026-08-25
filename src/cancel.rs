//! Cooperative cancellation for the workers `timeout.rs` abandons.
//!
//! Rust cannot forcibly kill a thread, so `spawn_with_timeout` stops *waiting* on expiry and
//! detaches the worker. The worker then keeps walking, parsing and allocating with nothing
//! consuming its result, and `MAX_ABANDONED_THREADS` permits eight of those at once. This
//! module gives the walk a flag to notice with.
//!
//! # The property that makes this safe
//!
//! A per-file flag check is exactly the shape a naive read-time check would be nondeterministic
//! about, so the reason this one is different has to be stated rather than assumed:
//!
//! > A token is set to cancelled **only** inside the arm that has already won
//! > `ThreadCoord::claim_timeout`, and winning that CAS is what guarantees the worker's result will
//! > be discarded.
//!
//! So a cancelled walk's *own* result is never returned — the flag cannot change that answer
//! because there is no answer left to change. It is emphatically not a general bound on the walk.
//! `only_the_expired_request_is_cancelled` in `timeout.rs` pins both halves — that an expired
//! worker sees the cancel, and that the next request does not — and it is the test to keep if any
//! of the rest is rewritten.
//!
//! **That is not the whole story.** A cancelled worker is not killed: it finishes ranking and
//! rendering whatever it retained, and rendering *writes to state the request outlives* —
//! `Session::record_expand` decides whether a later request prints a definition body or
//! `[shown earlier]`, and `record_savings` feeds session savings accounting. A walk stopped
//! mid-flight records less than a walk that ran to completion, so without a second guard "how much
//! got recorded" would become a function of when the deadline landed, and that *would* reach a
//! returned answer. [`worker_request_cancelled`] is that guard, and it is why this module has two
//! mechanisms rather than one.
//!
//! # Why the token is passed by identity through a global rather than by argument
//!
//! The walk is built deep inside `search`, so the token has to reach `base_walk_builder` somehow.
//! Three ways were available and two of them are wrong here:
//!
//! * **Threading a `&CancelToken` through every search signature** is airtight and was rejected on
//!   churn alone: eight functions build walks, and the parameter would have to travel from
//!   `dispatch_tool` through every caller between.
//! * **A `thread_local` set on the worker thread** does not work: `symbol::search` builds its
//!   walkers inside `rayon::join`, which may run either closure on a stolen thread. A walker built
//!   on a stolen thread would silently see no token.
//! * **A global epoch counter** ("cancel every generation at or below N") needs no allocation, but
//!   a later request's expiry cancels every walk an earlier request ever built, live or not.
//!
//! What is here instead: a fresh `Arc<AtomicBool>` per request, published in [`CURRENT`] while that
//! request is in flight, and captured **by identity** into each walk as it is built.
//!
//! # What the identity actually buys, and what it does not
//!
//! Worth stating precisely, because the obvious reading is stronger than the truth. Capture is by
//! identity, but *which* token a builder captures still comes from a global slot, so:
//!
//! * A walk built while its own request was the published one can be cancelled **only** by that
//!   request. That covers every walk the server actually builds.
//! * A walk built while *another* request is published would capture that other request's token —
//!   and could then be stopped by a timeout that has nothing to do with it.
//!
//! The second case needs two requests overlapping, and the reason it cannot happen is one line in
//! another module: `mcp::serve` reads and dispatches stdin one line at a time, so a request cannot
//! begin while another is still inside `spawn_with_timeout`. Between requests nothing is published
//! at all, because `RequestCancel` un-publishes on drop. **So this module's correctness leans on
//! that serial loop**, and a reader should know it rather than infer independence from the word
//! "identity".
//!
//! # What this does not reach
//!
//! A walk an abandoned worker builds *after* its deadline captures nothing, so it runs to
//! completion as it does today. That is the safe direction to fail in — a walk is never wrongly
//! cancelled, only insufficiently — and the walk in flight at the deadline is the expensive one.
//! Recorded so a future reader does not mistake it for an oversight.
//!
//! Adapted from lack435/tilth cancel.rs (end-state at 81c3a72).

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

/// The in-flight request's cancellation flag, or `None` when nothing has published one.
///
/// `None` is the normal state for every non-MCP caller — the CLI, `map`, the whole test suite — so
/// [`current`] hands those a token that is cancelled-never and costs one null check per entry.
static CURRENT: Mutex<Option<Arc<AtomicBool>>> = Mutex::new(None);

/// A snapshot of one request's cancellation flag, cheap to clone into a walk closure.
///
/// `None` means "no request published a token when this was taken", which reads as never
/// cancelled. Constructing it as an `Option` rather than always allocating an `Arc` keeps the
/// non-MCP paths free of both the allocation and the atomic load.
#[derive(Clone, Debug)]
pub(crate) struct CancelToken(Option<Arc<AtomicBool>>);

impl CancelToken {
    /// A token that is never cancelled. Test-only: production callers all reach a token through
    /// [`current`], which already yields this shape when no request has published one.
    #[cfg(test)]
    pub(crate) fn never() -> Self {
        Self(None)
    }

    /// `Relaxed` is sufficient and deliberate. The flag carries no data — nothing is published
    /// alongside it that a reader must see — and every use is a pure early exit whose result is
    /// discarded either way. A missed or late read costs one more file of walking, which is the
    /// same cost as not having the flag at all.
    pub(crate) fn is_cancelled(&self) -> bool {
        self.0.as_ref().is_some_and(|f| f.load(Ordering::Relaxed))
    }

    #[cfg(test)]
    pub(crate) fn cancel_for_test(&self) {
        if let Some(f) = &self.0 {
            f.store(true, Ordering::Relaxed);
        }
    }
}

/// The live handle `timeout.rs` keeps for the request it just published.
///
/// Separate from [`CancelToken`] so that only the spawner can cancel: walk code receives a
/// `CancelToken`, which has no way to set the flag outside tests.
pub(crate) struct RequestCancel(Arc<AtomicBool>);

impl RequestCancel {
    /// Cancel this request's walks. Callers must have established that the request's result is
    /// already discarded — see the module header.
    pub(crate) fn cancel(&self) {
        self.0.store(true, Ordering::Relaxed);
    }

    /// A read-only view, for binding to the worker thread.
    pub(crate) fn token(&self) -> CancelToken {
        CancelToken(Some(Arc::clone(&self.0)))
    }
}

thread_local! {
    /// The token of the request whose worker *is* this thread, if any.
    ///
    /// Deliberately separate from [`CURRENT`], and the difference is the whole reason both exist.
    /// `CURRENT` answers "which request is in flight now", which is what a walk builder wants and
    /// what a thread-local cannot answer (builders run wherever `rayon::join` puts them). This
    /// answers "which request am *I* working for", which `CURRENT` cannot: by the time an
    /// abandoned worker reaches its render stage, the serial loop has already published the *next*
    /// request's token, so a worker asking `CURRENT` would be told it is not cancelled.
    ///
    /// Absent on rayon's stolen threads, which reads as not-cancelled — the same
    /// never-wrongly-cancelled direction the rest of this module fails in.
    static WORKER: std::cell::RefCell<Option<CancelToken>> =
        const { std::cell::RefCell::new(None) };
}

/// Bind `token` to this thread for the life of the guard. Set by `spawn_with_timeout` on the
/// worker thread it spawns.
pub(crate) fn bind_worker(token: CancelToken) -> WorkerBinding {
    WORKER.with(|w| *w.borrow_mut() = Some(token));
    WorkerBinding
}

pub(crate) struct WorkerBinding;

impl Drop for WorkerBinding {
    fn drop(&mut self) {
        WORKER.with(|w| *w.borrow_mut() = None);
    }
}

/// Whether the request *this thread is working for* has been abandoned.
///
/// The guard on state that outlives a request. A cancelled worker still ranks and renders what it
/// retained — cancellation stops the walking, not the rendering — and rendering writes to the
/// shared [`crate::session::Session`]: `record_expand` decides whether a *later* request prints a
/// definition body or `[shown earlier]`, and `record_savings` feeds session savings accounting.
/// Those writes survive the request that made them.
///
/// Before cancellation existed the pollution was at least deterministic, because the walk always
/// ran to completion and the retained set is order-independent from a fixed input. Cancelling
/// mid-walk makes *how much* got recorded a function of when the deadline landed — so without this
/// guard a scheduling-dependent quantity would reach a returned answer. The output of the
/// cancelled request itself is discarded either way; it is the residue that needed stopping.
pub(crate) fn worker_request_cancelled() -> bool {
    WORKER.with(|w| w.borrow().as_ref().is_some_and(CancelToken::is_cancelled))
}

/// Un-publish on the way out, so nothing is published between requests.
///
/// This started as a tidy-up and turned out to be load-bearing. Leaving a resolved request's token
/// published is inert in production — only the deadline arm that created a token ever sets it, and
/// that arm has run — but "inert" is a property of *who can still cancel it*, not of its value, and
/// a token that was already cancelled stays cancelled. A walk built afterwards would capture it and
/// yield nothing.
///
/// It also sharpens the guarantee. With nothing published between requests, a walk built outside a
/// request captures no token at all, rather than the *next* request's — so under the serial
/// dispatch loop a walk is cancellable only by the request that built it.
///
/// `ptr_eq`, not "clear it": a later request may already have replaced us, and clearing then would
/// silently disarm cancellation for a request that is still in flight.
impl Drop for RequestCancel {
    fn drop(&mut self) {
        let mut current = CURRENT
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if current.as_ref().is_some_and(|c| Arc::ptr_eq(c, &self.0)) {
            *current = None;
        }
    }
}

/// Publish a fresh token as the in-flight request's, replacing whatever was there.
///
/// Always a *fresh* flag, never a reused one, which is what makes a live request unable to observe
/// a cancel: whatever was published before, this request's walks capture something that is `false`
/// and that only this request's deadline arm can set.
///
/// Publication also ends on `RequestCancel`'s `Drop` rather than only being overwritten here — see
/// that impl for why that turned out to be load-bearing rather than tidy.
pub(crate) fn begin_request() -> RequestCancel {
    let flag = Arc::new(AtomicBool::new(false));
    let mut current = CURRENT
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    *current = Some(Arc::clone(&flag));
    RequestCancel(flag)
}

/// Snapshot the in-flight request's token, to be captured into a walk being built now.
///
/// Taken at build time rather than consulted per entry through the global. Without that, an
/// abandoned worker's still-running walk would start consulting a later request's flag and could
/// be stopped by a timeout that has nothing to do with it.
pub(crate) fn current() -> CancelToken {
    // Under test, the published token is visible only to threads that asked for it. See
    // `make_visible_on_this_thread`.
    #[cfg(test)]
    if !TEST_VISIBLE.with(std::cell::Cell::get) {
        return CancelToken(None);
    }
    let current = CURRENT
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    CancelToken(current.clone())
}

#[cfg(test)]
thread_local! {
    /// Whether this thread should see the published token. Test-only, and load-bearing.
    ///
    /// [`CURRENT`] is process-global while the test suite is parallel, so without this a test that
    /// cancels its request hands a cancelled token to every *other* test that happens to call
    /// [`current`] in the same window — and since `base_walk_builder` calls it on every walk, that
    /// is most of the suite. The failure would be a walk that silently yields nothing, in an
    /// unrelated test, occasionally.
    ///
    /// A thread-local is the right key *here* and would be the wrong one in production: production
    /// builders run wherever `rayon::join` puts them, but a test opts in on the thread that then
    /// does the building.
    static TEST_VISIBLE: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}

/// Opt this thread in to seeing the published token, until the guard drops.
#[cfg(test)]
pub(crate) fn make_visible_on_this_thread() -> TestVisibility {
    TEST_VISIBLE.with(|v| v.set(true));
    TestVisibility
}

#[cfg(test)]
pub(crate) struct TestVisibility;

#[cfg(test)]
impl Drop for TestVisibility {
    fn drop(&mut self) {
        TEST_VISIBLE.with(|v| v.set(false));
    }
}

/// Serialises tests that publish into [`CURRENT`], which is process-global while the suite runs in
/// parallel.
///
/// Required of any test that reaches [`begin_request`] — **including indirectly**, since every
/// `spawn_with_timeout` publishes. Missing it does not fail the test that omits it; it fails
/// whichever other test was mid-publication, which is why the rule is stated as "any test that
/// spawns" rather than left to judgement.
///
/// Distinct from [`make_visible_on_this_thread`], and both are needed: the lock stops two
/// publishers from overlapping, the opt-in stops a publication from being seen by tests that are
/// not part of this at all.
#[cfg(test)]
pub(crate) static PUBLISH_LOCK: Mutex<()> = Mutex::new(());

#[cfg(test)]
mod tests {
    use super::*;

    /// Take both the publish lock and this thread's visibility opt-in, which every test that
    /// publishes needs together.
    fn publishing() -> (std::sync::MutexGuard<'static, ()>, TestVisibility) {
        let g = PUBLISH_LOCK
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        (g, make_visible_on_this_thread())
    }

    /// The opt-in is not a formality: without it a published token is invisible, which is what
    /// keeps one test's cancel out of every other test's walk.
    #[test]
    fn a_thread_that_has_not_opted_in_sees_no_published_token() {
        let (_g, visible) = publishing();
        let req = begin_request();
        req.cancel();
        assert!(current().is_cancelled(), "opted-in thread should see it");
        drop(visible);
        assert!(
            !current().is_cancelled(),
            "a published cancel leaked to a thread that never opted in"
        );
    }

    #[test]
    fn a_token_with_no_request_behind_it_is_never_cancelled() {
        let t = CancelToken::never();
        assert!(!t.is_cancelled());
        // Cancelling it is a no-op rather than a panic, so a walk built outside any request
        // cannot be stopped by anything.
        t.cancel_for_test();
        assert!(!t.is_cancelled());
    }

    #[test]
    fn cancelling_a_request_reaches_tokens_captured_from_it() {
        let (_g, _v) = publishing();
        let req = begin_request();
        let captured = current();
        assert!(!captured.is_cancelled());
        req.cancel();
        assert!(
            captured.is_cancelled(),
            "a captured token missed the cancel"
        );
    }

    /// The identity property the module header rests on: a token captured from request A is not
    /// reachable by request B's cancel. This is what makes the safety argument independent of
    /// whether the MCP loop is serial.
    #[test]
    fn cancelling_one_request_cannot_reach_another_requests_token() {
        let (_g, _v) = publishing();
        let first = begin_request();
        let first_token = current();
        let second = begin_request();
        let second_token = current();

        second.cancel();
        assert!(
            !first_token.is_cancelled(),
            "a later request's timeout cancelled an earlier request's walk"
        );
        assert!(second_token.is_cancelled());

        first.cancel();
        assert!(first_token.is_cancelled());
    }
}
