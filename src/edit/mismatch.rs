//! Rejection error when a section tag doesn't match live content and recovery
//! failed. Ported from oh-my-pi `packages/hashline/src/mismatch.ts`: two shapes,
//! `Drift` (tag was minted this session but content moved on) vs `Fabricated`
//! (tag never seen — hallucinated or carried over from a prior session).
//!
//! PR2 maps this onto a new `TilthError` variant; PR1 keeps it standalone.

#![allow(dead_code)]

/// Render displayed ranges as `A-B, C-D` (a single-line range as `N`),
/// nearest the `anchor` line first, capped to 4 entries plus a `… +K more`
/// tail when more were displayed.
pub(super) fn format_ranges(ranges: &[(u32, u32)], anchor: u32) -> String {
    const CAP: usize = 4;
    let mut sorted: Vec<(u32, u32)> = ranges.to_vec();
    sorted.sort_by_key(|(lo, hi)| {
        if anchor < *lo {
            lo - anchor
        } else if anchor > *hi {
            anchor - hi
        } else {
            0
        }
    });
    let rendered = sorted
        .iter()
        .take(CAP)
        .map(|(lo, hi)| {
            if lo == hi {
                lo.to_string()
            } else {
                format!("{lo}-{hi}")
            }
        })
        .collect::<Vec<_>>()
        .join(", ");
    if sorted.len() > CAP {
        format!("{rendered}, \u{2026} +{} more", sorted.len() - CAP)
    } else {
        rendered
    }
}

fn unseen_message(
    path: &str,
    line: u32,
    displayed: &[(u32, u32)],
    reread: (u32, u32),
    reads: &[(u32, u32)],
) -> String {
    let p = crate::format::display_path_str(path);
    let ranges = format_ranges(displayed, line);
    let head = format!(
        "Edit rejected for {p}: line {line} was never displayed under this tag (displayed: {ranges})."
    );
    if reads.is_empty() {
        return format!(
            "{head} Re-read {p}#{}-{} to cover line {line}.",
            reread.0, reread.1
        );
    }
    let paths: Vec<String> = reads
        .iter()
        .map(|(lo, hi)| format!("{p}#{lo}-{hi}"))
        .collect();
    let paths = serde_json::Value::from(paths);
    format!(
        "{head} tilth_read paths {paths} shows every match range in the current file; \
         use the tag returned by that read when retrying."
    )
}

/// A tag/content mismatch that recovery could not resolve.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum MismatchError {
    /// The expected tag was recorded this session, but the live file hashes to
    /// something else and recovery declined the merge.
    #[error(
        "Edit rejected for {p}: file changed between read and edit. \
             Section is bound to #{expected_tag:04X}, but the current file hashes to \
             #{actual_tag:04X}. Re-read to refresh the tag before retrying.",
        p = crate::format::display_path_str(path)
    )]
    Drift {
        path: String,
        expected_tag: u16,
        actual_tag: u16,
    },
    /// The expected tag was never recorded — likely a hallucinated tag or one
    /// reused from a prior session.
    #[error(
        "Edit rejected for {p}: tag #{expected_tag:04X} is not from this session. \
             Re-read the file to copy a current [path#tag] header — never invent a tag.",
        p = crate::format::display_path_str(path)
    )]
    Fabricated { path: String, expected_tag: u16 },
    /// An edit anchored on a line the read never displayed under this tag. Names
    /// the ranges that WERE displayed and the exact re-read that would cover the
    /// unseen line, so the fix is one bounded read rather than a guess. `reread`
    /// is the smallest span joining `line` to the nearest displayed range, capped
    /// at 60 lines. Multi-match ops (`replace_text all`, `rewrite`) set `reads`:
    /// one small range per unseen match for one batched `tilth_read`, which
    /// replaces the re-read. Single-anchor ops leave `reads` empty.
    #[error("{}", unseen_message(path, *line, displayed, (*reread_lo, *reread_hi), reads))]
    UnseenAnchor {
        path: String,
        line: u32,
        displayed: Vec<(u32, u32)>,
        reread_lo: u32,
        reread_hi: u32,
        reads: Vec<(u32, u32)>,
    },
    /// A `replace_text` anchor did not resolve against the live file. The
    /// specific match failure is what the caller must act on — reporting it as
    /// generic drift sends the agent into a re-read loop that cannot help.
    /// Carries the [`ApplyError`] itself so callers can branch on which kind of
    /// match failure it was, rather than parsing its rendered text.
    #[error(
        "Edit rejected for {p}: {source}. The file also changed since the read \
             that minted this tag — re-read to refresh it.",
        p = crate::format::display_path_str(path)
    )]
    TextMatch {
        path: String,
        #[source]
        source: super::apply::ApplyError,
    },
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn drift_message_names_both_tags() {
        let e = MismatchError::Drift {
            path: "src/a.rs".into(),
            expected_tag: 0x1A2B,
            actual_tag: 0x3C4D,
        };
        let s = e.to_string();
        assert!(s.contains("#1A2B"), "{s}");
        assert!(s.contains("#3C4D"), "{s}");
        assert!(s.contains("changed between read and edit"), "{s}");
    }

    #[test]
    fn unseen_anchor_message_names_displayed_ranges_and_reread() {
        let e = MismatchError::UnseenAnchor {
            path: "src/a.rs".into(),
            line: 2823,
            displayed: vec![(2655, 2700), (3250, 3270)],
            reread_lo: 2764,
            reread_hi: 2823,
            reads: Vec::new(),
        };
        assert_eq!(
            e.to_string(),
            "Edit rejected for src/a.rs: line 2823 was never displayed under this tag \
             (displayed: 2655-2700, 3250-3270). Re-read src/a.rs#2764-2823 to cover line 2823."
        );
    }

    #[test]
    fn unseen_match_message_names_one_batched_read() {
        let e = MismatchError::UnseenAnchor {
            path: "src/a.go".into(),
            line: 39,
            displayed: vec![(1, 36)],
            reread_lo: 1,
            reread_hi: 39,
            reads: vec![(39, 39), (120, 122)],
        };
        assert_eq!(
            e.to_string(),
            "Edit rejected for src/a.go: line 39 was never displayed under this tag \
             (displayed: 1-36). tilth_read paths \
             [\"src/a.go#39-39\",\"src/a.go#120-122\"] shows every match range in the current \
             file; use the tag returned by that read when retrying."
        );
    }

    #[test]
    fn format_ranges_caps_to_four_nearest_with_more_tail() {
        let ranges: Vec<(u32, u32)> = vec![
            (10, 10),
            (20, 20),
            (30, 30),
            (40, 40),
            (50, 50),
            (60, 60),
            (70, 70),
            (80, 80),
        ];
        assert_eq!(
            format_ranges(&ranges, 45),
            "40, 50, 30, 60, \u{2026} +4 more"
        );
    }

    #[test]
    fn fabricated_message_flags_unknown_tag() {
        let e = MismatchError::Fabricated {
            path: "src/a.rs".into(),
            expected_tag: 0x9F3E,
        };
        let s = e.to_string();
        assert!(s.contains("not from this session"), "{s}");
        assert!(s.contains("#9F3E"), "{s}");
    }
}
