use crate::types::estimate_tokens;

/// Metadata returned when truncation occurs.
pub struct TruncationInfo {
    /// Line number (1-indexed, relative to the output text) at which truncation happened.
    pub at_line: u32,
}

/// Apply token budget to output. Works backwards from the cap:
/// 1. Reserve 50 tokens for header
/// 2. Truncate content at section boundaries to avoid broken output
/// 3. Never exceed the budget
pub fn apply(output: &str, budget: u64) -> String {
    apply_with_info(output, budget).0
}

/// Like `apply`, but also returns truncation metadata when truncation occurs.
/// Returns `(truncated_text, Some(TruncationInfo))` when truncated,
/// `(original_text, None)` when the budget was sufficient.
pub fn apply_with_info(output: &str, budget: u64) -> (String, Option<TruncationInfo>) {
    let current = estimate_tokens(output.len() as u64);
    if current <= budget {
        return (output.to_string(), None);
    }

    let header_reserve = 50u64;
    let content_budget = budget.saturating_sub(header_reserve);
    let max_bytes = (content_budget * 4) as usize; // inverse of estimate_tokens

    // Find the first newline after the header (first line)
    let header_end = output.find('\n').unwrap_or(0);
    let header = &output[..header_end];
    let body = &output[header_end..];

    if body.len() <= max_bytes {
        return (output.to_string(), None);
    }

    let safe_max = body.floor_char_boundary(max_bytes);
    let truncated = &body[..safe_max];

    // Prefer section boundaries (\n\n##) to avoid cutting mid-match in search results
    let cut_point = truncated
        .rfind("\n\n##")
        .or_else(|| truncated.rfind("\n\n"))
        .or_else(|| truncated.rfind('\n'))
        .unwrap_or(max_bytes);

    let clean_body = &body[..cut_point];

    let omitted_bytes = output.len() - header_end - cut_point;
    let remaining_tokens = estimate_tokens(omitted_bytes as u64);
    let result = format!(
        "{header}{clean_body}\n\n... truncated ({remaining_tokens} tokens omitted, budget: {budget})"
    );

    // Count newlines in the kept portion (header + clean_body) to find truncation line.
    let kept = &output[..header_end + cut_point];
    let at_line = (kept.bytes().filter(|&b| b == b'\n').count() + 1) as u32;

    (result, Some(TruncationInfo { at_line }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn no_truncation_when_budget_sufficient() {
        let text = "line one\nline two\nline three\n";
        let (out, info) = apply_with_info(text, 10_000);
        assert_eq!(out, text);
        assert!(info.is_none());
    }

    #[test]
    fn truncation_returns_info() {
        // Build a text large enough that a tiny budget forces truncation.
        let long_body: String = (0..500).map(|i| format!("line {i}\n")).collect();
        let text = format!("header line\n{long_body}");
        let (out, info) = apply_with_info(&text, 10);
        assert!(info.is_some(), "expected truncation");
        let info = info.unwrap();
        // The sentinel must be present in the output.
        assert!(out.contains("... truncated"), "sentinel missing");
        // at_line must be positive.
        assert!(info.at_line > 0);
        // at_line must not exceed the number of lines kept.
        let kept_lines = out.lines().count();
        assert!(
            info.at_line as usize <= kept_lines + 1,
            "at_line {0} out of range (kept {kept_lines} lines)",
            info.at_line
        );
    }

    #[test]
    fn apply_wrapper_matches_apply_with_info() {
        let long_body: String = (0..500).map(|i| format!("line {i}\n")).collect();
        let text = format!("header line\n{long_body}");
        let (expected, _) = apply_with_info(&text, 10);
        assert_eq!(apply(&text, 10), expected);
    }
}
