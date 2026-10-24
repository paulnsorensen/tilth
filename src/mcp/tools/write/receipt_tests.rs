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
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(dir.path().join("f.txt"), rows(count)).unwrap();
    let session = Session::new();
    let tag = tag_of(&read(&session, dir.path(), "f.txt#1-100"));
    (dir, session, tag)
}

#[test]
fn receipt_shows_every_changed_range_and_authorizes_a_second_write() {
    let (dir, session, tag) = fixture(100);
    let receipt = write(
        &session,
        dir.path(),
        Some(&tag),
        json!([
            {"op": "replace_text", "old": "row-010", "new": "ROW-010"},
            {"op": "replace_text", "old": "row-080", "new": "ROW-080"},
        ]),
    )
    .expect("first write");
    assert!(receipt.contains("10:ROW-010"), "{receipt}");
    assert!(
        receipt.contains("80:ROW-080"),
        "every changed range, not the first only: {receipt}"
    );

    let second = write(
        &session,
        dir.path(),
        Some(&tag_of(&receipt)),
        json!([
            {"op": "replace_text", "old": "ROW-080", "new": "again-080"},
            {"op": "replace_text", "old": "row-011", "new": "again-011"},
        ]),
    )
    .expect("second write from the receipt alone");
    assert!(second.contains("applied"), "{second}");
    let text = std::fs::read_to_string(dir.path().join("f.txt")).unwrap();
    assert!(text.contains("ROW-010") && text.contains("again-080") && text.contains("again-011"));
}

#[test]
fn follow_up_hint_never_points_at_an_unrelated_range() {
    let (dir, session, tag) = fixture(100);
    let receipt = write(
        &session,
        dir.path(),
        Some(&tag),
        json!([{"op": "replace_text", "old": "row-050", "new": "ROW-050"}]),
    )
    .expect("write");
    assert!(
        !receipt.contains("re-read") && !receipt.contains("omitted"),
        "nothing was omitted, so no hint: {receipt}"
    );
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
