//! Redb file path resolution: worktree identity → cache-dir key.
//!
//! The on-disk layout is `$XDG_CACHE_HOME/tilth/deps/<worktree-key>/<client-key>.redb`
//! (falling back to `$HOME/.cache` when `XDG_CACHE_HOME` is unset). The
//! worktree-key is a hash of the git toplevel + git-dir pair so two linked
//! worktrees of the same repo (same toplevel, different git dir) resolve to
//! distinct database files.

use std::path::{Path, PathBuf};
use std::process::Command;

use twox_hash::XxHash32;

use super::DepsError;

/// Git identity of a worktree: its toplevel directory and its (per-worktree)
/// git directory. Two linked worktrees of the same repo share a toplevel
/// ancestry but have distinct git dirs.
pub(super) struct WorktreeIdentity {
    pub(super) toplevel: PathBuf,
    pub(super) git_dir: PathBuf,
}

/// Resolve `cwd`'s git toplevel and absolute git dir via `git rev-parse`.
pub(super) fn worktree_identity(cwd: &Path) -> Result<WorktreeIdentity, DepsError> {
    let toplevel = run_git(cwd, &["rev-parse", "--show-toplevel"])?;
    let git_dir = run_git(cwd, &["rev-parse", "--absolute-git-dir"])?;
    let toplevel = PathBuf::from(toplevel)
        .canonicalize()
        .map_err(DepsError::Io)?;
    Ok(WorktreeIdentity {
        toplevel,
        git_dir: PathBuf::from(git_dir),
    })
}

fn run_git(cwd: &Path, args: &[&str]) -> Result<String, DepsError> {
    let output = Command::new("git")
        .args(args)
        .current_dir(cwd)
        .output()
        .map_err(DepsError::Io)?;
    if !output.status.success() {
        return Err(DepsError::Git(format!(
            "git {} failed: {}",
            args.join(" "),
            String::from_utf8_lossy(&output.stderr).trim()
        )));
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

/// Stable hex hash of the worktree identity — the directory component of the
/// redb path. Same (toplevel, git dir) pair always hashes the same; a
/// different git dir (linked worktree) always hashes differently.
pub(super) fn worktree_key(identity: &WorktreeIdentity) -> String {
    let seed = XxHash32::oneshot(0, identity.toplevel.to_string_lossy().as_bytes());
    let combined = XxHash32::oneshot(seed, identity.git_dir.to_string_lossy().as_bytes());
    format!("{combined:08x}")
}

/// Normalize a caller-supplied client key into a filesystem-safe stem.
pub(super) fn normalize_client_key(client_key: &str) -> String {
    client_key
        .trim()
        .chars()
        .map(|c| {
            if c.is_alphanumeric() || c == '-' || c == '_' {
                c
            } else {
                '_'
            }
        })
        .collect()
}

/// Base cache directory: `$XDG_CACHE_HOME/tilth/deps` or `$HOME/.cache/tilth/deps`.
pub(super) fn cache_root() -> Result<PathBuf, DepsError> {
    if let Ok(xdg) = std::env::var("XDG_CACHE_HOME") {
        if !xdg.is_empty() {
            return Ok(PathBuf::from(xdg).join("tilth").join("deps"));
        }
    }
    let home = home::home_dir()
        .ok_or_else(|| DepsError::Git("home directory not found ($HOME / $USERPROFILE)".into()))?;
    Ok(home.join(".cache").join("tilth").join("deps"))
}

/// Full redb file path for a (worktree, client) pair. Each client gets its own
/// directory under the worktree key so distinct clients never share a parent.
pub(super) fn redb_path(worktree_key: &str, client_key: &str) -> Result<PathBuf, DepsError> {
    let stem = normalize_client_key(client_key);
    Ok(cache_root()?
        .join(worktree_key)
        .join(stem)
        .join("index.redb"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalize_client_key_replaces_unsafe_chars() {
        assert_eq!(normalize_client_key(" claude/code "), "claude_code");
    }

    #[test]
    fn worktree_key_is_stable_and_distinguishes_git_dirs() {
        let a = WorktreeIdentity {
            toplevel: PathBuf::from("/repo"),
            git_dir: PathBuf::from("/repo/.git"),
        };
        let b = WorktreeIdentity {
            toplevel: PathBuf::from("/repo"),
            git_dir: PathBuf::from("/repo/.git/worktrees/feature"),
        };
        assert_eq!(worktree_key(&a), worktree_key(&a));
        assert_ne!(worktree_key(&a), worktree_key(&b));
    }
}
