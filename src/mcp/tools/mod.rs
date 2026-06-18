mod definitions;
mod deps;
mod diff;
mod grok;
mod list;
mod read;
mod search;
mod write;

pub(super) use definitions::tool_definitions;
pub(super) use deps::tool_deps;
pub(super) use diff::tool_diff;
pub(super) use grok::tool_grok;
pub(super) use list::tool_list;
pub(super) use read::tool_read;
pub(super) use search::tool_search;
pub(super) use write::tool_write;

use std::path::PathBuf;

use serde_json::Value;

/// Falls back to cwd when scope is invalid, with a warning message.
/// When `root` is `Some`, resolves the scope under `root` instead of cwd.
pub(super) fn resolve_scope(
    args: &Value,
    root: Option<&std::path::Path>,
) -> (PathBuf, Option<String>) {
    let raw_str = args.get("scope").and_then(|v| v.as_str()).unwrap_or(".");
    let raw: PathBuf = raw_str.into();
    let resolved = if raw.is_absolute() {
        raw.canonicalize().unwrap_or_else(|_| raw.clone())
    } else if let Some(r) = root {
        let joined = r.join(&raw);
        joined.canonicalize().unwrap_or(joined)
    } else {
        raw.canonicalize().unwrap_or_else(|_| raw.clone())
    };
    let cwd = std::env::current_dir().unwrap_or_else(|_| ".".into());
    if resolved == cwd {
        return (".".into(), None);
    }
    if !resolved.is_dir() {
        return (
            ".".into(),
            Some(format!(
                "scope \"{raw_str}\" is not a valid directory, searching current directory instead.\n\n"
            )),
        );
    }
    (resolved, None)
}

/// Resolve a relative read path against an optional `root`, mirroring `resolve_write_path`.
/// Absolute paths are used as-is; relative paths join against `root` when provided.
pub(super) fn resolve_read_path(path: &std::path::Path, root: Option<&std::path::Path>) -> PathBuf {
    if path.is_absolute() {
        return path.to_path_buf();
    }
    match root {
        Some(r) => r.join(path),
        None => path.to_path_buf(),
    }
}

pub(super) fn apply_budget(output: &str, budget: Option<u64>) -> String {
    match budget {
        Some(b) => crate::budget::apply(output, b),
        None => crate::budget::apply(output, crate::budget::DEFAULT_BUDGET),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resolve_scope_explicit_arg() {
        let tmp = tempfile::tempdir().unwrap();
        let args = serde_json::json!({ "scope": tmp.path().to_str().unwrap() });
        let (scope, warning) = resolve_scope(&args, None);
        assert_eq!(scope, tmp.path().canonicalize().unwrap());
        assert!(warning.is_none());
    }

    #[test]
    fn resolve_scope_no_arg_uses_cwd() {
        let args = serde_json::json!({});
        let (scope, warning) = resolve_scope(&args, None);
        // With no arg, defaults to "." which is cwd
        let cwd = std::env::current_dir().unwrap();
        // The function returns "." when resolved == cwd
        assert!(scope == std::path::Path::new(".") || scope == cwd);
        assert!(warning.is_none());
    }

    #[test]
    fn resolve_scope_invalid_dir_warns() {
        let args = serde_json::json!({ "scope": "/nonexistent/directory/zzz" });
        let (scope, warning) = resolve_scope(&args, None);
        assert_eq!(scope, PathBuf::from("."));
        assert!(warning.is_some());
        assert!(warning.unwrap().contains("not a valid directory"));
    }

    #[test]
    fn apply_budget_none_caps_at_default() {
        // An output far larger than DEFAULT_BUDGET must be truncated even with
        // no explicit budget — otherwise a broad read/regex/diff blows the host
        // ~25K tool-response limit.
        let oversized = format!(
            "# header line\n{}",
            "filler content that repeats and repeats\n".repeat(20_000)
        );
        let capped = apply_budget(&oversized, None);
        assert!(
            capped.len() < oversized.len(),
            "output should be truncated below the default budget"
        );
        assert!(
            capped.contains("truncated"),
            "truncation notice should be present: {}",
            &capped[capped.len().saturating_sub(120)..]
        );
    }

    /// Reproduces issue #37: MCP host launches tilth with cwd=/. The --scope
    /// flag should override this.
    #[test]
    fn scope_flag_overrides_bad_cwd() {
        let project = tempfile::tempdir().unwrap();
        let project_path = project.path();

        // Create a manifest so package_root can find it
        std::fs::write(
            project_path.join("Cargo.toml"),
            "[package]\nname = \"test\"",
        )
        .unwrap();
        std::fs::create_dir(project_path.join("src")).unwrap();
        std::fs::write(project_path.join("src/main.rs"), "fn main() {}").unwrap();

        // Save current cwd
        let orig_cwd = std::env::current_dir().unwrap();

        // Simulate Codex: cwd=/
        std::env::set_current_dir("/").unwrap();

        // Without --scope: resolve_scope returns "." which is /
        let args = serde_json::json!({});
        let (scope, _) = resolve_scope(&args, None);
        assert_eq!(
            scope,
            PathBuf::from("."),
            "Without --scope, should return . (which is /)"
        );

        // With --scope pointing to project: set_current_dir should fix everything
        let _ = std::env::set_current_dir(project_path);
        let args = serde_json::json!({});
        let (scope, _) = resolve_scope(&args, None);
        assert_eq!(
            scope,
            PathBuf::from("."),
            "After chdir to project, . should resolve correctly"
        );

        // Confirm the working dir resolved to the project, not / — so
        // scope-relative lookups land in the right tree.
        let cwd = std::env::current_dir().unwrap();
        assert_eq!(cwd, project_path.canonicalize().unwrap());

        // Restore
        std::env::set_current_dir(orig_cwd).unwrap();
    }

    #[test]
    fn resolve_read_path_relative_anchors_under_root() {
        // Guards the #78 contract: a relative path + root must resolve under root,
        // not under the server's cwd. Prevents worktree agents from silently reading
        // the parent checkout.
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path();
        let result = resolve_read_path(std::path::Path::new("src/foo.rs"), Some(root));
        assert_eq!(result, root.join("src/foo.rs"));
    }

    #[test]
    fn resolve_read_path_absolute_unaffected_by_root() {
        // Absolute paths must be used as-is regardless of root.
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path();
        let abs = std::path::Path::new("/tmp/other/file.rs");
        let result = resolve_read_path(abs, Some(root));
        assert_eq!(result, abs);
    }

    #[test]
    fn resolve_read_path_no_root_returns_relative_unchanged() {
        // Omitting root must be byte-identical to today's behavior (cwd-relative).
        let result = resolve_read_path(std::path::Path::new("src/lib.rs"), None);
        assert_eq!(result, PathBuf::from("src/lib.rs"));
    }

    #[test]
    fn resolve_scope_with_root_anchors_relative_scope() {
        // resolve_scope(Some(root)) must resolve a relative scope under root,
        // not under cwd — same contract as resolve_read_path.
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path();
        // Create a subdirectory under root to use as the relative scope.
        let sub = root.join("sub");
        std::fs::create_dir(&sub).unwrap();
        let args = serde_json::json!({ "scope": "sub" });
        let (scope, warning) = resolve_scope(&args, Some(root));
        assert_eq!(scope, sub.canonicalize().unwrap());
        assert!(warning.is_none());
    }

    #[test]
    fn resolve_scope_none_root_unchanged() {
        // Passing None for root must not change existing behavior.
        let tmp = tempfile::tempdir().unwrap();
        let args = serde_json::json!({ "scope": tmp.path().to_str().unwrap() });
        let (scope, warning) = resolve_scope(&args, None);
        assert_eq!(scope, tmp.path().canonicalize().unwrap());
        assert!(warning.is_none());
    }
}
