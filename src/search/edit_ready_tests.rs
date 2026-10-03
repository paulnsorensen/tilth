//! Search output is edit-ready: every source line it prints is tagged and
//! recorded as seen, and no unprinted line is.

#![allow(clippy::format_push_string)]

use super::*;
use std::collections::BTreeSet;

/// A `fn host` of `total` lines; each line in `hits` calls `marker_call`.
fn host_source(total: usize, hits: &[usize]) -> String {
    let mut lines = vec![String::from("fn host() {")];
    for n in 2..total {
        if hits.contains(&n) {
            lines.push(format!("    marker_call({n});"));
        } else {
            lines.push(format!(
                "    let value_{n} = compute_something({n}, \"padding text so the outline is not inlined\");"
            ));
        }
    }
    lines.push(String::from("}"));
    lines.join("\n") + "\n"
}

fn search_with_session(
    dir: &Path,
    query: &str,
    expand: usize,
    budget: Option<u64>,
) -> (String, Session) {
    let result = symbol::search(query, dir, None, None, false).unwrap();
    let cache = OutlineCache::new();
    let session = Session::new();
    let bloom = crate::index::bloom::BloomFilterCache::new();
    let out = format_search_result(
        &result,
        &cache,
        Some(&session),
        &bloom,
        expand,
        format::EmptyHint::Merged,
        None,
        budget,
    )
    .unwrap();
    (out, session)
}

/// The `#TAG` printed in the header of `file`.
fn header_tag(out: &str, file: &str) -> u16 {
    let marker = format!("{file}#");
    let at = out
        .find(&marker)
        .unwrap_or_else(|| panic!("no {marker} in a header:\n{out}"))
        + marker.len();
    u16::from_str_radix(&out[at..at + 4], 16).unwrap()
}

fn snapshot(session: &Session, path: &Path, tag: u16) -> crate::edit::snapshots::Snapshot {
    session
        .snapshots()
        .by_tag(path, tag)
        .expect("snapshot for the printed tag")
}

fn seen_set(session: &Session, path: &Path, tag: u16) -> BTreeSet<u32> {
    snapshot(session, path, tag)
        .seen_lines
        .into_iter()
        .collect()
}

/// Line numbers of the `N | text` gutter lines inside the first code fence.
fn fence_lines(out: &str) -> BTreeSet<u32> {
    let body = out.split("```").nth(1).unwrap_or("");
    body.lines()
        .filter_map(|l| l.split_once(" | ")?.0.trim().parse().ok())
        .collect()
}

#[test]
fn outline_context_hit_is_tagged_and_records_the_printed_line() {
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("big.rs");
    std::fs::write(&path, host_source(70, &[40])).unwrap();

    let (out, session) = search_with_session(tmp.path(), "marker_call", 0, None);

    let tag = header_tag(&out, "big.rs");
    assert!(out.contains("marker_call(40);"), "{out}");
    assert_eq!(seen_set(&session, &path, tag), BTreeSet::from([40]));
    let snap = snapshot(&session, &path, tag);
    assert_eq!(snap.first_unseen_anchor([40]), None);
    assert_eq!(snap.first_unseen_anchor([41]), Some(41));
}

#[test]
fn expanded_fence_is_tagged_and_records_exactly_the_printed_lines() {
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("fence.rs");
    let mut src = String::new();
    for i in 0..30 {
        src.push_str(&format!("fn f{i}() {{}}\n"));
    }
    // Lines 31 and 32 are blank; the fence collapses the second one.
    src.push_str("\n\nfn target_fn() {\n    let a = 1;\n}\n");
    for i in 30..40 {
        src.push_str(&format!("fn f{i}() {{}}\n"));
    }
    std::fs::write(&path, src).unwrap();

    let (out, session) = search_with_session(tmp.path(), "target_fn", 1, None);

    let tag = header_tag(&out, "fence.rs");
    let printed = fence_lines(&out);
    assert!(printed.contains(&33), "{out}");
    assert!(!printed.contains(&32), "{out}");
    assert_eq!(seen_set(&session, &path, tag), printed);
    let snap = snapshot(&session, &path, tag);
    assert_eq!(snap.first_unseen_anchor([33, 34]), None);
    assert_eq!(snap.first_unseen_anchor([32]), Some(32));
}

#[test]
fn grouped_usages_print_each_matched_line_under_a_tagged_header() {
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("grouped.rs");
    std::fs::write(&path, host_source(70, &[30, 31])).unwrap();

    let (out, session) = search_with_session(tmp.path(), "marker_call", 0, None);

    let tag = header_tag(&out, "grouped.rs");
    assert!(out.contains("[2 usages"), "{out}");
    assert!(
        out.contains("\n30:") && out.contains("marker_call(30);"),
        "{out}"
    );
    assert!(
        out.contains("\n31:") && out.contains("marker_call(31);"),
        "{out}"
    );
    assert_eq!(seen_set(&session, &path, tag), BTreeSet::from([30, 31]));
    assert_eq!(
        snapshot(&session, &path, tag).first_unseen_anchor([32]),
        Some(32)
    );
}

#[test]
fn small_file_hit_prints_the_whole_file_and_records_every_line() {
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("small.rs");
    std::fs::write(&path, host_source(WHOLE_FILE_MAX_LINES as usize, &[10])).unwrap();

    let (out, session) = search_with_session(tmp.path(), "marker_call", 0, None);

    let tag = header_tag(&out, "small.rs");
    for n in 1..=WHOLE_FILE_MAX_LINES {
        assert!(out.contains(&format!("\n{n}:")), "line {n} missing:\n{out}");
    }
    let all: BTreeSet<u32> = (1..=WHOLE_FILE_MAX_LINES).collect();
    assert_eq!(seen_set(&session, &path, tag), all);
}

#[test]
fn file_over_the_threshold_keeps_the_current_output() {
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("large.rs");
    let total = WHOLE_FILE_MAX_LINES as usize + 1;
    std::fs::write(&path, host_source(total, &[10])).unwrap();

    let (out, session) = search_with_session(tmp.path(), "marker_call", 0, None);

    let tag = header_tag(&out, "large.rs");
    assert!(!out.contains("\n12:"), "{out}");
    assert_eq!(seen_set(&session, &path, tag), BTreeSet::from([10]));
}

#[test]
fn whole_file_is_skipped_when_the_budget_is_tight() {
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("tight.rs");
    std::fs::write(&path, host_source(WHOLE_FILE_MAX_LINES as usize, &[10])).unwrap();

    let (out, session) = search_with_session(tmp.path(), "marker_call", 0, Some(400));

    let tag = header_tag(&out, "tight.rs");
    assert!(!out.contains("\n12:"), "{out}");
    assert_eq!(seen_set(&session, &path, tag), BTreeSet::from([10]));
}

#[test]
fn second_hit_in_a_whole_file_does_not_reprint_it() {
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("twice.rs");
    let mut src = String::new();
    for name in ["first", "second"] {
        src.push_str(&format!("fn {name}() {{\n"));
        for n in 0..12 {
            src.push_str(&format!(
                "    let value_{n} = compute_something({n}, \"padding text so the outline is not inlined\");\n"
            ));
        }
        src.push_str("    marker_call(1);\n}\n");
    }
    std::fs::write(&path, src).unwrap();

    let (out, _session) = search_with_session(tmp.path(), "marker_call", 0, None);

    assert_eq!(out.matches("\n5:").count(), 1, "{out}");
}

// ---- press attack: request-cuts adversarial ----

/// A file whose every line carries a unique token `tok_<stem>_<n>`; line `hit`
/// also calls `marker_call`.
fn tokened_source(stem: &str, total: usize, hit: usize, eol: &str) -> String {
    (1..=total)
        .map(|n| {
            if n == hit {
                format!("// tok_{stem}_{n} marker_call();")
            } else {
                format!("// tok_{stem}_{n} padding padding padding padding padding padding")
            }
        })
        .collect::<Vec<_>>()
        .join(eol)
}

/// Every seen line of `path` must have its token in `out`.
fn assert_seen_lines_were_shown(out: &str, session: &Session, path: &Path, stem: &str, ctx: &str) {
    let name = path.file_name().unwrap().to_str().unwrap();
    let Some(head) = session.snapshots().head_tag(path) else {
        return;
    };
    let seen = snapshot(session, path, head).seen_lines;
    if !out.contains(&format!("{name}#")) {
        assert!(
            seen.is_empty(),
            "{ctx}: seen lines {seen:?} recorded for {name} but no tagged header printed:\n{out}"
        );
        return;
    }
    for n in seen {
        assert!(
            out.contains(&format!("tok_{stem}_{n} ")),
            "{ctx}: line {n} of {name} is seen but never printed:\n{out}"
        );
    }
}

#[test]
fn whole_file_crlf_prints_lines_without_cr_and_records_every_line() {
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("crlf.rs");
    std::fs::write(&path, tokened_source("crlf", 60, 30, "\r\n") + "\r\n").unwrap();
    let (out, session) = search_with_session(tmp.path(), "marker_call", 0, None);
    let tag = header_tag(&out, "crlf.rs");
    assert!(out.contains("\n60:// tok_crlf_60 "), "{out}");
    assert!(!out.contains('\r'), "CR leaked into the block: {out:?}");
    let all: BTreeSet<u32> = (1..=60).collect();
    assert_eq!(seen_set(&session, &path, tag), all);
}

#[test]
fn whole_file_without_trailing_newline_records_exactly_the_real_lines() {
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("nonl.rs");
    std::fs::write(&path, tokened_source("nonl", 60, 30, "\n")).unwrap();
    let (out, session) = search_with_session(tmp.path(), "marker_call", 0, None);
    let tag = header_tag(&out, "nonl.rs");
    let all: BTreeSet<u32> = (1..=60).collect();
    assert_eq!(seen_set(&session, &path, tag), all);
    let snap = snapshot(&session, &path, tag);
    assert_eq!(snap.first_unseen_anchor([61]), Some(61));
}

#[test]
fn sixty_one_lines_with_crlf_is_not_printed_whole() {
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("crlf61.rs");
    std::fs::write(&path, tokened_source("c61", 61, 30, "\r\n") + "\r\n").unwrap();
    let (out, session) = search_with_session(tmp.path(), "marker_call", 0, None);
    let tag = header_tag(&out, "crlf61.rs");
    assert!(!out.contains("tok_c61_31 "), "{out}");
    assert_eq!(seen_set(&session, &path, tag), BTreeSet::from([30]));
}

#[test]
fn long_line_file_over_byte_cap_with_few_lines_is_not_printed_whole() {
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("wide.rs");
    // 40 lines of ~700 bytes: > 60 * 400 bytes, <= 60 lines.
    let src = (1..=40)
        .map(|n| {
            let body = "x".repeat(700);
            if n == 20 {
                format!("// tok_wide_{n} marker_call(); {body}")
            } else {
                format!("// tok_wide_{n} {body}")
            }
        })
        .collect::<Vec<_>>()
        .join("\n");
    std::fs::write(&path, src).unwrap();
    let (out, session) = search_with_session(tmp.path(), "marker_call", 0, None);
    let tag = header_tag(&out, "wide.rs");
    assert!(!out.contains("tok_wide_21 "), "{out}");
    assert_eq!(seen_set(&session, &path, tag), BTreeSet::from([20]));
}

#[test]
fn dropped_whole_file_block_records_no_seen_lines_for_its_file() {
    // Several small files each get a whole-file block; sweep budgets so some
    // blocks are trimmed. A seen line must always have been printed.
    for budget in [60u64, 100, 150, 200, 300, 400, 600, 800, 1200, 2000, 4000] {
        let tmp = tempfile::tempdir().unwrap();
        let mut paths = Vec::new();
        for i in 0..5 {
            let stem = format!("f{i}");
            let path = tmp.path().join(format!("{stem}.rs"));
            std::fs::write(&path, tokened_source(&stem, 40, 20, "\n") + "\n").unwrap();
            paths.push((stem, path));
        }
        let (out, session) = search_with_session(tmp.path(), "marker_call", 0, Some(budget));
        for (stem, path) in &paths {
            assert_seen_lines_were_shown(&out, &session, path, stem, &format!("budget {budget}"));
        }
    }
}

#[test]
fn trimmed_large_file_hits_record_only_printed_match_lines() {
    for budget in [60u64, 100, 150, 200, 300, 400, 600, 1000, 3000] {
        for expand in [0usize, 1, 2] {
            let tmp = tempfile::tempdir().unwrap();
            let mut paths = Vec::new();
            for i in 0..4 {
                let stem = format!("g{i}");
                let path = tmp.path().join(format!("{stem}.rs"));
                std::fs::write(&path, tokened_source(&stem, 90, 45, "\n") + "\n").unwrap();
                paths.push((stem, path));
            }
            let (out, session) =
                search_with_session(tmp.path(), "marker_call", expand, Some(budget));
            for (stem, path) in &paths {
                assert_seen_lines_were_shown(
                    &out,
                    &session,
                    path,
                    stem,
                    &format!("budget {budget} expand {expand}"),
                );
            }
        }
    }
}

#[test]
fn expanded_fence_never_marks_stripped_comment_lines_as_seen() {
    for budget in [None, Some(400u64), Some(1500)] {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("fenced.rs");
        let mut src = String::new();
        for i in 0..30 {
            src.push_str(&format!("fn pre{i}() {{}}\n"));
        }
        src.push_str("fn target_fn() {\n");
        for i in 0..40 {
            src.push_str(&format!("    // tok_fenced_{} comment\n", 32 + i));
            src.push_str(&format!("    let v{i} = {i};\n"));
        }
        src.push_str("}\n");
        std::fs::write(&path, src).unwrap();
        let (out, session) = search_with_session(tmp.path(), "target_fn", 1, budget);
        if !out.contains("fenced.rs#") {
            let head = session.snapshots().head_tag(&path);
            let seen = head.map(|t| seen_set(&session, &path, t));
            assert!(
                seen.is_none_or(|s| s.is_empty()),
                "dropped block still seen: {out}"
            );
            continue;
        }
        let tag = header_tag(&out, "fenced.rs");
        let printed = fence_lines(&out);
        let seen = seen_set(&session, &path, tag);
        assert_eq!(seen, printed, "budget {budget:?}: {out}");
    }
}

#[test]
fn expanded_fence_with_long_body_gap_marks_only_printed_lines() {
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("longfn.rs");
    let mut src = String::from("fn target_fn() {\n");
    for i in 0..300 {
        src.push_str(&format!("    let value_{i} = compute({i});\n"));
    }
    src.push_str("}\n");
    std::fs::write(&path, src).unwrap();
    let (out, session) = search_with_session(tmp.path(), "target_fn", 1, None);
    let tag = header_tag(&out, "longfn.rs");
    let printed = fence_lines(&out);
    assert_eq!(seen_set(&session, &path, tag), printed, "{out}");
    let snap = snapshot(&session, &path, tag);
    for n in 1..=302u32 {
        let shown = printed.contains(&n);
        assert_eq!(
            snap.first_unseen_anchor([n]).is_none(),
            shown,
            "line {n} shown={shown}"
        );
    }
}

#[test]
fn grouped_usages_do_not_authorize_the_line_between_two_matches() {
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("gap.rs");
    std::fs::write(&path, host_source(70, &[30, 32, 34])).unwrap();
    let (out, session) = search_with_session(tmp.path(), "marker_call", 0, None);
    let tag = header_tag(&out, "gap.rs");
    assert_eq!(seen_set(&session, &path, tag), BTreeSet::from([30, 32, 34]));
    let snap = snapshot(&session, &path, tag);
    assert_eq!(snap.first_unseen_anchor([31]), Some(31));
    assert_eq!(snap.first_unseen_anchor([33]), Some(33));
    assert_eq!(snap.first_unseen_anchor([29]), Some(29));
}

#[test]
fn outline_context_hit_does_not_authorize_adjacent_lines() {
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("ctx.rs");
    std::fs::write(&path, host_source(90, &[45])).unwrap();
    let (out, session) = search_with_session(tmp.path(), "marker_call", 0, None);
    let tag = header_tag(&out, "ctx.rs");
    let snap = snapshot(&session, &path, tag);
    assert_eq!(snap.first_unseen_anchor([44]), Some(44));
    assert_eq!(snap.first_unseen_anchor([46]), Some(46));
    assert_eq!(snap.first_unseen_anchor([1]), Some(1));
}
