use std::path::Path;

use serde::Serialize;

use crate::types::estimate_tokens;

#[derive(Debug, Clone, Serialize)]
pub struct SectionMeta {
    pub section: String,
    pub truncated: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub truncated_at_line: Option<u32>,
}

#[derive(Debug)]
pub struct SectionsResult {
    pub text: String,
    pub any_truncated: bool,
    pub first_truncated_at: Option<u32>,
    pub metas: Vec<SectionMeta>,
}

/// Read multiple byte ranges in one pass under a shared token budget.
///
/// Earlier sections consume budget first; once it is exhausted, later
/// sections are truncated (via [`crate::budget::apply_with_info`]) and, if
/// nothing is left, replaced with an omission marker. Each emitted block is
/// prefixed with `## section: "<range>"` so callers can demultiplex results
/// back to their input order.
pub fn read_sections_with_budget(
    path: &Path,
    ranges: &[String],
    edit_mode: bool,
    budget: Option<u64>,
) -> SectionsResult {
    let mut blocks: Vec<String> = Vec::with_capacity(ranges.len());
    let mut metas: Vec<SectionMeta> = Vec::with_capacity(ranges.len());
    let mut remaining: u64 = budget.unwrap_or(u64::MAX);
    let mut any_truncated = false;
    let mut first_truncated_at: Option<u32> = None;

    for range in ranges {
        let raw_body = match super::read_section_body(path, range, edit_mode) {
            Ok(s) => s,
            Err(e) => format!("error reading section: {e}"),
        };
        let header = format!("## section: {range:?}");
        let block_full = format!("{header}\n\n{raw_body}");
        let tokens = estimate_tokens(block_full.len() as u64);

        let (emitted, section_truncated, at_line) = if remaining == u64::MAX || tokens <= remaining
        {
            remaining = remaining.saturating_sub(tokens);
            (block_full, false, None)
        } else if remaining == 0 {
            (
                format!("{header}\n\n... section omitted due to budget"),
                true,
                None,
            )
        } else {
            let (truncated_text, info) = crate::budget::apply_with_info(&block_full, remaining);
            remaining = 0;
            (truncated_text, true, info.map(|i| i.at_line))
        };

        if section_truncated {
            any_truncated = true;
            if first_truncated_at.is_none() {
                first_truncated_at = at_line;
            }
        }

        blocks.push(emitted);
        metas.push(SectionMeta {
            section: range.clone(),
            truncated: section_truncated,
            truncated_at_line: at_line,
        });
    }

    SectionsResult {
        text: blocks.join("\n\n"),
        any_truncated,
        first_truncated_at,
        metas,
    }
}
