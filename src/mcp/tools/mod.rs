mod definitions;
mod read;
mod search_v2;
mod write;

pub(super) use definitions::tool_definitions;
pub(super) use read::tool_read;
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
/// - **Home** path (`~/…` or `$HOME/…`) → expanded to the home directory, then
///   treated as absolute. Only a leading `~` / `$HOME` component expands.
/// - **Relative** path → joined under `cwd`; `..` traversal is refused so a
///   relative spelling cannot climb out of the checkout.
pub(super) fn resolve_anchored(
    raw: &std::path::Path,
    cwd: &std::path::Path,
) -> Result<PathBuf, String> {
    if raw.is_absolute() {
        return Ok(raw.to_path_buf());
    }
    if let Some(home_path) = expand_home(raw)? {
        return Ok(home_path);
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

/// Expand a leading `~` or `$HOME` component to the home directory. Returns
/// `None` when `raw` does not start with one, so it anchors under `cwd`.
fn expand_home(raw: &std::path::Path) -> Result<Option<PathBuf>, String> {
    let mut components = raw.components();
    let Some(std::path::Component::Normal(first)) = components.next() else {
        return Ok(None);
    };
    if first != "~" && first != "$HOME" {
        return Ok(None);
    }
    let home = home::home_dir().ok_or_else(|| {
        format!(
            "cannot expand \"{}\": home directory not found ($HOME): pass an absolute path.",
            raw.display(),
        )
    })?;
    let rest = components.as_path();
    if rest.as_os_str().is_empty() {
        return Ok(Some(home));
    }
    Ok(Some(home.join(rest)))
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
    fn resolve_anchored_expands_tilde_to_home() {
        // `~/…` names the home directory, not a literal `~` dir under cwd (#83).
        let home = home::home_dir().expect("home directory");
        let cwd = std::path::Path::new("/checkout");
        let out = resolve_anchored(std::path::Path::new("~/.claude/hooks/hook.js"), cwd).unwrap();
        assert_eq!(out, home.join(".claude/hooks/hook.js"));
        assert_eq!(
            resolve_anchored(std::path::Path::new("~"), cwd).unwrap(),
            home
        );
    }

    #[test]
    fn resolve_anchored_expands_dollar_home() {
        let home = home::home_dir().expect("home directory");
        let cwd = std::path::Path::new("/checkout");
        let out = resolve_anchored(std::path::Path::new("$HOME/Dev/repo/a.rs"), cwd).unwrap();
        assert_eq!(out, home.join("Dev/repo/a.rs"));
    }

    #[test]
    fn resolve_anchored_tilde_inside_path_stays_literal() {
        // Only a leading `~` component expands; an interior `~` is a real name.
        let cwd = std::path::Path::new("/checkout");
        let out = resolve_anchored(std::path::Path::new("src/~/a.rs"), cwd).unwrap();
        assert_eq!(out, std::path::Path::new("/checkout/src/~/a.rs"));
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
}
