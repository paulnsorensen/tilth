use serde_json::Value;

pub(in crate::mcp) fn tool_diff(args: &Value) -> Result<String, String> {
    // Every git-backed source (working/staged/ref/log) runs against `cwd`, so
    // the diff reflects the caller's repository rather than the server's frozen
    // process dir. The file-path params (`patch`, `a`, `b`) are filesystem
    // reads, so relative spellings anchor under cwd like every other
    // path-taking tool.
    let cwd = super::require_cwd(args)?;
    let source = args.get("source").and_then(|v| v.as_str());
    let scope = args.get("scope").and_then(|v| v.as_str());
    let anchor = |key: &str| -> Result<Option<String>, String> {
        match args.get(key).and_then(|v| v.as_str()) {
            Some(raw) => {
                let anchored = super::resolve_anchored(std::path::Path::new(raw), cwd)?;
                Ok(Some(anchored.to_string_lossy().into_owned()))
            }
            None => Ok(None),
        }
    };
    let a = anchor("a")?;
    let b = anchor("b")?;
    let patch = anchor("patch")?;
    let log = args.get("log").and_then(|v| v.as_str());
    let search = args.get("search").and_then(|v| v.as_str());
    let blast = args.get("blast").and_then(Value::as_bool).unwrap_or(false);
    let budget = args
        .get("budget")
        .and_then(Value::as_u64)
        .unwrap_or(crate::budget::DEFAULT_BUDGET);

    let diff_source =
        crate::diff::resolve_source(source, a.as_deref(), b.as_deref(), patch.as_deref(), log)?;
    let result = crate::diff::diff(&diff_source, scope, search, blast, Some(budget), cwd)?;
    Ok(crate::budget::apply(&result, budget))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn missing_cwd_refused() {
        // A call with no cwd must refuse with the teaching error before any
        // diff work.
        let args = serde_json::json!({});
        let err = tool_diff(&args).unwrap_err();
        assert!(
            err.contains("cwd") && err.contains("absolute checkout directory"),
            "diff without cwd must refuse with the teaching error: {err}"
        );
    }

    #[test]
    fn relative_cwd_refused() {
        // Git-based sources now diff against cwd, and the value is validated
        // up front regardless: a relative cwd is refused.
        let args = serde_json::json!({ "cwd": "relative/dir", "source": "working" });
        let err = tool_diff(&args).unwrap_err();
        assert!(
            err.contains("relative") && err.contains("absolute checkout directory"),
            "diff with a relative cwd must refuse: {err}"
        );
    }

    #[test]
    fn relative_patch_anchors_under_cwd() {
        // A relative `patch` path must read under cwd, never the server's
        // process dir — the wrong-checkout hazard the posture exists to kill.
        let tmp = tempfile::tempdir().unwrap();
        let patch = "diff --git a/f.txt b/f.txt\n\
                     index 0000000..1111111 100644\n\
                     --- a/f.txt\n\
                     +++ b/f.txt\n\
                     @@ -1 +1 @@\n\
                     -old\n\
                     +new\n";
        std::fs::write(tmp.path().join("fix.patch"), patch).unwrap();
        let args = serde_json::json!({
            "cwd": tmp.path().to_str().unwrap(),
            "patch": "fix.patch",
        });
        let out = tool_diff(&args).expect("relative patch under cwd must resolve");
        assert!(
            out.contains("f.txt"),
            "anchored patch must be parsed into the diff overview: {out}"
        );
    }

    #[test]
    fn relative_patch_dotdot_refused() {
        // `..` traversal in a relative file-path param is refused, same as
        // every other anchored path.
        let args = serde_json::json!({
            "cwd": "/abs/checkout",
            "patch": "../escape.patch",
        });
        let err = tool_diff(&args).unwrap_err();
        assert!(
            err.contains("..") && err.contains("escapes"),
            "relative `..` patch path must be refused: {err}"
        );
    }

    #[test]
    fn relative_file_pair_anchors_under_cwd() {
        // Relative `a`/`b` file paths anchor under cwd before the two-file
        // diff reads them.
        let tmp = tempfile::tempdir().unwrap();
        std::fs::write(tmp.path().join("a.txt"), "one\n").unwrap();
        std::fs::write(tmp.path().join("b.txt"), "two\n").unwrap();
        let args = serde_json::json!({
            "cwd": tmp.path().to_str().unwrap(),
            "a": "a.txt",
            "b": "b.txt",
        });
        let out = tool_diff(&args).expect("relative a/b under cwd must resolve");
        assert!(
            !out.is_empty(),
            "anchored two-file diff must produce output"
        );
    }
}
