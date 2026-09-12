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

/// Atomically replace the per-file shards + reverse edges for every file
/// under `worktree` whose content signature (mtime + length) changed since
/// the last reconcile, and remove shards for files that disappeared.
/// Stops scanning at `deadline`, in which case deletions are not inferred
/// (an incomplete scan cannot tell "not seen" from "not yet reached").
pub(crate) fn reconcile(handle: &HandleState, worktree: &Path, deadline: Instant) -> Coverage {
    let Ok(known_signatures) = storage::all_signatures(&handle.db) else {
        return Coverage::default();
    };
    let previously_known: HashSet<String> = known_signatures.keys().cloned().collect();
    // Package roots for absolute-import resolution are discovered once per pass
    // from the worktree — the explicit scope — never inferred per file.
    let roots = crate::read::imports::PyRoots::discover(worktree);

    let mut seen = HashSet::new();
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
            continue;
        }

        // Non-code files (including binaries that would fail read_to_string
        // as non-UTF-8) never have imports to resolve; storing an empty-deps
        // shard for them (instead of `continue`-ing without one) records
        // their signature so they aren't re-scanned on every future pass.
        let deps = if matches!(
            crate::lang::detect_file_type(path),
            crate::types::FileType::Code(_)
        ) {
            let Ok(content) = std::fs::read_to_string(path) else {
                failed = true;
                continue;
            };
            crate::read::imports::resolve_scoped_paths(path, &content, &roots)
                .into_iter()
                .filter_map(|p| {
                    p.strip_prefix(worktree)
                        .ok()
                        .map(|r| r.to_string_lossy().to_string())
                })
                .collect()
        } else {
            Vec::new()
        };
        upserts.push((rel, storage::FileShard { signature, deps }));
    }

    // A cut-short scan cannot distinguish "deleted" from "not yet reached",
    // so only infer deletions from a complete pass.
    let deletes: Vec<String> = if timed_out || failed {
        Vec::new()
    } else {
        previously_known.difference(&seen).cloned().collect()
    };
    let files_changed = upserts.len() + deletes.len();

    if storage::apply_reconcile(&handle.db, &storage::ReconcileWrite { upserts, deletes }).is_err()
    {
        return Coverage {
            complete: false,
            files_scanned,
            files_changed: 0,
            timed_out,
        };
    }

    // `timed_out` reflects the walk only: a fully-walked pass stays complete
    // even when the redb write phase runs past the deadline.
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
}
