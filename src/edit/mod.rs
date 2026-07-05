use std::path::{Component, Path, PathBuf};

// Whole-file-tag edit subsystem: a Rust port of oh-my-pi's whole-file-tag edit
// model (upstream name: hashline), wired into the MCP through `crate::mcp::tools::write`.
pub mod apply;
pub mod block;
pub mod json;
pub mod mismatch;
pub mod parser;
pub mod recovery;
pub mod snapshots;
pub mod tag;

#[cfg(test)]
mod integration_tests;

/// Build a stable dedup key for a path. Canonicalise first (resolves symlinks
/// and `.`/`..` when the file exists), fall back to a lexical normalization
/// (strips `CurDir` components, walks `ParentDir` against the in-memory
/// stack — catches not-yet-created aliases like `new.rs` vs `./new.rs`)
/// then to the raw path. On macOS (commonly case-insensitive APFS) the key
/// is ASCII-lowercased so `Foo.rs` and `FOO.RS` collide; false-positive
/// collisions on case-sensitive APFS configs are preferred over
/// false-negatives that race two writers against the same inode.
///
/// **No `current_dir()` calls.** `std::path::absolute(p)` was previously
/// used here, but it reads `current_dir()` which is process-global mutable
/// state. Two parallel tests (one of which calls `set_current_dir`) could
/// race against each other and produce different keys for the same path
/// — surfacing as a flaky `dedup_catches_nonexistent_alias_spellings`
/// failure under CI's parallel test runner. Pure-lexical normalization
/// removes the race.
pub(crate) fn normalize_path_key(path: &Path) -> String {
    let resolved = std::fs::canonicalize(path).unwrap_or_else(|_| lexical_normalize(path));
    let key = resolved.to_string_lossy().into_owned();
    if cfg!(target_os = "macos") {
        key.to_ascii_lowercase()
    } else {
        key
    }
}

/// Lexical-only path normalization: skip `CurDir`, walk `ParentDir`
/// against the component stack, leave the rest in order. Does not touch
/// the filesystem or `current_dir()`, so it's deterministic under
/// parallel tests.
///
/// `ParentDir` handling depends on what's already on the stack:
///   * If the last component is a real (`Normal`) name, pop it —
///     `a/../b.rs` collapses to `b.rs`.
///   * If the stack is empty or only contains `..` markers AND the path
///     is relative, push `..` — `../foo.rs` stays `../foo.rs` (else it
///     would collapse to `foo.rs`, which is a different file on disk).
///   * If absolute and at root, `..` is a no-op (Linux semantics:
///     `/.. == /`).
///
/// The result is that two paths produce the same key iff they refer to
/// the same logical target through the lexical lens — `foo.rs` and
/// `./foo.rs` collide; `a/../b.rs` and `b.rs` collide; **`../foo.rs`
/// and `foo.rs` do NOT collide** (different parent dirs).
fn lexical_normalize(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    let mut is_absolute = false;
    // Count of `Normal` segments currently on the stack. Lets us decide
    // in O(1) whether `..` can pop something real (vs. needing to be
    // preserved as an unresolved `..` in a relative path).
    let mut normal_count: usize = 0;
    for component in path.components() {
        match component {
            Component::Prefix(_) | Component::RootDir => {
                is_absolute = true;
                out.push(component.as_os_str());
            }
            Component::Normal(_) => {
                out.push(component.as_os_str());
                normal_count += 1;
            }
            Component::CurDir => {}
            Component::ParentDir => {
                if normal_count > 0 {
                    out.pop();
                    normal_count -= 1;
                } else if !is_absolute {
                    // Preserve unresolved `..` in relative paths.
                    out.push("..");
                }
                // Absolute path with `..` at root → no-op.
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Pin the race fix: `normalize_path_key` MUST NOT depend on
    /// `current_dir()`. If it did, this test could flake under parallel
    /// test execution (another test calling `set_current_dir` could
    /// change the result of the second key computation). The dedup
    /// must hold even when cwd changes between the two computations,
    /// which we simulate here by toggling cwd in between.
    ///
    /// Regression: pre-fix this used `std::path::absolute()` whose
    /// behavior depends on `current_dir()`; the test was flaky on
    /// Linux CI because `mcp::tests::scope_handoff_when_cwd_is_root`
    /// runs in parallel and calls `set_current_dir("/")`.
    #[test]
    fn normalize_path_key_is_cwd_independent() {
        let key_a = normalize_path_key(Path::new("foo.rs"));
        let key_b = normalize_path_key(Path::new("./foo.rs"));
        assert_eq!(
            key_a, key_b,
            "foo.rs and ./foo.rs must normalize identically"
        );

        // `a/../b.rs` should resolve lexically to `b.rs` — same key as `b.rs`.
        let key_c = normalize_path_key(Path::new("a/../b.rs"));
        let key_d = normalize_path_key(Path::new("b.rs"));
        assert_eq!(
            key_c, key_d,
            "a/../b.rs and b.rs must normalize identically"
        );
    }

    /// Lexical normalization unit tests — these are the predicates the
    /// `normalize_path_key_is_cwd_independent` test pins as a guarantee.
    #[test]
    fn lexical_normalize_strips_curdir() {
        assert_eq!(
            lexical_normalize(Path::new("./foo.rs")),
            PathBuf::from("foo.rs")
        );
        assert_eq!(
            lexical_normalize(Path::new("a/./b/./c.rs")),
            PathBuf::from("a/b/c.rs")
        );
    }

    #[test]
    fn lexical_normalize_pops_on_parentdir() {
        assert_eq!(
            lexical_normalize(Path::new("a/../b.rs")),
            PathBuf::from("b.rs")
        );
        assert_eq!(
            lexical_normalize(Path::new("a/b/../../c.rs")),
            PathBuf::from("c.rs")
        );
    }

    #[test]
    fn lexical_normalize_preserves_absolute() {
        assert_eq!(
            lexical_normalize(Path::new("/abs/./foo.rs")),
            PathBuf::from("/abs/foo.rs")
        );
        assert_eq!(
            lexical_normalize(Path::new("/foo/../bar.rs")),
            PathBuf::from("/bar.rs")
        );
        // `..` at absolute root is a no-op (Linux: /.. == /).
        assert_eq!(lexical_normalize(Path::new("/..")), PathBuf::from("/"));
    }

    /// `../foo.rs` and `foo.rs` refer to DIFFERENT files (one in parent
    /// dir, one in cwd). The dedup must NOT collide them. Tests the
    /// fix to v1 of `lexical_normalize` which mistakenly popped at empty
    /// stack and collapsed `../foo.rs` → `foo.rs`.
    #[test]
    fn lexical_normalize_preserves_unresolved_parentdir() {
        assert_eq!(
            lexical_normalize(Path::new("../foo.rs")),
            PathBuf::from("../foo.rs")
        );
        // Multi-level: foo/bar/../../../baz.rs → ../baz.rs
        // (foo → +foo, bar → +bar, .. → pop bar, .. → pop foo, .. → push .., baz.rs → +baz.rs)
        assert_eq!(
            lexical_normalize(Path::new("foo/bar/../../../baz.rs")),
            PathBuf::from("../baz.rs")
        );
        // ../foo.rs and foo.rs must NOT produce equal keys.
        assert_ne!(
            normalize_path_key(Path::new("../foo.rs")),
            normalize_path_key(Path::new("foo.rs"))
        );
    }
}
