use crate::types::estimate_tokens;

/// Metadata returned alongside a truncated string so callers can surface
/// "we cut at line N of M" without re-parsing the output.
pub struct TruncationInfo {
    /// 1-indexed line of the original output where the cut landed.
    pub at_line: u32,
    /// Total line count of the input before truncation. Lets MCP hosts
    /// render a "showing 1–N of M lines" hint without re-reading the file.
    pub original_line_count: u32,
}

/// Apply token budget to output. Works backwards from the cap:
/// 1. Reserve 50 tokens for header
/// 2. Truncate content at section boundaries to avoid broken output
/// 3. Never exceed the budget
pub fn apply(output: &str, budget: u64) -> String {
    apply_with_info(output, budget).0
}

/// Like [`apply`], but also returns truncation metadata when truncation
/// occurs. Returns `(text, None)` when the budget was sufficient, or
/// `(text, Some(info))` when the input was clipped.
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

    // Count newlines in the kept portion so callers can show "truncated at
    // line N" without scanning the result themselves, and on the full input
    // so they can show "of M".
    let kept = &output[..header_end + cut_point];
    let at_line = (kept.bytes().filter(|&b| b == b'\n').count() + 1) as u32;
    let original_line_count = (output.bytes().filter(|&b| b == b'\n').count() + 1) as u32;

    (
        result,
        Some(TruncationInfo {
            at_line,
            original_line_count,
        }),
    )
}
