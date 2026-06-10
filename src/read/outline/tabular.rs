/// CSV/TSV outline: column headers + row count + first 5 + last 3 rows.
/// Uses memchr for line counting on the raw bytes, then only collects
/// the head/tail slices needed for display.
pub fn outline(content: &str, _max_lines: usize) -> (String, bool) {
    let buf = content.as_bytes();
    if buf.is_empty() {
        return ("(empty)".to_string(), false);
    }

    // Count lines via memchr — O(n) SIMD scan, no Vec allocation
    let newline_count = memchr::memchr_iter(b'\n', buf).count();
    // Subtract one extra when content ends with \n (trailing newline is not a data row)
    let data_rows = if buf.last() == Some(&b'\n') {
        newline_count.saturating_sub(1)
    } else {
        newline_count
    };

    // We still need to index into lines for head/tail display,
    // but only collect offsets, not full line slices
    let lines: Vec<&str> = content.lines().collect();

    let mut out = Vec::new();

    // Header
    out.push(format!("columns: {}", lines[0]));
    out.push(format!("rows: {data_rows}"));
    out.push(String::new());

    // First 5 data rows
    let head_end = 6.min(lines.len()); // header + 5 rows
    for line in &lines[1..head_end] {
        out.push(line.to_string());
    }

    // Gap indicator + last 3 rows
    let total = newline_count + 1; // total line count including header
    if total > 9 {
        out.push(format!("... {} rows omitted", total - 9));
        out.push(String::new());
        let tail_start = lines.len().saturating_sub(3);
        for line in &lines[tail_start..] {
            out.push(line.to_string());
        }
    } else if lines.len() > head_end {
        for line in &lines[head_end..] {
            out.push(line.to_string());
        }
    }

    (out.join("\n"), false) // tabular outline is never line-capped
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Regression: trailing-newline CSV must not inflate the data row count.
    /// `a,b\n1,2\n3,4\n` has 2 data rows, not 3.
    #[test]
    fn rows_count_trailing_newline() {
        let csv = "a,b\n1,2\n3,4\n";
        let (result, _) = outline(csv, 100);
        assert!(
            result.contains("rows: 2"),
            "trailing-newline CSV must report 2 data rows, got:\n{result}"
        );
    }

    /// Without a trailing newline the count must still be correct.
    #[test]
    fn rows_count_no_trailing_newline() {
        let csv = "a,b\n1,2\n3,4";
        let (result, _) = outline(csv, 100);
        assert!(
            result.contains("rows: 2"),
            "no-trailing-newline CSV must report 2 data rows, got:\n{result}"
        );
    }

    #[test]
    fn empty_input() {
        let (result, _) = outline("", 100);
        assert_eq!(result, "(empty)");
    }
}
