mod definitions;
mod deps;
mod diff;
mod edit;
mod files;
mod read;
mod search;
mod session;

pub(crate) use definitions::tool_definitions;
pub(crate) use deps::tool_deps;
pub(crate) use diff::tool_diff;
#[cfg(test)]
pub(crate) use edit::parse_file_edit;
pub(crate) use edit::tool_edit;
pub(crate) use files::tool_files;
pub(crate) use read::tool_read;
pub(crate) use search::tool_search;
pub(crate) use session::tool_session;

use std::path::PathBuf;

use serde_json::Value;

/// Falls back to cwd when scope is invalid, with a warning message.
pub(crate) fn resolve_scope(args: &Value) -> (PathBuf, Option<String>) {
    let raw_str = args.get("scope").and_then(|v| v.as_str()).unwrap_or(".");
    let raw: PathBuf = raw_str.into();
    let resolved = raw.canonicalize().unwrap_or_else(|_| raw.clone());
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

pub(super) fn apply_budget(output: String, budget: Option<u64>) -> String {
    match budget {
        Some(b) => crate::budget::apply(&output, b),
        None => output,
    }
}
