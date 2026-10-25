//! Receipt and teaching-error behavior of `tilth_write`: the result must carry
//! everything the next edit of the same file needs, and common failures must say
//! how to fix the call.

use std::path::Path;
use std::sync::Arc;

use serde_json::{json, Value};

use super::tool_write;
use crate::cache::OutlineCache;
use crate::index::bloom::BloomFilterCache;
use crate::session::Session;

fn rows(count: u32) -> String {
    use std::fmt::Write as _;
    (1..=count).fold(String::new(), |mut acc, i| {
        writeln!(acc, "row-{i:03}").unwrap();
        acc
    })
}

fn read(session: &Session, root: &Path, spec: &str) -> String {
    crate::mcp::tools::tool_read(
        &json!({"paths": [spec], "cwd": root.to_str().unwrap()}),
        &OutlineCache::new(),
        session,
    )
    .expect("read")
}

fn tag_of(output: &str) -> String {
    let at = output.find("f.txt#").expect("[f.txt#TAG] header") + "f.txt#".len();
    output[at..at + 4].to_string()
}

fn write(session: &Session, root: &Path, tag: Option<&str>, ops: Value) -> Result<String, String> {
    let mut section = json!({"path": "f.txt"});
    section["ops"] = ops;
    if let Some(tag) = tag {
        section["tag"] = json!(tag);
    }
    let bloom = Arc::new(BloomFilterCache::new());
    tool_write(
        &json!({"edits": [section], "cwd": root.to_str().unwrap()}),
        session,
        &bloom,
        &OutlineCache::new(),
    )
}

fn fixture(count: u32) -> (tempfile::TempDir, Session, String) {
    fixture_shown(count, count)
}

/// A `total`-line file of which the session displayed only lines `1..=shown`.
fn fixture_shown(total: u32, shown: u32) -> (tempfile::TempDir, Session, String) {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(dir.path().join("f.txt"), rows(total)).unwrap();
    let session = Session::new();
    let tag = tag_of(&read(&session, dir.path(), &format!("f.txt#1-{shown}")));
    (dir, session, tag)
}

#[test]
fn receipt_is_slim_and_names_the_changed_line() {
    let (dir, session, tag) = fixture(200);
    let receipt = write(
        &session,
        dir.path(),
        Some(&tag),
        json!([{"op": "replace_text", "old": "row-100", "new": "WRITTEN-TEXT"}]),
    )
    .expect("write");
    assert!(receipt.contains("[f.txt#"), "{receipt}");
    assert!(receipt.contains("changed 100"), "{receipt}");
    assert!(
        !receipt.contains("WRITTEN-TEXT"),
        "echoed content: {receipt}"
    );
    assert!(!receipt.contains("100:"), "numbered row: {receipt}");
    assert!(receipt.len() < 300, "{} chars: {receipt}", receipt.len());
}

#[test]
fn receipt_merges_adjacent_lines_into_ranges() {
    let (dir, session, tag) = fixture(100);
    let receipt = write(
        &session,
        dir.path(),
        Some(&tag),
        json!([
            {"op": "replace", "start": 10, "end": 12, "content": "a\nb\nc"},
            {"op": "replace_text", "old": "row-080", "new": "ROW-080"},
        ]),
    )
    .expect("write");
    assert!(receipt.contains("changed 10-12, 80"), "{receipt}");
}

#[test]
fn authored_line_is_editable_from_the_receipt_tag_without_a_read() {
    let (dir, session, tag) = fixture(200);
    let receipt = write(
        &session,
        dir.path(),
        Some(&tag),
        json!([{"op": "replace_text", "old": "row-100", "new": "WRITTEN-TEXT"}]),
    )
    .expect("first write");
    let second = write(
        &session,
        dir.path(),
        Some(&tag_of(&receipt)),
        json!([{"op": "replace_text", "old": "WRITTEN-TEXT", "new": "again"}]),
    )
    .expect("authored line needs no read");
    assert!(second.contains("applied"), "{second}");
    let text = std::fs::read_to_string(dir.path().join("f.txt")).unwrap();
    assert!(text.contains("again") && !text.contains("WRITTEN-TEXT"));
}

#[test]
fn unshown_neighbour_is_refused_from_the_receipt_tag() {
    let (dir, session, tag) = fixture_shown(200, 100);
    let receipt = write(
        &session,
        dir.path(),
        Some(&tag),
        json!([{"op": "replace_text", "old": "row-100", "new": "WRITTEN-TEXT"}]),
    )
    .expect("first write");
    let err = write(
        &session,
        dir.path(),
        Some(&tag_of(&receipt)),
        json!([{"op": "replace_text", "old": "row-101", "new": "X"}]),
    )
    .expect_err("the neighbour was never displayed in this version");
    assert!(err.contains("never displayed"), "{err}");
}

#[test]
fn pure_deletion_reports_the_deletion_point_and_marks_nothing_seen() {
    let (dir, session, tag) = fixture_shown(200, 100);
    let receipt = write(
        &session,
        dir.path(),
        Some(&tag),
        json!([{"op": "delete", "start": 100, "end": 100}]),
    )
    .expect("delete");
    assert!(receipt.contains("deleted at 100"), "{receipt}");
    assert!(!receipt.contains("changed"), "{receipt}");
    let err = write(
        &session,
        dir.path(),
        Some(&tag_of(&receipt)),
        json!([{"op": "replace_text", "old": "row-101", "new": "X"}]),
    )
    .expect_err("the line at the deletion point was not shown");
    assert!(err.contains("never displayed"), "{err}");
}

#[test]
fn lines_read_before_the_write_stay_editable_at_their_new_number() {
    let (dir, session, tag) = fixture_shown(200, 60);
    let receipt = write(
        &session,
        dir.path(),
        Some(&tag),
        json!([{"op": "insert_after", "line": 30, "content": "new-a\nnew-b"}]),
    )
    .expect("insert");
    assert!(receipt.contains("changed 31-32"), "{receipt}");
    let new_tag = tag_of(&receipt);
    let second = write(
        &session,
        dir.path(),
        Some(&new_tag),
        json!([{"op": "replace_text", "old": "row-050", "new": "moved-050"}]),
    )
    .expect("old line 50 (now 52) was read and is unchanged");
    assert!(second.contains("applied"), "{second}");
}

#[test]
fn lines_never_shown_before_the_write_stay_refused() {
    let (dir, session, tag) = fixture_shown(200, 60);
    let receipt = write(
        &session,
        dir.path(),
        Some(&tag),
        json!([{"op": "insert_after", "line": 30, "content": "new-a\nnew-b"}]),
    )
    .expect("insert");
    let err = write(
        &session,
        dir.path(),
        Some(&tag_of(&receipt)),
        json!([{"op": "replace_text", "old": "row-150", "new": "X"}]),
    )
    .expect_err("row-150 was never displayed");
    assert!(err.contains("never displayed"), "{err}");
}

#[test]
fn lines_after_a_deletion_shift_up_and_stay_editable() {
    let (dir, session, tag) = fixture_shown(200, 60);
    let receipt = write(
        &session,
        dir.path(),
        Some(&tag),
        json!([{"op": "delete", "start": 20, "end": 21}]),
    )
    .expect("delete");
    assert!(receipt.contains("deleted at 20"), "{receipt}");
    let second = write(
        &session,
        dir.path(),
        Some(&tag_of(&receipt)),
        json!([{"op": "replace_text", "old": "row-040", "new": "X"}]),
    )
    .expect("row-040 (now line 38) was read and is unchanged");
    assert!(second.contains("applied"), "{second}");
}

#[test]
fn missing_tag_error_states_the_current_tag_when_the_file_was_displayed() {
    let (dir, session, tag) = fixture(100);
    let err = write(
        &session,
        dir.path(),
        None,
        json!([{"op": "replace_text", "old": "row-050", "new": "ROW-050"}]),
    )
    .expect_err("tagless text swap is rejected");
    assert!(err.contains(&format!("tag \"{tag}\"")), "{err}");
    assert!(err.contains("1-100"), "names the displayed lines: {err}");
    let text = std::fs::read_to_string(dir.path().join("f.txt")).unwrap();
    assert_eq!(text, rows(100), "a rejected edit must not write");
}

#[test]
fn missing_tag_error_stays_generic_for_a_file_never_displayed() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(dir.path().join("f.txt"), rows(10)).unwrap();
    let err = write(
        &Session::new(),
        dir.path(),
        None,
        json!([{"op": "replace_text", "old": "row-005", "new": "X"}]),
    )
    .expect_err("rejected");
    assert!(err.contains("requires the [path#TAG]"), "{err}");
    assert!(
        !err.contains("Retry with tag"),
        "must not invent a tag: {err}"
    );
}

#[test]
fn unseen_line_error_names_the_exact_read_range() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(dir.path().join("f.txt"), rows(100)).unwrap();
    let session = Session::new();
    let tag = tag_of(&read(&session, dir.path(), "f.txt#1-5"));
    let err = write(
        &session,
        dir.path(),
        Some(&tag),
        json!([{"op": "replace", "start": 50, "end": 50, "content": "X"}]),
    )
    .expect_err("line 50 was never displayed");
    assert!(
        err.contains("f.txt#") && err.contains("to cover line 50"),
        "{err}"
    );
}

#[test]
fn control_character_error_names_the_field_and_the_escape() {
    let line = "{\"params\":{\"edits\":[{\"ops\":[{\"old\":\"a\tb\"}]}]}}";
    let err = serde_json::from_str::<Value>(line).unwrap_err();
    let message = crate::edit::json::describe_json_parse_error(line, &err);
    assert!(message.contains("TAB"), "{message}");
    assert!(message.contains("field \"old\""), "{message}");
    assert!(message.contains("\\t"), "{message}");
}

#[test]
fn ambiguous_and_overlap_errors_say_how_to_fix_the_call() {
    let (dir, session, tag) = fixture(100);
    let ambiguous = write(
        &session,
        dir.path(),
        Some(&tag),
        json!([{"op": "replace_text", "old": "row-0", "new": "x"}]),
    )
    .expect_err("ambiguous");
    assert!(ambiguous.contains("all:true"), "{ambiguous}");
    let overlap = write(
        &session,
        dir.path(),
        Some(&tag),
        json!([
            {"op": "replace", "start": 5, "end": 7, "content": "a"},
            {"op": "replace", "start": 6, "end": 8, "content": "b"},
        ]),
    )
    .expect_err("overlap");
    assert!(overlap.contains("lines 5-7 and 6-8"), "{overlap}");
}
