//! `tilth_list` — tree output with token-cost rollups.
//!
//! Resolves each glob via `ignore::WalkBuilder`, collects `(path, byte_len)`
//! pairs, and renders them as a single tree rooted at scope.

use std::fmt::Write as _;
use std::path::PathBuf;

use serde_json::Value;

const PATTERNS_SHAPE: &str = "\"patterns\" must be an array of glob strings: \
     pass patterns: [\"*.rs\"], or omit it for the full tree.";

pub(crate) fn tool_list(args: &Value) -> Result<String, String> {
    use globset::Glob;
    let cwd = super::require_cwd(args)?;
    let (scope, scope_warning) = super::resolve_scope(args, cwd)?;
    let budget = args.get("budget").and_then(serde_json::Value::as_u64);

    let patterns: Vec<String> = match args.get("patterns") {
        None => vec!["*".to_string()],
        Some(value) => {
            let Some(arr) = value.as_array() else {
                return Err(PATTERNS_SHAPE.to_string());
            };
            if arr.is_empty() {
                return Err("patterns must contain at least one glob".into());
            }
            if arr.len() > 20 {
                return Err(format!(
                    "patterns limited to 20 per call (got {})",
                    arr.len()
                ));
            }
            let mut patterns = Vec::with_capacity(arr.len());
            for item in arr {
                let Some(pattern) = item.as_str() else {
                    return Err(PATTERNS_SHAPE.to_string());
                };
                patterns.push(pattern.to_string());
            }
            patterns
        }
    };

    let depth = args.get("depth").and_then(|v| {
        v.as_u64()
            .map(|d| d as usize)
            .or_else(|| v.as_f64().map(|f| f as usize))
    });

    // Walk the scope directory and collect all files matching any pattern.
    let mut matchers = Vec::with_capacity(patterns.len());
    for p in &patterns {
        let glob = Glob::new(p).map_err(|e| format!("invalid glob pattern {p:?}: {e}"))?;
        matchers.push(glob.compile_matcher());
    }

    let mut entries: Vec<(PathBuf, u64)> = Vec::new();
    let mut extensions: std::collections::BTreeSet<String> = std::collections::BTreeSet::new();
    let mut builder = ignore::WalkBuilder::new(&scope);
    builder
        .follow_links(true)
        .hidden(false)
        .git_ignore(false)
        .git_global(false)
        .git_exclude(false)
        .ignore(false)
        .parents(false)
        .add_custom_ignore_filename(crate::search::TILTHIGNORE_FILE)
        .filter_entry(|entry| {
            if entry.file_type().is_some_and(|ft| ft.is_dir()) {
                if let Some(name) = entry.file_name().to_str() {
                    return !crate::search::SKIP_DIRS.contains(&name);
                }
            }
            true
        });
    if let Some(d) = depth {
        builder.max_depth(Some(d));
    }
    let walker = builder.build();
    for entry in walker.filter_map(Result::ok) {
        let Some(ft) = entry.file_type() else {
            continue;
        };
        if !ft.is_file() {
            continue;
        }
        let path = entry.path();
        let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
        let rel = path.strip_prefix(&scope).unwrap_or(path);
        if let Some(ext) = path.extension().and_then(|e| e.to_str()) {
            extensions.insert(ext.to_string());
        }
        let matched = matchers.iter().any(|m| m.is_match(name) || m.is_match(rel));
        if matched {
            let bytes = entry.metadata().map_or(0, |m| m.len());
            entries.push((path.to_path_buf(), bytes));
        }
    }

    let tree = crate::mcp::tree::render_tree(&scope, &entries);
    let mut result = scope_warning.unwrap_or_default();
    result.push_str(&super::apply_budget(&tree, budget));
    if entries.is_empty() {
        if extensions.is_empty() {
            result.push_str("\nno matches\n");
        } else {
            let exts: Vec<String> = extensions.into_iter().take(10).collect();
            let _ = write!(
                result,
                "\nno matches; found extensions: {}\n",
                exts.join(", ")
            );
        }
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn no_cwd_refused() {
        // tilth_list requires cwd — the server cannot see the caller's shell cwd,
        // so a bare list must refuse with the teaching error rather than walk the
        // server's frozen process directory (the worktree bug).
        let args = serde_json::json!({ "patterns": ["*.rs"] });
        let err = tool_list(&args).unwrap_err();
        assert!(
            err.contains("cwd") && err.contains("absolute checkout directory"),
            "bare list must refuse without cwd: {err}"
        );
    }

    #[test]
    fn relative_scope_anchors_under_cwd() {
        // A relative scope anchored to cwd must resolve under cwd (not error).
        let tmp = tempfile::tempdir().unwrap();
        let sub = tmp.path().join("sub");
        std::fs::create_dir(&sub).unwrap();
        std::fs::write(sub.join("a.rs"), "fn a() {}\n").unwrap();
        let args = serde_json::json!({
            "patterns": ["*.rs"],
            "scope": "sub",
            "cwd": tmp.path().to_str().unwrap(),
        });
        let out = tool_list(&args).expect("relative scope + cwd resolves");
        assert!(
            out.contains("a.rs"),
            "expected listing under anchored cwd: {out}"
        );
    }

    #[test]
    fn invalid_glob_pattern_returns_error() {
        // An invalid glob must surface a specific error, not be silently
        // dropped from the matcher set.
        let tmp = tempfile::tempdir().unwrap();
        let args = serde_json::json!({
            "patterns": ["["],
            "cwd": tmp.path().to_str().unwrap(),
        });
        let err = tool_list(&args).unwrap_err();
        assert!(
            err.contains("invalid glob pattern") && err.contains('['),
            "expected invalid-glob error naming the pattern: {err}"
        );
    }
    #[test]
    fn tool_list_budget_truncates_output() {
        let tmp = tempfile::tempdir().unwrap();
        for i in 0..50 {
            std::fs::write(tmp.path().join(format!("f{i}.rs")), "fn f() {}\n").unwrap();
        }
        let args = serde_json::json!({
            "patterns": ["*.rs"],
            "cwd": tmp.path().to_str().unwrap(),
            "budget": 1,
        });
        let out = tool_list(&args).expect("tool_list should succeed");
        assert!(
            out.contains("... truncated"),
            "expected truncation note: {out}"
        );
    }

    #[test]
    fn tool_list_no_match_hints_available_extensions() {
        let tmp = tempfile::tempdir().unwrap();
        std::fs::write(tmp.path().join("a.rs"), "fn a() {}\n").unwrap();
        let args = serde_json::json!({
            "patterns": ["*.md"],
            "cwd": tmp.path().to_str().unwrap(),
        });
        let out = tool_list(&args).expect("tool_list should succeed");
        assert!(
            out.contains("no matches; found extensions:") && out.contains("rs"),
            "expected no-match extension hint: {out}"
        );
    }

    #[test]
    fn omitted_patterns_defaults_to_full_tree() {
        // Omitting `patterns` must behave exactly like patterns: ["*"] — the
        // zero-ceremony layout-tree call — not error as a missing parameter.
        let tmp = tempfile::tempdir().unwrap();
        let sub = tmp.path().join("sub");
        std::fs::create_dir(&sub).unwrap();
        std::fs::write(tmp.path().join("a.rs"), "fn a() {}\n").unwrap();
        std::fs::write(sub.join("b.md"), "# b\n").unwrap();
        let cwd = tmp.path().to_str().unwrap();
        let omitted =
            tool_list(&serde_json::json!({ "cwd": cwd })).expect("omitted patterns defaults");
        let explicit = tool_list(&serde_json::json!({ "patterns": ["*"], "cwd": cwd }))
            .expect("explicit [\"*\"] lists");
        assert_eq!(
            omitted, explicit,
            "omitted patterns must be byte-identical to explicit [\"*\"]"
        );
        assert!(
            omitted.contains("a.rs") && omitted.contains("b.md"),
            "default tree must include all files: {omitted}"
        );
    }

    #[test]
    fn non_array_patterns_returns_teaching_error() {
        // A present-but-wrong-shape `patterns` is a caller mistake, not an
        // omission — the error must teach the expected shape.
        let tmp = tempfile::tempdir().unwrap();
        let args = serde_json::json!({
            "patterns": "not-an-array",
            "cwd": tmp.path().to_str().unwrap(),
        });
        let err = tool_list(&args).unwrap_err();
        assert!(
            err.contains("array of glob strings"),
            "expected teaching error naming the expected shape: {err}"
        );
    }

    #[test]
    fn non_string_pattern_element_returns_teaching_error() {
        // Array with non-string elements is invalid too, with the same
        // shape-teaching error.
        let tmp = tempfile::tempdir().unwrap();
        let args = serde_json::json!({
            "patterns": [42],
            "cwd": tmp.path().to_str().unwrap(),
        });
        let err = tool_list(&args).unwrap_err();
        assert!(
            err.contains("array of glob strings"),
            "expected teaching error naming the expected shape: {err}"
        );
    }
}
