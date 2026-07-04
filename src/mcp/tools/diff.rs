use serde_json::Value;

pub(in crate::mcp) fn tool_diff(args: &Value) -> Result<String, String> {
    // cwd is required on every path-taking tool for schema consistency; git
    // diff runs in the server's project directory, so the value is validated
    // (absolute, present) but not otherwise consumed here.
    super::require_cwd(args)?;
    let source = args.get("source").and_then(|v| v.as_str());
    let scope = args.get("scope").and_then(|v| v.as_str());
    let a = args.get("a").and_then(|v| v.as_str());
    let b = args.get("b").and_then(|v| v.as_str());
    let patch = args.get("patch").and_then(|v| v.as_str());
    let log = args.get("log").and_then(|v| v.as_str());
    let search = args.get("search").and_then(|v| v.as_str());
    let blast = args.get("blast").and_then(Value::as_bool).unwrap_or(false);
    let expand = args.get("expand").and_then(Value::as_u64).unwrap_or(0) as usize;
    let budget = args
        .get("budget")
        .and_then(Value::as_u64)
        .unwrap_or(crate::budget::DEFAULT_BUDGET);

    let diff_source = crate::diff::resolve_source(source, a, b, patch, log)?;
    let result = crate::diff::diff(&diff_source, scope, search, blast, expand, Some(budget))?;
    Ok(crate::budget::apply(&result, budget))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn missing_cwd_refused() {
        // tilth_diff gained cwd for schema consistency only — git diff runs in
        // the server dir. But the value is still validated: a call with no cwd
        // must refuse with the teaching error before any diff work.
        let args = serde_json::json!({});
        let err = tool_diff(&args).unwrap_err();
        assert!(
            err.contains("cwd") && err.contains("absolute checkout directory"),
            "diff without cwd must refuse with the teaching error: {err}"
        );
    }

    #[test]
    fn relative_cwd_refused() {
        // The validated-but-unused seam still enforces absoluteness: a relative
        // cwd is refused even though the value is not otherwise consumed.
        let args = serde_json::json!({ "cwd": "relative/dir", "source": "working" });
        let err = tool_diff(&args).unwrap_err();
        assert!(
            err.contains("relative") && err.contains("absolute checkout directory"),
            "diff with a relative cwd must refuse: {err}"
        );
    }
}
