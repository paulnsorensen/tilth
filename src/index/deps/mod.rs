//! Persistent per-file dependency index, backed by redb.
//!
//! Stores per-file dependency shards + reverse (dependent) edges at
//! `$XDG_CACHE_HOME/tilth/deps/<worktree-key>/<client-key>.redb` (see
//! `paths::cache_root`). `reconcile` atomically replaces only the shards for
//! files that changed since the last reconcile; `impact` returns only edges
//! verified against the file's current on-disk state, never a stale stored
//! edge.
//!
//! Every redb type and table definition stays private to this module family
//! (`storage.rs`, `paths.rs`). This module (`mod.rs`) is the crate-visible
//! surface: the `DepsError`, `HandleState`, `Coverage`, and `VerifiedPartial`
//! types plus the `open`, `reconcile`, `impact`, and `worktree_key` free
//! functions are all `pub(crate)` here; `handles::DepsIndexHandles` is the
//! one item re-exported from a submodule.

mod handles;
mod paths;
mod storage;

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::{Arc, OnceLock};
use std::time::Instant;

use redb::Database;

pub(crate) use handles::DepsIndexHandles;

#[cfg(test)]
static ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

/// `XDG_CACHE_HOME` is process-global; hold this for the duration of any
/// test that reads or writes it, to keep parallel tests from racing.
#[cfg(test)]
fn env_lock() -> std::sync::MutexGuard<'static, ()> {
    ENV_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

/// Failure opening or operating on a deps-index redb handle.
#[derive(Debug, thiserror::Error)]
pub(crate) enum DepsError {
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
    #[error("git error: {0}")]
    Git(String),
    #[error("redb error: {0}")]
    Redb(String),
}

/// An open redb handle for one (worktree, client) pair.
pub(crate) struct HandleState {
    db: Arc<Database>,
    worktree_root: PathBuf,
    #[allow(dead_code)] // read by `db_path()` under #[cfg(test)] only
    db_path: PathBuf,
}

impl HandleState {
    pub(crate) fn worktree_root(&self) -> &Path {
        &self.worktree_root
    }
    /// Path to the backing `.redb` file. Test/introspection only.
    #[cfg(test)]
    pub(crate) fn db_path(&self) -> &Path {
        &self.db_path
    }
}

/// Result of a `reconcile` or `impact` pass: whether it ran to completion
/// against the whole relevant file set, or stopped early at the deadline.
#[derive(Debug, Clone, Copy, Default)]
pub(crate) struct Coverage {
    pub(crate) complete: bool,
    pub(crate) files_scanned: usize,
    pub(crate) files_changed: usize,
    pub(crate) timed_out: bool,
}

/// Dependents of a target file, verified against current on-disk state.
pub(crate) struct VerifiedPartial {
    #[allow(dead_code)] // echoed back for callers that report the resolved target
    pub(crate) target: PathBuf,
    pub(crate) dependents: Vec<PathBuf>,
    pub(crate) coverage: Coverage,
}

/// Process-wide handle cache backing the free-function `open`.
static HANDLES: OnceLock<DepsIndexHandles> = OnceLock::new();

/// Open (or reuse) the redb handle for `cwd`'s worktree + `client_key`.
pub(crate) fn open(cwd: &Path, client_key: &str) -> Result<HandleState, DepsError> {
    HANDLES
        .get_or_init(DepsIndexHandles::new)
        .open(cwd, client_key)
}

/// The same worktree-identity hash used for the deps-index cache-dir key
/// (see `paths::worktree_key`), for telemetry's `worktree` field. Best-effort:
/// an unresolvable git identity yields an empty string rather than an error.
pub(crate) fn worktree_key(cwd: &Path) -> String {
    paths::worktree_identity(cwd)
        .map(|identity| paths::worktree_key(&identity))
        .unwrap_or_default()
}

/// Whether a worktree-relative path is a Python package initializer. Its
/// re-export bindings decide the ownership edges of every file that imports it,
/// so a change to one invalidates those importers even when their own bytes are
/// unchanged.
fn is_python_init(rel: &str) -> bool {
    Path::new(rel).file_name().and_then(|n| n.to_str()) == Some("__init__.py")
}

/// Build one changed file's shard fields (deps + re-export invalidation
/// hops) from its already-read `content`. Python files also record every
/// intermediate `__init__.py` hop proven while resolving a named
/// re-export; other languages keep the unscoped resolver's behavior and
/// record no hops.
fn resolved_shard_fields(
    abs: &Path,
    content: &str,
    worktree: &Path,
    roots: &crate::read::imports::PyRoots,
) -> (Vec<String>, Vec<String>) {
    let to_rel = |p: PathBuf| {
        p.strip_prefix(worktree)
            .ok()
            .map(|r| r.to_string_lossy().to_string())
    };
    let (paths, hops) = crate::read::imports::resolve_scoped_paths_with_hops(abs, content, roots);
    (
        paths.into_iter().filter_map(to_rel).collect(),
        hops.into_iter().filter_map(to_rel).collect(),
    )
}

/// Rebuild `rel`'s shard from its current on-disk state. `None` when the
/// file has disappeared or cannot be read. The one builder every
/// forced-rescan site (redirect/edited/deleted-init worklist, new-init
/// discovery, pending catch-up) shares, replacing duplicated inline blocks.
fn rescan_shard(
    worktree: &Path,
    roots: &crate::read::imports::PyRoots,
    rel: &str,
) -> Option<(String, storage::FileShard)> {
    let abs = worktree.join(rel);
    let signature = storage::signature_of(&abs)?;
    if !matches!(
        crate::lang::detect_file_type(&abs),
        crate::types::FileType::Code(_)
    ) {
        return Some((
            rel.to_string(),
            storage::FileShard {
                signature,
                deps: Vec::new(),
                reexport_hops: Vec::new(),
            },
        ));
    }
    let content = std::fs::read_to_string(&abs).ok()?;
    let (deps, reexport_hops) = resolved_shard_fields(&abs, &content, worktree, roots);
    Some((
        rel.to_string(),
        storage::FileShard {
            signature,
            deps,
            reexport_hops,
        },
    ))
}

/// The bounded set of files needing a forced shard rebuild this pass:
/// consumers whose re-export resolution passed through a changed or
/// deleted `__init__.py`, whether as their stored direct/owner edge
/// (`read_reverse`) or as an intermediate hop that isn't itself a stored
/// edge (`read_reexport_hop_reverse` — the redirect case) — plus any rel
/// carried over from a prior pass that ran out of deadline. Bounded by
/// (changed/deleted inits this pass × their known importers) + the
/// carried-over pending set; never a recursive graph walk. One exception:
/// a deadline cut mid-way through the *new-init discovery* scan (see
/// `reconcile`) spills every not-yet-checked unchanged Python file into
/// the carried-over pending set, since which of them will actually acquire
/// the new init can't be known without resolving them. That's a one-time,
/// bounded catch-up cost equal to what the interrupted scan would have
/// spent anyway — the next pass's `drain_worklist` just rescans each
/// unconditionally instead of re-checking `acquires_init`, so a file that
/// doesn't acquire anything is rewritten with an identical (harmless) shard.
fn reexport_worklist(
    db: &Database,
    upserts: &[(String, storage::FileShard)],
    deletes: &[String],
    previously_known: &HashSet<String>,
    pending: Vec<String>,
) -> HashSet<String> {
    let mut worklist: HashSet<String> = pending.into_iter().collect();
    let mut triggers: Vec<&str> = Vec::new();
    for (rel, _) in upserts {
        if is_python_init(rel) && previously_known.contains(rel.as_str()) {
            triggers.push(rel.as_str());
        }
    }
    for rel in deletes {
        if is_python_init(rel) {
            triggers.push(rel.as_str());
        }
    }
    for rel in triggers {
        if let Ok(importers) = storage::read_reverse(db, rel) {
            worklist.extend(importers);
        }
        if let Ok(importers) = storage::read_reexport_hop_reverse(db, rel) {
            worklist.extend(importers);
        }
    }
    worklist
}

/// Drain `worklist` under `deadline`, rescanning each rel's shard. Returns
/// the rebuilt shards plus whatever remained unprocessed when the deadline
/// stopped further work — the caller persists that remainder as
/// `pending_rescan` so the next pass resumes exactly where this one left
/// off, instead of losing track of a still-stale consumer.
fn drain_worklist(
    worktree: &Path,
    roots: &crate::read::imports::PyRoots,
    worklist: HashSet<String>,
    already: &HashSet<String>,
    deadline: Instant,
) -> (Vec<(String, storage::FileShard)>, Vec<String>) {
    let mut forced_upserts = Vec::new();
    let mut still_pending = Vec::new();
    let mut iter = worklist.into_iter();
    for rel in iter.by_ref() {
        if already.contains(&rel) {
            continue;
        }
        if Instant::now() >= deadline {
            still_pending.push(rel);
            break;
        }
        if let Some(shard) = rescan_shard(worktree, roots, &rel) {
            forced_upserts.push(shard);
        }
    }
    still_pending.extend(iter);
    (forced_upserts, still_pending)
}

/// Atomically replace the per-file shards + reverse edges for every file
/// under `worktree` whose content signature (mtime + length) changed since
/// the last reconcile, and remove shards for files that disappeared.
/// Stops scanning at `deadline`, in which case deletions are not inferred
/// (an incomplete scan cannot tell "not seen" from "not yet reached").
///
/// Named Python re-export chains are invalidated via a bounded worklist
/// (`reexport_worklist`): a changed or deleted `__init__.py` forces a
/// rescan of every importer that stored it as a direct/owner edge *or*
/// walked through it as an intermediate hop (a "redirect"), never an
/// unbounded transitive walk. A partial pass persists whatever worklist
/// entries it couldn't reach (`pending_rescan`) so the next reconcile
/// resumes them even if the triggering file's own signature no longer
/// looks changed — that signature was already committed by this pass. The
/// deadline is checked across the initial walk, the invalidation worklist,
/// the forced rebuild, and the write stage; `Coverage.complete` is false
/// whenever any of those stages stops with work remaining.
pub(crate) fn reconcile(handle: &HandleState, worktree: &Path, deadline: Instant) -> Coverage {
    let Ok(known_signatures) = storage::all_signatures(&handle.db) else {
        return Coverage::default();
    };
    let previously_known: HashSet<String> = known_signatures.keys().cloned().collect();
    // Package roots for absolute-import resolution are discovered once per pass
    // from the worktree — the explicit scope — never inferred per file.
    let roots = crate::read::imports::PyRoots::discover(worktree);

    let mut seen = HashSet::new();
    let mut unchanged_python = Vec::new();
    let mut upserts = Vec::new();
    let mut files_scanned = 0usize;
    let mut timed_out = false;
    let mut failed = false;

    for entry in ignore::WalkBuilder::new(worktree)
        .filter_entry(|entry| {
            !(entry.file_type().is_some_and(|ft| ft.is_dir())
                && entry
                    .file_name()
                    .to_str()
                    .is_some_and(|name| crate::search::skip_dir_entry(entry.path(), name)))
        })
        .build()
    {
        if Instant::now() >= deadline {
            timed_out = true;
            break;
        }
        let Ok(entry) = entry else {
            failed = true;
            continue;
        };
        let path = entry.path();
        if !path.is_file() {
            continue;
        }
        let Ok(rel_path) = path.strip_prefix(worktree) else {
            continue;
        };
        let rel = rel_path.to_string_lossy().to_string();
        seen.insert(rel.clone());
        files_scanned += 1;

        let Some(signature) = storage::signature_of(path) else {
            failed = true;
            continue;
        };
        let unchanged = known_signatures
            .get(&rel)
            .is_some_and(|sig| *sig == signature);
        if unchanged {
            if path.extension().and_then(|ext| ext.to_str()) == Some("py") {
                unchanged_python.push(rel);
            }
            continue;
        }

        // Non-code files (including binaries that would fail read_to_string
        // as non-UTF-8) never have imports to resolve; storing an empty-deps
        // shard for them (instead of `continue`-ing without one) records
        // their signature so they aren't re-scanned on every future pass.
        let (deps, reexport_hops) = if matches!(
            crate::lang::detect_file_type(path),
            crate::types::FileType::Code(_)
        ) {
            let Ok(content) = std::fs::read_to_string(path) else {
                failed = true;
                continue;
            };
            resolved_shard_fields(path, &content, worktree, &roots)
        } else {
            (Vec::new(), Vec::new())
        };
        upserts.push((
            rel,
            storage::FileShard {
                signature,
                deps,
                reexport_hops,
            },
        ));
    }

    // A cut-short scan cannot distinguish "deleted" from "not yet reached",
    // so only infer deletions from a complete pass.
    let deletes: Vec<String> = if timed_out || failed {
        Vec::new()
    } else {
        previously_known.difference(&seen).cloned().collect()
    };

    let pending_before = storage::read_pending_rescan(&handle.db).unwrap_or_default();
    let mut pending_to_write = pending_before.clone();

    if !timed_out {
        let already: HashSet<String> = upserts.iter().map(|(rel, _)| rel.clone()).collect();
        let worklist = reexport_worklist(
            &handle.db,
            &upserts,
            &deletes,
            &previously_known,
            pending_before,
        );
        let (mut forced_upserts, still_pending) =
            drain_worklist(worktree, &roots, worklist, &already, deadline);
        let worklist_timed_out = !still_pending.is_empty();
        timed_out |= worklist_timed_out;
        pending_to_write = still_pending;

        if !timed_out {
            let new_inits: HashSet<&str> = upserts
                .iter()
                .filter(|(rel, _)| is_python_init(rel) && !previously_known.contains(rel.as_str()))
                .map(|(rel, _)| rel.as_str())
                .collect();
            if !new_inits.is_empty() {
                let already_forced: HashSet<String> =
                    forced_upserts.iter().map(|(rel, _)| rel.clone()).collect();
                let mut unchanged_iter = unchanged_python.into_iter();
                for importer in unchanged_iter.by_ref() {
                    if already_forced.contains(importer.as_str()) {
                        continue;
                    }
                    if Instant::now() >= deadline {
                        timed_out = true;
                        pending_to_write.push(importer);
                        break;
                    }
                    if let Some((rel, shard)) = rescan_shard(worktree, &roots, &importer) {
                        if shard.deps.iter().any(|d| new_inits.contains(d.as_str())) {
                            forced_upserts.push((rel, shard));
                        }
                    }
                }
                pending_to_write.extend(unchanged_iter);
            }
        }
        upserts.extend(forced_upserts);
    }

    if Instant::now() >= deadline {
        timed_out = true;
    }

    let files_changed = upserts.len() + deletes.len();
    let write = storage::ReconcileWrite {
        upserts,
        deletes,
        pending_rescan: pending_to_write,
    };
    if storage::apply_reconcile(&handle.db, &write).is_err() {
        return Coverage {
            complete: false,
            files_scanned,
            files_changed: 0,
            timed_out,
        };
    }

    Coverage {
        complete: !timed_out && !failed,
        files_scanned,
        files_changed,
        timed_out,
    }
}

/// Dependents of `target`, each re-verified against the dependent file's
/// current on-disk state before being reported — a stored edge that no
/// longer holds (source deleted, import removed) is dropped rather than
/// returned stale.
pub(crate) fn impact(handle: &HandleState, target: &Path, deadline: Instant) -> VerifiedPartial {
    let target_abs = if target.is_absolute() {
        target.to_path_buf()
    } else {
        handle.worktree_root.join(target)
    };
    let Ok(target_rel) = target_abs
        .strip_prefix(&handle.worktree_root)
        .map(|r| r.to_string_lossy().to_string())
    else {
        return VerifiedPartial {
            target: target_abs,
            dependents: Vec::new(),
            coverage: Coverage::default(),
        };
    };

    let Ok(candidates) = storage::read_reverse(&handle.db, &target_rel) else {
        return VerifiedPartial {
            target: target_abs,
            dependents: Vec::new(),
            coverage: Coverage::default(),
        };
    };
    let mut dependents = Vec::new();
    let mut checked = 0usize;
    let mut timed_out = Instant::now() >= deadline;
    let mut failed = false;
    // Re-verification must resolve edges the same way reconcile stored them,
    // so a drifted consumer's absolute import is re-checked, not dropped.
    let roots = crate::read::imports::PyRoots::discover(&handle.worktree_root);

    for candidate_rel in &candidates {
        if Instant::now() >= deadline {
            timed_out = true;
            break;
        }
        checked += 1;
        let candidate_abs = handle.worktree_root.join(candidate_rel);
        let Some(live_signature) = storage::signature_of(&candidate_abs) else {
            failed |= candidate_abs.exists();
            continue; // source no longer exists: drop the stale edge
        };
        let Ok(Some(shard)) = storage::read_shard(&handle.db, candidate_rel) else {
            failed = true;
            continue;
        };
        let verified = if shard.signature == live_signature {
            true
        } else if let Ok(content) = std::fs::read_to_string(&candidate_abs) {
            crate::read::imports::resolve_scoped_paths(&candidate_abs, &content, &roots)
                .iter()
                .filter_map(|p| p.strip_prefix(&handle.worktree_root).ok())
                .any(|r| r.to_string_lossy() == target_rel)
        } else {
            failed = true;
            false
        };
        if verified {
            dependents.push(candidate_abs);
        }
    }

    VerifiedPartial {
        target: target_abs,
        dependents,
        coverage: Coverage {
            complete: !timed_out && !failed,
            files_scanned: checked,
            files_changed: 0,
            timed_out,
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command;
    use std::time::Duration;
    use tempfile::TempDir;

    fn init_git_repo() -> TempDir {
        let dir = tempfile::tempdir().unwrap();
        Command::new("git")
            .args(["init", "-q"])
            .current_dir(dir.path())
            .status()
            .unwrap();
        dir
    }

    fn far_deadline() -> Instant {
        Instant::now() + Duration::from_secs(30)
    }

    /// Canonicalize each path so equality holds where the temp dir is reached
    /// through a symlink (macOS aliases `/var` to `/private/var`). The index
    /// stores canonicalized worktree paths, so the expected side is
    /// canonicalized too — a portable normalization, not a `/private` string
    /// replacement.
    fn canonicalized(paths: &[PathBuf]) -> Vec<PathBuf> {
        paths.iter().map(|p| p.canonicalize().unwrap()).collect()
    }

    /// `XDG_CACHE_HOME` is process-global, so tests that set it must not
    /// run concurrently with each other; this guard serializes them.
    fn set_cache_dir() -> (std::sync::MutexGuard<'static, ()>, TempDir) {
        let guard = env_lock();
        let cache_dir = tempfile::tempdir().unwrap();
        std::env::set_var("XDG_CACHE_HOME", cache_dir.path());
        (guard, cache_dir)
    }

    #[test]
    fn unreadable_code_does_not_report_complete_coverage() {
        let repo = init_git_repo();
        std::fs::write(repo.path().join("broken.rs"), [0xff, 0xfe]).unwrap();
        let handle = DepsIndexHandles::new()
            .open(repo.path(), "unreadable-test")
            .unwrap();
        let coverage = reconcile(&handle, repo.path(), far_deadline());
        assert!(!coverage.complete);
        assert!(!coverage.timed_out);
    }

    #[test]
    fn nested_checkout_under_worktrees_is_not_ingested() {
        let repo = init_git_repo();
        std::fs::write(repo.path().join("real.rs"), "fn real() {}\n").unwrap();
        let nested = repo.path().join("worktrees").join("x");
        std::fs::create_dir_all(nested.join(".git")).unwrap();
        std::fs::write(nested.join("buried.rs"), "fn buried() {}\n").unwrap();
        let handle = DepsIndexHandles::new()
            .open(repo.path(), "nested-worktree-test")
            .unwrap();
        let coverage = reconcile(&handle, repo.path(), far_deadline());
        assert!(coverage.complete);
        assert_eq!(coverage.files_scanned, 1, "nested checkout was ingested");
    }

    #[test]
    fn distinct_client_keys_and_worktrees_get_distinct_db_paths() {
        let _cache = set_cache_dir();
        let repo_a = init_git_repo();
        let repo_b = init_git_repo();
        let handles = DepsIndexHandles::new();

        let a1 = handles.open(repo_a.path(), "client-1").unwrap();
        let a2 = handles.open(repo_a.path(), "client-2").unwrap();
        let b1 = handles.open(repo_b.path(), "client-1").unwrap();

        assert_ne!(a1.db_path(), a2.db_path());
        assert_ne!(a1.db_path(), b1.db_path());
    }

    #[test]
    fn key_normalization_is_stable_across_calls() {
        let _cache = set_cache_dir();
        let repo = init_git_repo();

        let handles = DepsIndexHandles::new();
        let first_path = handles
            .open(repo.path(), "client")
            .unwrap()
            .db_path()
            .to_path_buf();
        drop(handles); // release the redb lock before reopening from a fresh cache

        let second_handles = DepsIndexHandles::new();
        let second_path = second_handles
            .open(repo.path(), "client")
            .unwrap()
            .db_path()
            .to_path_buf();

        assert_eq!(first_path, second_path);
    }

    #[test]
    fn impact_drops_edge_when_source_is_deleted() {
        let _cache = set_cache_dir();
        let repo = init_git_repo();
        std::fs::write(repo.path().join("target.rs"), "pub fn f() {}\n").unwrap();
        std::fs::write(repo.path().join("dep.rs"), "use self::target;\n").unwrap();

        let handles = DepsIndexHandles::new();
        let handle = handles.open(repo.path(), "client").unwrap();
        reconcile(&handle, repo.path(), far_deadline());

        let before = impact(&handle, Path::new("target.rs"), far_deadline());
        assert_eq!(
            canonicalized(&before.dependents),
            canonicalized(&[repo.path().join("dep.rs")])
        );

        std::fs::remove_file(repo.path().join("dep.rs")).unwrap();
        let after = impact(&handle, Path::new("target.rs"), far_deadline());
        assert!(after.dependents.is_empty());
    }

    #[test]
    fn reconcile_rebuilds_only_the_changed_file() {
        let _cache = set_cache_dir();
        let repo = init_git_repo();
        std::fs::write(repo.path().join("a.rs"), "fn a() {}\n").unwrap();
        std::fs::write(repo.path().join("b.rs"), "fn b() {}\n").unwrap();

        let handles = DepsIndexHandles::new();
        let handle = handles.open(repo.path(), "client").unwrap();
        let first = reconcile(&handle, repo.path(), far_deadline());
        assert_eq!(first.files_changed, 2);

        // dirty then revert: content ends up identical, but mtime moves.
        std::fs::write(repo.path().join("a.rs"), "fn a() { /* dirty */ }\n").unwrap();
        std::fs::write(repo.path().join("a.rs"), "fn a() {}\n").unwrap();
        let second = reconcile(&handle, repo.path(), far_deadline());
        assert_eq!(second.files_changed, 1);
        assert!(second.complete);
    }

    #[test]
    fn reconcile_removes_shard_for_deleted_file_and_its_reverse_edges() {
        let _cache = set_cache_dir();
        let repo = init_git_repo();
        std::fs::write(repo.path().join("target.rs"), "pub fn f() {}\n").unwrap();
        std::fs::write(repo.path().join("dep.rs"), "use self::target;\n").unwrap();

        let handles = DepsIndexHandles::new();
        let handle = handles.open(repo.path(), "client").unwrap();
        reconcile(&handle, repo.path(), far_deadline());
        assert_eq!(
            impact(&handle, Path::new("target.rs"), far_deadline())
                .dependents
                .len(),
            1
        );

        std::fs::remove_file(repo.path().join("dep.rs")).unwrap();
        let coverage = reconcile(&handle, repo.path(), far_deadline());
        assert_eq!(coverage.files_changed, 1);
        assert!(impact(&handle, Path::new("target.rs"), far_deadline())
            .dependents
            .is_empty());
    }

    #[test]
    fn reconcile_picks_up_untracked_and_renamed_files() {
        let _cache = set_cache_dir();
        let repo = init_git_repo();
        std::fs::write(repo.path().join("target.rs"), "pub fn f() {}\n").unwrap();

        let handles = DepsIndexHandles::new();
        let handle = handles.open(repo.path(), "client").unwrap();
        reconcile(&handle, repo.path(), far_deadline());

        // untracked file appears
        std::fs::write(repo.path().join("untracked.rs"), "use self::target;\n").unwrap();
        reconcile(&handle, repo.path(), far_deadline());
        assert_eq!(
            impact(&handle, Path::new("target.rs"), far_deadline())
                .dependents
                .len(),
            1
        );

        // rename: old path gone, new path takes over the dependency
        std::fs::rename(
            repo.path().join("untracked.rs"),
            repo.path().join("renamed.rs"),
        )
        .unwrap();
        reconcile(&handle, repo.path(), far_deadline());
        let after = impact(&handle, Path::new("target.rs"), far_deadline());
        assert_eq!(
            canonicalized(&after.dependents),
            canonicalized(&[repo.path().join("renamed.rs")])
        );
    }

    fn write_file(root: &Path, rel: &str, body: &str) {
        let path = root.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, body).unwrap();
    }

    /// The confirmed #197 src-layout fixture: a producer package re-exporting
    /// `RankingEntry` from `rankings.py` through its `ingest/__init__.py`, plus a
    /// direct importer and a package-surface importer.
    fn reexport_fixture(root: &Path) {
        write_file(root, "packages/producer/src/producer/__init__.py", "");
        write_file(
            root,
            "packages/producer/src/producer/ingest/rankings.py",
            "class RankingEntry:\n    pass\n",
        );
        write_file(
            root,
            "packages/producer/src/producer/ingest/__init__.py",
            "from .rankings import RankingEntry\n",
        );
        write_file(root, "consumer/src/consumer/__init__.py", "");
        write_file(
            root,
            "consumer/src/consumer/direct.py",
            "from producer.ingest.rankings import RankingEntry\n",
        );
        write_file(
            root,
            "consumer/src/consumer/surface.py",
            "from producer.ingest import RankingEntry\n",
        );
    }

    #[test]
    fn warm_reconcile_drops_reexport_ownership_when_only_init_changes() {
        let _cache = set_cache_dir();
        let repo = init_git_repo();
        reexport_fixture(repo.path());
        let handle = DepsIndexHandles::new()
            .open(repo.path(), "reexport")
            .unwrap();
        reconcile(&handle, repo.path(), far_deadline());

        let rankings = Path::new("packages/producer/src/producer/ingest/rankings.py");
        let surface = repo
            .path()
            .join("consumer/src/consumer/surface.py")
            .canonicalize()
            .unwrap();
        let direct = repo
            .path()
            .join("consumer/src/consumer/direct.py")
            .canonicalize()
            .unwrap();

        let before: HashSet<PathBuf> =
            canonicalized(&impact(&handle, rankings, far_deadline()).dependents)
                .into_iter()
                .collect();
        assert!(
            before.contains(&surface),
            "re-export ownership edge missing on warm base: {before:?}"
        );
        assert!(before.contains(&direct));

        // Remove the re-export; leave every consumer's bytes untouched. Only
        // __init__.py changes, so the consumer-signature skip would keep the
        // stale surface.py -> rankings.py ownership edge without the force pass.
        write_file(
            repo.path(),
            "packages/producer/src/producer/ingest/__init__.py",
            "",
        );
        let warm = reconcile(&handle, repo.path(), far_deadline());
        assert!(warm.complete);

        let after: HashSet<PathBuf> =
            canonicalized(&impact(&handle, rankings, far_deadline()).dependents)
                .into_iter()
                .collect();
        assert!(
            !after.contains(&surface),
            "stale re-export ownership edge survived warm reconcile: {after:?}"
        );
        // The genuine direct importer is unaffected by the __init__ edit.
        assert!(after.contains(&direct));
    }

    #[test]
    fn warm_reconcile_drops_edges_when_init_is_deleted() {
        let _cache = set_cache_dir();
        let repo = init_git_repo();
        reexport_fixture(repo.path());
        let handle = DepsIndexHandles::new()
            .open(repo.path(), "delete-init")
            .unwrap();
        reconcile(&handle, repo.path(), far_deadline());

        let init = "packages/producer/src/producer/ingest/__init__.py";
        let rankings = Path::new("packages/producer/src/producer/ingest/rankings.py");
        std::fs::remove_file(repo.path().join(init)).unwrap();
        let warm = reconcile(&handle, repo.path(), far_deadline());
        assert!(warm.complete);

        let surface = "consumer/src/consumer/surface.py";
        let shard = storage::read_shard(&handle.db, surface).unwrap().unwrap();
        assert!(!shard.deps.contains(&init.to_string()));
        assert!(!shard.deps.contains(&rankings.to_string_lossy().to_string()));
    }

    #[test]
    fn warm_reconcile_discovers_importers_when_init_is_added() {
        let _cache = set_cache_dir();
        let repo = init_git_repo();
        reexport_fixture(repo.path());
        let init = "packages/producer/src/producer/ingest/__init__.py";
        std::fs::remove_file(repo.path().join(init)).unwrap();
        let handle = DepsIndexHandles::new()
            .open(repo.path(), "add-init")
            .unwrap();
        reconcile(&handle, repo.path(), far_deadline());

        write_file(repo.path(), init, "from .rankings import RankingEntry\n");
        let warm = reconcile(&handle, repo.path(), far_deadline());
        assert!(warm.complete);

        let surface = "consumer/src/consumer/surface.py";
        let rankings = "packages/producer/src/producer/ingest/rankings.py";
        let shard = storage::read_shard(&handle.db, surface).unwrap().unwrap();
        assert!(shard.deps.contains(&init.to_string()));
        assert!(shard.deps.contains(&rankings.to_string()));
    }

    #[test]
    fn reconcile_reports_partial_coverage_when_deadline_expires() {
        let _cache = set_cache_dir();
        let repo = init_git_repo();
        for i in 0..20 {
            std::fs::write(repo.path().join(format!("f{i}.rs")), "fn f() {}\n").unwrap();
        }
        let handles = DepsIndexHandles::new();
        let handle = handles.open(repo.path(), "client").unwrap();
        let coverage = reconcile(&handle, repo.path(), Instant::now());
        assert!(coverage.timed_out);
        assert!(!coverage.complete);
    }

    /// A genuine two-hop re-export chain: `producer/__init__.py` re-exports
    /// from `producer/ingest/__init__.py`, which itself re-exports from a
    /// leaf module. The middle `__init__.py` is invisible to the consumer's
    /// own direct edges — it only shows up as a `reexport_hops` entry, which
    /// is exactly what the old code couldn't invalidate through.
    fn two_hop_reexport_fixture(root: &Path) {
        write_file(
            root,
            "packages/producer/src/producer/__init__.py",
            "from .ingest import Thing\n",
        );
        write_file(
            root,
            "packages/producer/src/producer/ingest/__init__.py",
            "from .leaf1 import Thing\n",
        );
        write_file(
            root,
            "packages/producer/src/producer/ingest/leaf1.py",
            "class Thing:\n    pass\n",
        );
        write_file(
            root,
            "packages/producer/src/producer/ingest/leaf2.py",
            "class Thing:\n    pass\n",
        );
        write_file(root, "consumer/src/consumer/__init__.py", "");
        write_file(
            root,
            "consumer/src/consumer/surface.py",
            "from producer import Thing\n",
        );
    }

    #[test]
    fn warm_reconcile_redirects_two_hop_owner_from_leaf1_to_leaf2() {
        let _cache = set_cache_dir();
        let repo = init_git_repo();
        two_hop_reexport_fixture(repo.path());
        let handle = DepsIndexHandles::new()
            .open(repo.path(), "redirect")
            .unwrap();
        reconcile(&handle, repo.path(), far_deadline());

        let leaf1 = Path::new("packages/producer/src/producer/ingest/leaf1.py");
        let leaf2 = Path::new("packages/producer/src/producer/ingest/leaf2.py");
        let surface_abs = repo.path().join("consumer/src/consumer/surface.py");
        let surface = surface_abs.canonicalize().unwrap();
        let surface_bytes_before = std::fs::read(&surface_abs).unwrap();

        assert!(
            canonicalized(&impact(&handle, leaf1, far_deadline()).dependents).contains(&surface)
        );
        assert!(
            !canonicalized(&impact(&handle, leaf2, far_deadline()).dependents).contains(&surface)
        );

        // Redirect: only the middle-hop init's bytes change. Consumer is untouched.
        write_file(
            repo.path(),
            "packages/producer/src/producer/ingest/__init__.py",
            "from .leaf2 import Thing\n",
        );
        let warm = reconcile(&handle, repo.path(), far_deadline());
        assert!(warm.complete);
        assert!(!warm.timed_out);
        // The edited ingest/__init__.py itself, plus two forced rescans: the
        // outer producer/__init__.py (ingest/__init__.py is its own direct
        // edge, so `read_reverse` names it) and surface.py (ingest/__init__.py
        // is only its `reexport_hops` entry, so `read_reexport_hop_reverse`
        // names it).
        assert_eq!(
            warm.files_changed, 3,
            "expected the edited init + the two rescanned importers (producer/__init__.py and surface.py)"
        );

        assert_eq!(
            std::fs::read(&surface_abs).unwrap(),
            surface_bytes_before,
            "consumer bytes must stay untouched by the redirect"
        );
        assert!(
            !canonicalized(&impact(&handle, leaf1, far_deadline()).dependents).contains(&surface),
            "stale leaf1 ownership edge survived the redirect"
        );
        assert!(
            canonicalized(&impact(&handle, leaf2, far_deadline()).dependents).contains(&surface),
            "redirected leaf2 ownership edge missing after warm reconcile"
        );
    }

    #[test]
    fn warm_reconcile_drops_two_hop_owner_edge_when_middle_init_is_deleted() {
        let _cache = set_cache_dir();
        let repo = init_git_repo();
        two_hop_reexport_fixture(repo.path());
        let handle = DepsIndexHandles::new()
            .open(repo.path(), "two-hop-delete")
            .unwrap();
        reconcile(&handle, repo.path(), far_deadline());

        let leaf1 = Path::new("packages/producer/src/producer/ingest/leaf1.py");
        let surface = repo
            .path()
            .join("consumer/src/consumer/surface.py")
            .canonicalize()
            .unwrap();
        assert!(
            canonicalized(&impact(&handle, leaf1, far_deadline()).dependents).contains(&surface)
        );

        std::fs::remove_file(
            repo.path()
                .join("packages/producer/src/producer/ingest/__init__.py"),
        )
        .unwrap();
        let warm = reconcile(&handle, repo.path(), far_deadline());
        assert!(warm.complete);

        assert!(
            !canonicalized(&impact(&handle, leaf1, far_deadline()).dependents).contains(&surface),
            "stale two-hop ownership edge survived middle-init deletion"
        );
    }

    #[test]
    fn partial_reexport_invalidation_resumes_on_next_reconcile() {
        let _cache = set_cache_dir();
        let repo = init_git_repo();
        two_hop_reexport_fixture(repo.path());
        let handle = DepsIndexHandles::new()
            .open(repo.path(), "deadline-resume")
            .unwrap();
        reconcile(&handle, repo.path(), far_deadline());

        // Redirect on disk, then simulate "the walk just noticed this one file
        // changed" without racing the real walk against a deadline.
        write_file(
            repo.path(),
            "packages/producer/src/producer/ingest/__init__.py",
            "from .leaf2 import Thing\n",
        );
        let roots = crate::read::imports::PyRoots::discover(repo.path());
        let init_rel = "packages/producer/src/producer/ingest/__init__.py".to_string();
        let (rel, new_shard) = rescan_shard(repo.path(), &roots, &init_rel).unwrap();
        let upserts = vec![(rel, new_shard)];
        let previously_known: HashSet<String> = [init_rel.clone()].into_iter().collect();

        let worklist = reexport_worklist(&handle.db, &upserts, &[], &previously_known, Vec::new());
        let (forced, pending) = drain_worklist(
            repo.path(),
            &roots,
            worklist,
            &HashSet::new(),
            Instant::now(),
        );

        assert!(
            !pending.is_empty(),
            "an already-past deadline must carry the worklist forward as pending"
        );
        assert!(
            forced.is_empty(),
            "no rescans should complete once the deadline has passed"
        );
        let surface_rel = "consumer/src/consumer/surface.py";
        assert!(
            pending.iter().any(|p| p == surface_rel),
            "the consumer needing rescan must be carried forward as pending, not dropped: {pending:?}"
        );

        // Persist exactly what a real partial pass would have committed: the
        // trigger's own new shard, plus the still-pending worklist.
        storage::apply_reconcile(
            &handle.db,
            &storage::ReconcileWrite {
                upserts,
                deletes: Vec::new(),
                pending_rescan: pending,
            },
        )
        .unwrap();
        assert!(!storage::read_pending_rescan(&handle.db).unwrap().is_empty());

        // The triggering init's signature is already committed, so it no longer
        // "looks changed" — only the persisted pending entry can resume the work.
        let leaf1 = Path::new("packages/producer/src/producer/ingest/leaf1.py");
        let leaf2 = Path::new("packages/producer/src/producer/ingest/leaf2.py");
        let resumed = reconcile(&handle, repo.path(), far_deadline());
        assert!(resumed.complete);

        let surface = repo.path().join(surface_rel).canonicalize().unwrap();
        assert!(
            !canonicalized(&impact(&handle, leaf1, far_deadline()).dependents).contains(&surface)
        );
        assert!(
            canonicalized(&impact(&handle, leaf2, far_deadline()).dependents).contains(&surface)
        );
        assert!(
            storage::read_pending_rescan(&handle.db).unwrap().is_empty(),
            "pending worklist must drain once the follow-up reconcile completes it"
        );
    }
}
