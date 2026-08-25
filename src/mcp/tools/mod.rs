mod definitions;
mod deps;
mod diff;
mod grok;
mod list;
mod read;
mod search;
mod search_v2;
mod write;

pub(super) use definitions::tool_definitions;
pub(super) use deps::tool_deps;
pub(super) use diff::tool_diff;
pub(super) use grok::tool_grok;
pub(super) use list::tool_list;
pub(super) use read::tool_read;
pub(super) use search::tool_search;
pub(super) use search_v2::tool_search_v2;
pub(super) use write::tool_write;

use std::path::PathBuf;

use serde_json::Value;

/// Extract the required `cwd` — the caller's absolute checkout directory. The
/// server's process cwd is frozen at spawn and cannot track the caller's live
/// shell, so every path-taking tool must be told where the checkout is. A
/// missing or relative `cwd` is refused with a teaching error naming the fix.
pub(super) fn require_cwd(args: &Value) -> Result<&std::path::Path, String> {
    let cwd = args.get("cwd").and_then(|v| v.as_str()).ok_or_else(|| {
        "missing required parameter \"cwd\": pass cwd: <absolute checkout directory> \
         (the server cannot see your shell's cwd)."
            .to_string()
    })?;
    let path = std::path::Path::new(cwd);
    if !path.is_absolute() {
        return Err(format!(
            "\"cwd\" \"{cwd}\" is relative: pass cwd: <absolute checkout directory> \
             (the server cannot see your shell's cwd)."
        ));
    }
    Ok(path)
}

/// Anchor a caller-supplied path/scope under the trust-absolute posture:
///
/// - **Absolute** path → used as-is (trusted as explicit intent, no confinement).
/// - **Relative** path → joined under `cwd`; `..` traversal is refused so a
///   relative spelling cannot climb out of the checkout.
pub(super) fn resolve_anchored(
    raw: &std::path::Path,
    cwd: &std::path::Path,
) -> Result<PathBuf, String> {
    if raw.is_absolute() {
        return Ok(raw.to_path_buf());
    }
    if raw
        .components()
        .any(|c| matches!(c, std::path::Component::ParentDir))
    {
        return Err(format!(
            "relative path \"{}\" escapes cwd via \"..\": pass a path under cwd or an absolute path.",
            raw.display(),
        ));
    }
    Ok(cwd.join(raw))
}

/// Resolve the `scope` arg under the trust-absolute posture (`resolve_anchored`).
/// An omitted scope defaults to `"."` → `cwd`. Accepts either a directory or a
/// single file path; a file scope restricts search to that file. grok, deps
/// and list have no single-file meaning and collapse it to the parent
/// directory instead.
///
/// When the anchored path does not resolve to an existing file or directory,
/// this refuses rather than falling back to `cwd`. A silent fallback substitutes a
/// search the caller did not ask for and cannot see they got — a scope naming
/// one file that does not exist would silently widen to the whole checkout.
/// Refusing is immediate and actionable; the error names which of three
/// things went wrong — missing, not a file/directory, or unstattable — using
/// `try_exists()` rather than `exists()`/`is_dir()` because both of those
/// answer "no" to a permission error too.
///
/// Adapted from lack435/tilth 19c235e9d132d052700199ca9617c89c88d6ba02, extended
/// to accept a file scope per lack435/tilth b5e964c + caa6d2b.
pub(super) fn resolve_scope(args: &Value, cwd: &std::path::Path) -> Result<PathBuf, String> {
    let raw_str = args.get("scope").and_then(|v| v.as_str()).unwrap_or(".");
    let raw: PathBuf = raw_str.into();
    let anchored = resolve_anchored(&raw, cwd)?;
    let resolved = anchored.canonicalize().unwrap_or(anchored);
    if resolved.is_dir() || resolved.is_file() {
        return Ok(resolved);
    }

    // Neither a file nor a directory. Refused rather than widened to `cwd`.
    //
    // `is_dir()`/`is_file()`/`exists()` all answer "no" to a permission error, so a bare
    // "does not exist" would be a false statement about a path that demonstrably exists.
    let detail: String = match resolved.try_exists() {
        Ok(true) => "exists but is neither a file nor a directory (e.g. a special file, \
                     or a symlink whose target is missing)"
            .to_string(),
        Ok(false) => "does not exist".to_string(),
        Err(e) => format!("cannot be inspected: {e}"),
    };
    // Name the resolved path too, but only when anchoring actually moved it — with a
    // relative scope the two differ and which one is wrong is exactly what the caller
    // cannot tell; with an absolute scope they already agree.
    let resolved_str = resolved.display().to_string();
    let same_place = |a: &str| a.trim_start_matches(r"\\?\").replace('\\', "/");
    let where_it_looked = if same_place(&resolved_str) == same_place(raw_str) {
        String::new()
    } else {
        format!(" (resolved to \"{resolved_str}\")")
    };
    Err(format!(
        "scope \"{raw_str}\"{where_it_looked} {detail}. \
         Refusing rather than falling back to a broader scope, which would silently \
         search far more than you asked for."
    ))
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
    fn require_cwd_missing_refused_with_teaching_error() {
        let args = serde_json::json!({});
        let err = require_cwd(&args).unwrap_err();
        assert!(
            err.contains("cwd") && err.contains("absolute checkout directory"),
            "missing cwd must teach the fix: {err}"
        );
    }

    #[test]
    fn require_cwd_relative_refused_with_teaching_error() {
        let args = serde_json::json!({ "cwd": "relative/dir" });
        let err = require_cwd(&args).unwrap_err();
        assert!(
            err.contains("relative") && err.contains("absolute checkout directory"),
            "relative cwd must be refused with the teaching error: {err}"
        );
    }

    #[test]
    fn require_cwd_absolute_returns_path() {
        let args = serde_json::json!({ "cwd": "/abs/checkout" });
        assert_eq!(
            require_cwd(&args).unwrap(),
            std::path::Path::new("/abs/checkout")
        );
    }

    #[test]
    fn resolve_anchored_relative_joins_under_cwd() {
        // A relative path anchors under cwd, never against the server's cwd.
        let cwd = std::path::Path::new("/checkout");
        let out = resolve_anchored(std::path::Path::new("src/foo.rs"), cwd).unwrap();
        assert_eq!(out, std::path::Path::new("/checkout/src/foo.rs"));
    }

    #[test]
    fn resolve_anchored_absolute_passes_through_untouched() {
        // Trust-absolute: an absolute path OUTSIDE cwd is used as-is, no refusal.
        let cwd = std::path::Path::new("/checkout");
        let abs = std::path::Path::new("/elsewhere/worktree/file.rs");
        assert_eq!(resolve_anchored(abs, cwd).unwrap(), abs);
    }

    #[test]
    fn resolve_anchored_dotdot_traversal_refused() {
        // A relative `..` spelling must not climb out of the checkout.
        let cwd = std::path::Path::new("/checkout");
        let err = resolve_anchored(std::path::Path::new("../escape.rs"), cwd).unwrap_err();
        assert!(
            err.contains("..") && err.contains("escapes"),
            "relative `..` traversal must be refused: {err}"
        );
    }

    #[test]
    fn resolve_scope_explicit_absolute_arg() {
        let tmp = tempfile::tempdir().unwrap();
        let args = serde_json::json!({ "scope": tmp.path().to_str().unwrap() });
        let scope = resolve_scope(&args, std::path::Path::new("/unused/cwd")).unwrap();
        assert_eq!(scope, tmp.path().canonicalize().unwrap());
    }

    #[test]
    fn resolve_scope_omitted_defaults_to_cwd() {
        // An omitted scope defaults to "." → cwd. cwd is always absolute
        // (require_cwd guarantees it), so this is a safe repo-wide search.
        let tmp = tempfile::tempdir().unwrap();
        let args = serde_json::json!({});
        let scope = resolve_scope(&args, tmp.path()).unwrap();
        assert_eq!(scope, tmp.path().canonicalize().unwrap());
    }

    #[test]
    fn resolve_scope_anchors_relative_scope_under_cwd() {
        // A relative scope resolves under cwd, not the server's cwd.
        let tmp = tempfile::tempdir().unwrap();
        let sub = tmp.path().join("sub");
        std::fs::create_dir(&sub).unwrap();
        let args = serde_json::json!({ "scope": "sub" });
        let scope = resolve_scope(&args, tmp.path()).unwrap();
        assert_eq!(scope, sub.canonicalize().unwrap());
    }

    /// A `scope` that does not exist is refused, not widened to `cwd`. A silent
    /// fallback substitutes a search the caller did not ask for — a scope naming
    /// one file that does not exist would silently widen to the whole checkout.
    #[test]
    fn resolve_scope_missing_dir_is_refused_not_widened_to_cwd() {
        let tmp = tempfile::tempdir().unwrap();
        let args = serde_json::json!({ "scope": "/nonexistent/directory/zzz" });
        let err = resolve_scope(&args, tmp.path()).unwrap_err();
        assert_eq!(
            err,
            "scope \"/nonexistent/directory/zzz\" does not exist. Refusing rather than \
             falling back to a broader scope, which would silently search far more than \
             you asked for."
        );
    }

    #[test]
    fn resolve_scope_file_is_accepted_and_returned_verbatim() {
        // A file scope is now accepted (not refused) and returned as the
        // canonicalized file path — restricting search/grok/deps to that file.
        let tmp = tempfile::tempdir().unwrap();
        let file = tmp.path().join("foo.rs");
        std::fs::write(&file, "fn foo() {}").unwrap();
        let args = serde_json::json!({ "scope": file.to_str().unwrap() });
        let scope = resolve_scope(&args, tmp.path()).unwrap();
        assert_eq!(scope, file.canonicalize().unwrap());
    }

    #[test]
    fn resolve_scope_accepts_file_and_still_refuses_missing() {
        // A missing path is still refused even though files are now accepted —
        // acceptance is limited to paths that actually exist.
        let tmp = tempfile::tempdir().unwrap();
        let args = serde_json::json!({ "scope": "missing.rs" });
        let err = resolve_scope(&args, tmp.path()).unwrap_err();
        assert!(
            err.contains("does not exist"),
            "missing file scope must still be refused: {err}"
        );
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
}
