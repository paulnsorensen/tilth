//! Per-process cache of open redb handles, keyed by (worktree-key, client-key).
//!
//! Opening a redb `Database` mmaps the file and holds an OS-level advisory
//! lock; reopening it on every call would be wasteful and would contend the
//! lock against itself. The cache is count-bounded so a long-lived MCP
//! process serving many worktrees/clients cannot grow it unbounded.

use std::num::NonZeroUsize;
use std::path::Path;
use std::sync::{Arc, Mutex};

use clru::CLruCache;
use redb::Database;

use super::paths::{redb_path, worktree_identity, worktree_key};
use super::{DepsError, HandleState};

/// Count cap on simultaneously open redb handles. Each handle holds an mmap
/// and a file lock; a cap in the low tens keeps process resource usage
/// bounded even across many worktrees/clients.
const MAX_OPEN_HANDLES: usize = 32;

/// Cache keyed by (worktree-key, client-key) → (open handle, redb file path).
type HandleCache = CLruCache<(String, String), (Arc<Database>, std::path::PathBuf)>;

/// Bounded cache of open `redb::Database` handles.
pub(crate) struct DepsIndexHandles {
    handles: Mutex<HandleCache>,
}

impl Default for DepsIndexHandles {
    fn default() -> Self {
        Self::new()
    }
}

impl DepsIndexHandles {
    pub(crate) fn new() -> Self {
        Self {
            handles: Mutex::new(CLruCache::new(NonZeroUsize::new(MAX_OPEN_HANDLES).unwrap())),
        }
    }

    /// Number of handles currently cached. Test/introspection only.
    #[cfg(test)]
    pub(crate) fn len(&self) -> usize {
        self.handles.lock().unwrap().len()
    }

    /// Open (or reuse a cached) handle for `cwd`'s worktree + `client_key`.
    pub(crate) fn open(&self, cwd: &Path, client_key: &str) -> Result<HandleState, DepsError> {
        let identity = worktree_identity(cwd)?;
        let wt_key = worktree_key(&identity);
        let cache_key = (wt_key.clone(), client_key.to_string());

        let mut handles = self.handles.lock().unwrap();
        if let Some((db, db_path)) = handles.get(&cache_key) {
            return Ok(HandleState {
                db: Arc::clone(db),
                worktree_root: identity.toplevel,
                db_path: db_path.clone(),
            });
        }

        let db_path = redb_path(&wt_key, client_key)?;
        if let Some(parent) = db_path.parent() {
            std::fs::create_dir_all(parent).map_err(DepsError::Io)?;
        }
        let db = Arc::new(Database::create(&db_path).map_err(|e| DepsError::Redb(e.to_string()))?);
        handles.put(cache_key, (Arc::clone(&db), db_path.clone()));

        Ok(HandleState {
            db,
            worktree_root: identity.toplevel,
            db_path,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command;
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

    #[test]
    fn reuses_handle_for_repeated_worktree_and_client() {
        let _guard = super::super::env_lock();
        let repo = init_git_repo();
        let cache_dir = tempfile::tempdir().unwrap();
        std::env::set_var("XDG_CACHE_HOME", cache_dir.path());

        let handles = DepsIndexHandles::new();
        let a = handles.open(repo.path(), "client-a").unwrap();
        let b = handles.open(repo.path(), "client-a").unwrap();
        assert!(Arc::ptr_eq(&a.db, &b.db));
        assert_eq!(handles.len(), 1);
    }

    #[test]
    fn stays_bounded_under_many_distinct_clients() {
        let _guard = super::super::env_lock();
        let repo = init_git_repo();
        let cache_dir = tempfile::tempdir().unwrap();
        std::env::set_var("XDG_CACHE_HOME", cache_dir.path());

        let handles = DepsIndexHandles::new();
        for i in 0..(MAX_OPEN_HANDLES + 10) {
            handles.open(repo.path(), &format!("client-{i}")).unwrap();
        }
        assert!(handles.len() <= MAX_OPEN_HANDLES);
    }
}
