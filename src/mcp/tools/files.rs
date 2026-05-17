use serde_json::Value;

use super::{apply_budget, resolve_scope};

pub(crate) fn tool_files(args: &Value) -> Result<String, String> {
    let (scope, scope_warning) = resolve_scope(args);
    let budget = args.get("budget").and_then(serde_json::Value::as_u64);

    let single = args.get("pattern").and_then(|v| v.as_str());
    let patterns_arr = args.get("patterns").and_then(|v| v.as_array());

    if single.is_some() && patterns_arr.is_some() {
        return Err("provide either pattern (single) or patterns (array), not both".into());
    }

    let patterns: Vec<&str> = if let Some(arr) = patterns_arr {
        if arr.is_empty() {
            return Err("patterns must contain at least one glob".into());
        }
        if arr.len() > 20 {
            return Err(format!(
                "patterns limited to 20 per call (got {})",
                arr.len()
            ));
        }
        arr.iter()
            .map(|v| v.as_str().ok_or("patterns must be an array of strings"))
            .collect::<Result<Vec<_>, _>>()?
    } else {
        vec![single.ok_or("missing required parameter: pattern (or use patterns for batch)")?]
    };

    let mut blocks = Vec::with_capacity(patterns.len());
    for p in &patterns {
        let block = crate::search::search_glob(p, &scope).map_err(|e| e.to_string())?;
        blocks.push(block);
    }
    let combined = blocks.join("\n\n");

    let mut result = scope_warning.unwrap_or_default();
    result.push_str(&apply_budget(combined, budget));
    Ok(result)
}
