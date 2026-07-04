//! `tilth_write` — apply a whole-file-tag op-grammar blob.
//!
//! The tool takes a single `edits` text blob of `[path#TAG]` sections in
//! oh-my-pi's hashline op grammar (parsed by [`crate::edit::parser`]). Each
//! section is resolved to a confined path, verified against the whole-file tag
//! recorded by the read that displayed it, and applied — with 3-way-merge
//! recovery when the live file has drifted since that read. `REM`/`MV` file ops
//! and tagless `[path]` seed-creates are handled here; egress always flows
//! through the seen-lines-gated apply / recover entrypoints, never raw
//! `apply_ops`.

use std::collections::HashSet;
use std::fmt::Write as _;
use std::path::Path;
use std::sync::Arc;

use serde_json::Value;

use crate::edit::apply::FileOp;
use crate::edit::parser::{parse_sections, Op, Section};
use crate::edit::recovery::{check_seen_lines, gated_apply, try_recover, EditError};
use crate::edit::snapshots::{Snapshot, SnapshotStore};
use crate::edit::tag::{compute_file_hash, format_header, render_numbered_whole};
use crate::error::TilthError;
use crate::index::bloom::BloomFilterCache;
use crate::session::Session;

pub(crate) fn tool_write(
    args: &Value,
    session: &Session,
    _bloom: &Arc<BloomFilterCache>,
) -> Result<String, String> {
    let blob = args
        .get("edits")
        .and_then(|v| v.as_str())
        .ok_or("missing required parameter: edits (op-grammar text blob of [path#TAG] sections)")?;

    let show_diff = args
        .get("diff")
        .and_then(serde_json::Value::as_bool)
        .unwrap_or(false);

    // The absolute checkout directory anchors every relative section path.
    let cwd = super::require_cwd(args)?;

    let sections = parse_sections(blob).map_err(|e| format!("parse error at {e}"))?;
    if sections.is_empty() {
        return Err("edits contained no [path#TAG] sections".into());
    }
    if sections.len() > 20 {
        return Err(format!(
            "batch write limited to 20 sections (got {})",
            sections.len()
        ));
    }

    let ctx = SectionCtx {
        cwd,
        session,
        show_diff,
    };
    let mut results: Vec<String> = Vec::with_capacity(sections.len());
    let mut seen_paths: HashSet<String> = HashSet::new();
    for section in &sections {
        results.push(apply_section(section, &ctx, &mut seen_paths));
    }
    Ok(results.join("\n\n---\n\n"))
}

/// Shared per-call context threaded through the section pipeline: the absolute
/// checkout directory (anchors relative paths), the session store, and the diff
/// flag.
struct SectionCtx<'a> {
    cwd: &'a Path,
    session: &'a Session,
    show_diff: bool,
}

/// Resolve, confine, verify, apply, and commit one `[path#TAG]` section. Always
/// returns a `## <path>` Markdown block (success or error) — one failed section
/// never aborts the others.
fn apply_section(section: &Section, ctx: &SectionCtx, seen_paths: &mut HashSet<String>) -> String {
    let raw = &section.path;
    let path = match super::resolve_anchored(std::path::Path::new(raw), ctx.cwd) {
        Ok(p) => p,
        Err(e) => return format!("## {raw}\nerror: {e}"),
    };
    // Key the duplicate-path guard on the canonical key so `src/a.rs` and
    // `src/./a.rs` collide, preserving the one-section-per-file invariant.
    if !seen_paths.insert(crate::edit::normalize_path_key(&path)) {
        return format!(
            "## {}\nerror: duplicate path in this call — group all ops for a file under one [path#TAG] section",
            path.display()
        );
    }

    match commit_section(section, &path, ctx) {
        Ok(block) => block,
        Err(e) => format!("## {}\nerror: {e}", path.display()),
    }
}

/// The per-section egress: read live content, verify/recover against the tag,
/// carry out any file op, write, and record the fresh snapshot.
fn commit_section(section: &Section, path: &Path, ctx: &SectionCtx) -> Result<String, TilthError> {
    let session = ctx.session;
    // Read live content (missing file is allowed only for a tagless seed).
    let live = match std::fs::read_to_string(path) {
        Ok(c) => c,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            if section.tag.is_some() {
                return Err(TilthError::NotFound {
                    path: path.to_path_buf(),
                    suggestion: None,
                });
            }
            String::new()
        }
        Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => {
            return Err(TilthError::PermissionDenied {
                path: path.to_path_buf(),
            })
        }
        Err(e) => {
            return Err(TilthError::IoError {
                path: path.to_path_buf(),
                source: e,
            })
        }
    };

    let (new_text, file_op) = resolve_edit(section, path, session, &live)?;

    // File ops take precedence over an in-place write.
    if let Some(op) = file_op {
        return commit_file_op(&op, path, &new_text, &live, ctx);
    }

    // No-op guard: nothing changed.
    if new_text == live {
        return Ok(format!(
            "## {}\nno change (edit was a no-op)",
            path.display()
        ));
    }

    crate::util::atomic_write_bytes(path, new_text.as_bytes()).map_err(|e| {
        TilthError::IoError {
            path: path.to_path_buf(),
            source: e,
        }
    })?;
    session.record_read(path);

    // Record the fresh snapshot so a chained edit in a later call verifies.
    let key = crate::edit::normalize_path_key(path);
    let line_count = u32::try_from(new_text.split('\n').count()).unwrap_or(u32::MAX);
    let new_tag = session.record_snapshot(&key, &new_text, 1..=line_count);

    let mut block = format!("## {}\napplied", path.display());
    match new_tag {
        Some(tag) => {
            let header = format_header(&path.display().to_string(), tag);
            let _ = write!(block, "\n{header}\n{}", render_numbered_whole(&new_text));
        }
        // Over the per-file snapshot cap: no tag minted. Mirror the read side's
        // note so the model knows why it cannot re-anchor a follow-up edit.
        None => {
            let _ = write!(
                block,
                "\n# {} (too large to tag; edits cannot be tag-verified)",
                path.display()
            );
        }
    }
    if ctx.show_diff {
        block.push_str(&render_text_diff(Some(&live), &new_text));
    }
    Ok(block)
}

/// Verify the section's tag against live content and produce the edited text
/// plus any file op. On a matched tag with intact content, the seen-lines-gated
/// apply runs; on a drifted (or tag-collided) tag, [`recover_edit`] runs; a
/// tagless section seeds/edits against live directly (gate skipped).
fn resolve_edit(
    section: &Section,
    path: &Path,
    session: &Session,
    live: &str,
) -> Result<(String, Option<FileOp>), TilthError> {
    let key = crate::edit::normalize_path_key(path);
    let live_tag = compute_file_hash(live);

    match section.tag {
        // Tagless [path]: seed a new file or edit live with no provenance gate.
        None => {
            let snap = synthetic_snapshot(&key, live, live_tag);
            let r = gated_apply(&snap, path, &section.ops)?;
            Ok((r.text, r.file_op))
        }
        // Tag matches live → no drift (or a 16-bit tag collision). Run the
        // seen-lines gate over the recorded snapshot and apply — but only when
        // the recorded content actually equals live; a colliding-tag snapshot
        // whose text differs would silently overwrite the live drift, so route
        // it through recovery instead.
        Some(tag) if tag == live_tag => {
            let store = session.snapshots();
            if let Some(snap) = store.by_tag(&key, tag) {
                if snap.text == live {
                    let r = gated_apply(&snap, path, &section.ops)?;
                    return Ok((r.text, r.file_op));
                }
                return recover_edit(&store, section, path, &key, tag, live);
            }
            // The read's snapshot was evicted: synthetic over live (tag guards
            // content; empty provenance skips the seen-lines gate).
            let snap = synthetic_snapshot(&key, live, tag);
            let r = gated_apply(&snap, path, &section.ops)?;
            Ok((r.text, r.file_op))
        }
        // Tag ≠ live → the file drifted since the read. Recover via 3-way merge.
        Some(tag) => {
            let store = session.snapshots();
            recover_edit(&store, section, path, &key, tag, live)
        }
    }
}

/// The drift/collision egress: the recorded snapshot (if any) no longer matches
/// live content. Honor the seen-lines gate against the recorded snapshot, carry
/// a pure file op (`REM`/`MV`) through regardless of content drift, and
/// otherwise 3-way-merge the content edit onto live. The file op is derived
/// through the canonical [`FileOp::from_ops`] guard so this path rejects the
/// same op combinations the matched/tagless paths do.
fn recover_edit(
    store: &SnapshotStore,
    section: &Section,
    path: &Path,
    key: &str,
    tag: u16,
    live: &str,
) -> Result<(String, Option<FileOp>), TilthError> {
    // Provenance gate: if the read's snapshot survives, an edit anchored on a
    // never-displayed line is rejected here exactly as on the no-drift path. A
    // missing snapshot means the tag was never recorded this session (fabricated,
    // cross-session replay, or LRU-evicted) — it earns no short-circuit below.
    let tag_known = store.by_tag(key, tag).is_some();
    if let Some(snapshot) = store.by_tag(key, tag) {
        check_seen_lines(&snapshot, path, &section.ops).map_err(EditError::from)?;
    }
    let file_op = FileOp::from_ops(&section.ops).map_err(EditError::Apply)?;
    let has_content = section
        .ops
        .iter()
        .any(|o| !matches!(o, Op::Rem | Op::Mv { .. }));
    // A pure file op carries no content edit — file-level intent is independent
    // of content drift, so proceed without recovery, but ONLY for a session-known
    // tag. An unknown/fabricated tag falls through to try_recover, which rejects
    // it as Fabricated rather than silently deleting/moving on unverified intent.
    if tag_known && file_op.is_some() && !has_content {
        return Ok((live.to_string(), file_op));
    }
    let text = try_recover(store, path, tag, &section.ops, live)?;
    Ok((text, file_op))
}

/// Carry out a `REM`/`MV` file op with confinement, then reconcile the snapshot
/// store (invalidate on remove, relocate on move).
fn commit_file_op(
    op: &FileOp,
    path: &Path,
    new_text: &str,
    live: &str,
    ctx: &SectionCtx,
) -> Result<String, TilthError> {
    let session = ctx.session;
    let key = crate::edit::normalize_path_key(path);
    match op {
        FileOp::Remove => {
            std::fs::remove_file(path).map_err(|e| TilthError::IoError {
                path: path.to_path_buf(),
                source: e,
            })?;
            session.invalidate_snapshot(&key);
            Ok(format!("## {}\nremoved", path.display()))
        }
        FileOp::Move(dest_raw) => {
            let dest = super::resolve_anchored(std::path::Path::new(dest_raw), ctx.cwd)
                .map_err(TilthError::EditRejected)?;
            // If the move also carried content edits, land them before renaming.
            if new_text != live {
                crate::util::atomic_write_bytes(path, new_text.as_bytes()).map_err(|e| {
                    TilthError::IoError {
                        path: path.to_path_buf(),
                        source: e,
                    }
                })?;
            }
            if let Some(parent) = dest.parent() {
                std::fs::create_dir_all(parent).map_err(|e| TilthError::IoError {
                    path: parent.to_path_buf(),
                    source: e,
                })?;
            }
            std::fs::rename(path, &dest).map_err(|e| TilthError::IoError {
                path: dest.clone(),
                source: e,
            })?;
            let dest_key = crate::edit::normalize_path_key(&dest);
            session.relocate_snapshot(&key, &dest_key);
            Ok(format!("## {}\nmoved → {}", path.display(), dest.display()))
        }
    }
}

/// A provenance-free snapshot standing in for a real read: empty `seen_lines`
/// means the seen-lines gate is skipped (the tag still guards content).
fn synthetic_snapshot(key: &str, text: &str, tag: u16) -> Snapshot {
    Snapshot {
        path: key.to_string(),
        text: text.to_string(),
        tag,
        recorded_at: 0,
        seen_lines: HashSet::new(),
    }
}

/// Render a real minimal unified diff for the `diff:true` branch via `diffy`
/// (already the recovery layer's merge engine), rather than a degenerate
/// all-removed-then-all-added block.
fn render_text_diff(before: Option<&str>, after: &str) -> String {
    let patch = diffy::create_patch(before.unwrap_or(""), after);
    format!("\n── diff ──\n{patch}")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cache::OutlineCache;
    use crate::index::bloom::BloomFilterCache;
    use crate::session::Session;
    use serde_json::json;

    fn services() -> (Session, Arc<BloomFilterCache>) {
        (Session::new(), Arc::new(BloomFilterCache::new()))
    }

    /// Read a file in edit mode so the session records its whole-file-tag
    /// snapshot, and return the tag hex the read emitted in the `[path#TAG]`
    /// header. Fails the test if the header is absent.
    fn read_for_tag(session: &Session, path: &Path) -> String {
        let cache = OutlineCache::new();
        let out = crate::mcp::tools::tool_read(
            &json!({
                "paths": [path.to_str().unwrap()],
                "mode": "full",
                "cwd": path.parent().unwrap().to_str().unwrap()
            }),
            &cache,
            session,
            true,
        )
        .expect("edit-mode read");
        let marker = format!("{}#", path.display());
        let idx = out
            .find(&marker)
            .unwrap_or_else(|| panic!("read must emit [path#TAG] header, got:\n{out}"));
        let after = &out[idx + marker.len()..];
        let tag: String = after.chars().take(4).collect();
        assert_eq!(tag.len(), 4, "4-hex tag expected, got {tag:?} in:\n{out}");
        tag
    }

    #[test]
    fn read_then_edit_round_trip_applies_without_recovery() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("a.rs");
        std::fs::write(&p, "fn a() {}\nfn b() {}\n").unwrap();
        let (session, bloom) = services();

        let tag = read_for_tag(&session, &p);
        let blob = format!("[{}#{tag}]\nSWAP 1:\n+fn A() {{}}\n", p.display());
        let out = tool_write(
            &json!({"edits": blob, "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("write ok");
        assert!(out.contains("applied"), "expected applied, got:\n{out}");
        assert_eq!(
            std::fs::read_to_string(&p).unwrap(),
            "fn A() {}\nfn b() {}\n",
            "SWAP 1 must replace only line 1"
        );
    }

    #[test]
    fn edit_after_external_drift_recovers_not_rejects() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("drift.rs");
        std::fs::write(&p, "alpha\nbeta\nTARGET\ndelta\n").unwrap();
        let (session, bloom) = services();

        let tag = read_for_tag(&session, &p);
        std::fs::write(&p, "NEW1\nNEW2\nalpha\nbeta\nTARGET\ndelta\n").unwrap();

        let blob = format!("[{}#{tag}]\nSWAP 3:\n+RECOVERED\n", p.display());
        let out = tool_write(
            &json!({"edits": blob, "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("write ok");
        assert!(
            out.contains("applied"),
            "expected recovery applied, got:\n{out}"
        );
        assert_eq!(
            std::fs::read_to_string(&p).unwrap(),
            "NEW1\nNEW2\nalpha\nbeta\nRECOVERED\ndelta\n",
            "3-way merge must land the edit at the shifted position"
        );
    }

    #[test]
    fn conflicting_drift_yields_edit_rejected_not_silent_apply() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("conflict.rs");
        std::fs::write(&p, "a\nb\nTARGET\nd\n").unwrap();
        let (session, bloom) = services();
        let tag = read_for_tag(&session, &p);

        std::fs::write(&p, "totally\ndifferent\ncontent\nhere\n").unwrap();
        let blob = format!("[{}#{tag}]\nSWAP 3:\n+NEW\n", p.display());
        let out = tool_write(
            &json!({"edits": blob, "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("per-section error returns Ok");
        assert!(
            out.contains("error:") && out.contains("changed between read and edit"),
            "conflicting drift must be a Drift rejection, got:\n{out}"
        );
        assert_eq!(
            std::fs::read_to_string(&p).unwrap(),
            "totally\ndifferent\ncontent\nhere\n",
            "rejected edit must not touch the file"
        );
    }

    /// The drift branch must run the seen-lines gate exactly like the no-drift
    /// path: a symbol read displays only the symbol span, so after external
    /// drift an edit anchored on a never-displayed line is rejected — not
    /// silently recovered against the full snapshot.
    #[test]
    fn drift_branch_enforces_seen_lines_gate() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("driftgate.rs");
        let content = "fn outer() {\n    let x = 1;\n}\nfn other() {\n    let y = 2;\n}\n";
        std::fs::write(&p, content).unwrap();
        let (session, bloom) = services();
        let cache = OutlineCache::new();

        // Symbol read records only `outer`'s span (lines 1-3) as seen.
        crate::mcp::tools::tool_read(
            &json!({"paths": [format!("{}#outer", p.display())], "cwd": root.to_str().unwrap()}),
            &cache,
            &session,
            true,
        )
        .expect("symbol read");
        let tag = format!("{:04X}", compute_file_hash(content));

        // External drift: prepend a line so the tag no longer matches live.
        let drifted = format!("// prepended\n{content}");
        std::fs::write(&p, &drifted).unwrap();

        // Edit anchored on line 5 (inside `other`, never displayed) — on the
        // drift path this must still be rejected by the seen-lines gate.
        let blob = format!("[{}#{tag}]\nSWAP 5:\n+    let y = 9;\n", p.display());
        let out = tool_write(
            &json!({"edits": blob, "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("per-section error returns Ok");
        assert!(
            out.contains("never displayed"),
            "drift branch must enforce seen-lines; unseen-line edit must be rejected, got:\n{out}"
        );
        assert_eq!(
            std::fs::read_to_string(&p).unwrap(),
            drifted,
            "rejected edit must not touch the file"
        );
    }

    /// A pure `REM` against an externally-drifted file must succeed: file-level
    /// intent is independent of content drift. (Previously hard-rejected as
    /// Drift because `apply_ops` left a file-op-only section's text unchanged.)
    #[test]
    fn pure_rem_on_drifted_file_removes() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("remdrift.rs");
        std::fs::write(&p, "alpha\nbeta\n").unwrap();
        let (session, bloom) = services();
        let tag = read_for_tag(&session, &p);

        // External drift.
        std::fs::write(&p, "alpha\nbeta\ngamma\n").unwrap();
        let blob = format!("[{}#{tag}]\nREM\n", p.display());
        let out = tool_write(
            &json!({"edits": blob, "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("write ok");
        assert!(
            out.contains("removed"),
            "pure REM on a drifted file must remove, not reject, got:\n{out}"
        );
        assert!(!p.exists(), "file must be deleted despite content drift");
    }

    /// A pure `MV` against an externally-drifted file must move the (drifted)
    /// file rather than hard-reject.
    #[test]
    fn pure_mv_on_drifted_file_moves() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("mvdrift.rs");
        std::fs::write(&p, "one\ntwo\n").unwrap();
        let (session, bloom) = services();
        let tag = read_for_tag(&session, &p);

        std::fs::write(&p, "one\ntwo\nthree\n").unwrap();
        let blob = format!("[{}#{tag}]\nMV \"moved.rs\"\n", p.display());
        let out = tool_write(
            &json!({"edits": blob, "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("write ok");
        assert!(
            out.contains("moved"),
            "pure MV on a drifted file must move, not reject, got:\n{out}"
        );
        assert!(!p.exists(), "source must be gone after move");
        assert_eq!(
            std::fs::read_to_string(root.join("moved.rs")).unwrap(),
            "one\ntwo\nthree\n",
            "the drifted live content is what moves"
        );
    }

    /// The drift path derives its file op through the canonical `FileOp::from_ops`
    /// guard, so two file ops in one drifted section are rejected as a conflict
    /// (not silently reduced to the first op and applied).
    #[test]
    fn conflicting_file_ops_on_drift_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("conflictops.rs");
        std::fs::write(&p, "x\ny\n").unwrap();
        let (session, bloom) = services();
        let tag = read_for_tag(&session, &p);

        std::fs::write(&p, "x\ny\nz\n").unwrap();
        let blob = format!("[{}#{tag}]\nREM\nMV \"other.rs\"\n", p.display());
        let out = tool_write(
            &json!({"edits": blob, "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("per-section error returns Ok");
        assert!(
            out.contains("only one file op"),
            "REM + MV in one drifted section must be a FileOpConflict, got:\n{out}"
        );
        assert!(p.exists(), "rejected conflict must not remove the file");
        assert!(
            !root.join("other.rs").exists(),
            "rejected conflict must not move the file"
        );
    }

    /// A pure `REM` carrying a tag that was never recorded this session (never
    /// read) must be rejected as Fabricated — the provenance contract — and must
    /// not delete the file. The pure-file-op short-circuit only applies to a
    /// session-known tag.
    #[test]
    fn pure_rem_with_fabricated_tag_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("neverread.rs");
        std::fs::write(&p, "alpha\nbeta\n").unwrap();
        let (session, bloom) = services();
        // No read → the tag was never recorded this session; ^0x1 so tag ≠ live
        // (the drift egress) rather than the tagless synthetic path.
        let bogus = format!(
            "{:04X}",
            crate::edit::tag::compute_file_hash("alpha\nbeta\n") ^ 0x1
        );
        let blob = format!("[{}#{bogus}]\nREM\n", p.display());
        let out = tool_write(
            &json!({"edits": blob, "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("per-section error returns Ok");
        assert!(
            out.contains("not from this session"),
            "pure REM with a never-recorded tag must be Fabricated, got:\n{out}"
        );
        assert!(p.exists(), "fabricated-tag REM must not delete the file");
        assert_eq!(
            std::fs::read_to_string(&p).unwrap(),
            "alpha\nbeta\n",
            "fabricated-tag REM must leave the file untouched"
        );
    }

    /// A pure `MV` carrying a never-recorded tag must be rejected as Fabricated
    /// and must not move the file.
    #[test]
    fn pure_mv_with_fabricated_tag_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("nevermoved.rs");
        std::fs::write(&p, "one\ntwo\n").unwrap();
        let (session, bloom) = services();
        let bogus = format!(
            "{:04X}",
            crate::edit::tag::compute_file_hash("one\ntwo\n") ^ 0x1
        );
        let blob = format!("[{}#{bogus}]\nMV \"stolen.rs\"\n", p.display());
        let out = tool_write(
            &json!({"edits": blob, "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("per-section error returns Ok");
        assert!(
            out.contains("not from this session"),
            "pure MV with a never-recorded tag must be Fabricated, got:\n{out}"
        );
        assert!(p.exists(), "fabricated-tag MV must not move the source");
        assert!(
            !root.join("stolen.rs").exists(),
            "fabricated-tag MV must not create the destination"
        );
    }

    /// A 16-bit tag collision after external drift must not silently overwrite
    /// the live drift: when the recorded snapshot's content differs from live
    /// despite equal tags, the edit routes through recovery and is rejected
    /// rather than applied against the stale snapshot text.
    #[test]
    fn tag_collision_after_drift_does_not_silently_overwrite() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("collide.rs");
        let original = "line one\n";
        std::fs::write(&p, original).unwrap();
        let (session, bloom) = services();

        // Read records the snapshot for `original` under its tag.
        let tag = read_for_tag(&session, &p);

        // Brute-force a different content that hashes to the same 16-bit tag.
        let base_tag = compute_file_hash(original);
        let mut colliding = None;
        for i in 0..500_000u32 {
            let cand = format!("candidate {i}\n");
            if compute_file_hash(&cand) == base_tag {
                colliding = Some(cand);
                break;
            }
        }
        let colliding = colliding.expect("16-bit collision found within search budget");
        assert_ne!(colliding, original, "collision must be different content");

        // External drift swaps in the colliding content: live_tag == recorded tag.
        std::fs::write(&p, &colliding).unwrap();
        let blob = format!("[{}#{tag}]\nSWAP 1:\n+overwrite\n", p.display());
        let out = tool_write(
            &json!({"edits": blob, "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("per-section error returns Ok");
        assert!(
            out.contains("error:"),
            "colliding-tag edit over drifted content must be rejected, got:\n{out}"
        );
        assert_eq!(
            std::fs::read_to_string(&p).unwrap(),
            colliding,
            "rejected edit must leave the live (drifted) content intact"
        );
    }

    #[test]
    fn fabricated_tag_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("f.rs");
        std::fs::write(&p, "x\ny\n").unwrap();
        let (session, bloom) = services();
        let live_tag = crate::edit::tag::compute_file_hash("x\ny\n");
        let bogus = format!("{:04X}", live_tag ^ 0x1);
        let blob = format!("[{}#{bogus}]\nSWAP 1:\n+X\n", p.display());
        let out = tool_write(
            &json!({"edits": blob, "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("per-section error returns Ok");
        assert!(
            out.contains("not from this session"),
            "unknown tag must be a Fabricated rejection, got:\n{out}"
        );
        assert_eq!(std::fs::read_to_string(&p).unwrap(), "x\ny\n");
    }

    #[test]
    fn relative_dotdot_section_path_refused() {
        // A relative section path with `..` must not climb out of cwd.
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let (session, bloom) = services();
        let blob = "[../evil.rs#0000]\nSWAP 1:\n+x\n".to_string();
        let out = tool_write(
            &json!({"edits": blob, "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("per-section error returns Ok");
        assert!(
            out.contains("escapes") && out.contains(".."),
            "`..` traversal in a section path must be rejected, got:\n{out}"
        );
        assert!(
            !root.parent().unwrap().join("evil.rs").exists(),
            "no file may be created outside cwd"
        );
    }

    #[test]
    fn absolute_path_outside_cwd_succeeds() {
        // Trust-absolute posture: an absolute section path OUTSIDE cwd (e.g. a
        // linked worktree) is written, not refused.
        let checkout = tempfile::tempdir().unwrap();
        let outside = tempfile::tempdir().unwrap();
        let target = outside.path().join("out.rs");
        std::fs::write(&target, "a\n").unwrap();
        let (session, bloom) = services();
        let tag = read_for_tag(&session, &target);
        let blob = format!("[{}#{tag}]\nSWAP 1:\n+X\n", target.display());
        let out = tool_write(
            &json!({"edits": blob, "cwd": checkout.path().to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("write ok");
        assert!(
            out.contains("applied"),
            "absolute path outside cwd must be written (trust-absolute), got:\n{out}"
        );
        assert_eq!(
            std::fs::read_to_string(&target).unwrap(),
            "X\n",
            "the edit must land in the file outside cwd"
        );
    }

    #[test]
    fn mv_dest_escape_via_dotdot_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("m.rs");
        std::fs::write(&p, "content\n").unwrap();
        let (session, bloom) = services();
        let tag = read_for_tag(&session, &p);
        let blob = format!("[{}#{tag}]\nMV \"../escaped.rs\"\n", p.display());
        let out = tool_write(
            &json!({"edits": blob, "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("per-section error returns Ok");
        assert!(
            out.contains("escapes") && out.contains(".."),
            "MV dest with `..` must be rejected, got:\n{out}"
        );
        assert!(p.exists(), "source file must remain after a rejected MV");
        assert!(!root.parent().unwrap().join("escaped.rs").exists());
    }

    #[test]
    fn mv_moves_file_and_relocates_snapshot() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("src.rs");
        std::fs::write(&p, "a\nb\nc\nd\n").unwrap();
        let (session, bloom) = services();
        let cache = OutlineCache::new();
        // Range read records seen-lines {1,2} under the whole-file tag.
        crate::mcp::tools::tool_read(
            &json!({"paths": [format!("{}#1-2", p.display())], "cwd": root.to_str().unwrap()}),
            &cache,
            &session,
            true,
        )
        .expect("range read");
        let tag = format!("{:04X}", compute_file_hash("a\nb\nc\nd\n"));
        let blob = format!("[{}#{tag}]\nMV \"dest.rs\"\n", p.display());
        let out = tool_write(
            &json!({"edits": blob, "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("write ok");
        assert!(out.contains("moved"), "expected moved, got:\n{out}");
        assert!(!p.exists(), "source removed after move");
        let dest = root.join("dest.rs");
        assert_eq!(std::fs::read_to_string(&dest).unwrap(), "a\nb\nc\nd\n");

        // The relocated snapshot must carry src's seen-lines {1,2}: an edit on
        // the never-displayed line 4 of dest is rejected by the seen-lines gate.
        // Without relocation, by_tag(dest) would miss and a synthetic
        // empty-provenance snapshot would skip the gate and let it apply.
        let edit = format!("[{}#{tag}]\nSWAP 4:\n+D\n", dest.display());
        let rej = tool_write(
            &json!({"edits": edit, "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("per-section error returns Ok");
        assert!(
            rej.contains("never displayed"),
            "relocated snapshot must gate an unseen line at dest, got:\n{rej}"
        );
        assert_eq!(
            std::fs::read_to_string(&dest).unwrap(),
            "a\nb\nc\nd\n",
            "rejected edit must not touch dest"
        );
    }

    #[test]
    fn rem_removes_file_and_invalidates_snapshot() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("gone.rs");
        std::fs::write(&p, "bye\n").unwrap();
        let (session, bloom) = services();
        let tag = read_for_tag(&session, &p);
        let blob = format!("[{}#{tag}]\nREM\n", p.display());
        let out = tool_write(
            &json!({"edits": blob, "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("write ok");
        assert!(out.contains("removed"), "expected removed, got:\n{out}");
        assert!(!p.exists(), "file must be deleted by REM");

        // Recreate the path with fresh content. The pre-REM snapshot must have
        // been invalidated: the old tag is no longer known to the store, so a
        // stale-tag edit is a Fabricated rejection ("not from this session"). A
        // lingering snapshot under the key would instead surface as Drift.
        std::fs::write(&p, "fresh content here\n").unwrap();
        let stale = format!("[{}#{tag}]\nSWAP 1:\n+X\n", p.display());
        let out2 = tool_write(
            &json!({"edits": stale, "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("per-section error returns Ok");
        assert!(
            out2.contains("not from this session"),
            "post-REM edit with the old tag must be Fabricated (snapshot invalidated), got:\n{out2}"
        );
        assert_eq!(
            std::fs::read_to_string(&p).unwrap(),
            "fresh content here\n",
            "rejected stale edit must not touch the recreated file"
        );
    }

    #[test]
    fn tagless_section_seeds_new_file() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let (session, bloom) = services();
        let blob = "[new.rs]\nINS.HEAD:\n+fn seeded() {}\n".to_string();
        let out = tool_write(
            &json!({"edits": blob, "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("write ok");
        assert!(out.contains("applied"), "expected applied, got:\n{out}");
        assert_eq!(
            std::fs::read_to_string(root.join("new.rs")).unwrap(),
            "fn seeded() {}\n",
            "tagless section seeds the file with the inserted content"
        );
    }

    #[test]
    fn missing_edits_blob_rejected() {
        let (session, bloom) = services();
        let err = tool_write(&json!({}), &session, &bloom).expect_err("no edits → top-level error");
        assert!(
            err.contains("edits"),
            "error must name the required param: {err}"
        );
    }

    #[test]
    fn parse_error_is_top_level() {
        let (session, bloom) = services();
        let err = tool_write(
            &json!({"edits": "[a#0000]\n+orphan\n", "cwd": "/abs"}),
            &session,
            &bloom,
        )
        .expect_err("parse error is top-level");
        assert!(err.contains("parse error"), "got: {err}");
    }

    #[test]
    fn missing_cwd_rejected() {
        // A write with an edits blob but no cwd is refused with the teaching error.
        let (session, bloom) = services();
        let err = tool_write(&json!({"edits": "[a#0000]\nDEL 1\n"}), &session, &bloom)
            .expect_err("no cwd → top-level error");
        assert!(
            err.contains("cwd") && err.contains("absolute checkout directory"),
            "missing cwd must refuse with the teaching error: {err}"
        );
    }

    #[test]
    fn multi_section_write_lands_edits_in_both_files() {
        // Wiring seam: the section loop must apply every [path#TAG] section in
        // one blob, landing independent edits in independent files.
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let a = root.join("one.rs");
        let b = root.join("two.rs");
        std::fs::write(&a, "fn one() {}\n").unwrap();
        std::fs::write(&b, "fn two() {}\n").unwrap();
        let (session, bloom) = services();
        let tag_a = read_for_tag(&session, &a);
        let tag_b = read_for_tag(&session, &b);
        let blob = format!(
            "[{}#{tag_a}]\nSWAP 1:\n+fn ONE() {{}}\n\n[{}#{tag_b}]\nSWAP 1:\n+fn TWO() {{}}\n",
            a.display(),
            b.display()
        );
        let out = tool_write(
            &json!({"edits": blob, "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("write ok");
        assert_eq!(
            out.matches("applied").count(),
            2,
            "both sections must apply, got:\n{out}"
        );
        assert_eq!(std::fs::read_to_string(&a).unwrap(), "fn ONE() {}\n");
        assert_eq!(std::fs::read_to_string(&b).unwrap(), "fn TWO() {}\n");
    }

    #[test]
    fn duplicate_path_in_one_call_is_rejected_second_section_only() {
        // Two sections for the same file must be refused on the second: the
        // seen_paths dedup guards against split, conflicting op groups.
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("dup.rs");
        std::fs::write(&p, "fn a() {}\nfn b() {}\n").unwrap();
        let (session, bloom) = services();
        let tag = read_for_tag(&session, &p);
        let blob = format!(
            "[{}#{tag}]\nSWAP 1:\n+fn A() {{}}\n\n[{}#{tag}]\nSWAP 2:\n+fn B() {{}}\n",
            p.display(),
            p.display()
        );
        let out = tool_write(
            &json!({"edits": blob, "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("per-section error returns Ok");
        assert!(
            out.contains("duplicate path"),
            "second section on same path must be a duplicate-path error, got:\n{out}"
        );
        // The first section applied; the second was dropped, so the file shows
        // only the first edit's effect.
        assert_eq!(
            std::fs::read_to_string(&p).unwrap(),
            "fn A() {}\nfn b() {}\n",
            "only the first section's edit lands; the duplicate is dropped"
        );
    }

    /// A `#symbol` edit-mode read records only the symbol's span as seen, so a
    /// tag-matched edit anchored INSIDE that span applies but one anchored on a
    /// never-displayed line is rejected. Locks the `find_entry_by_name` hoist +
    /// `seen_spec` parity through the real read→write path.
    #[test]
    fn symbol_read_gates_edit_to_displayed_span() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("sym.rs");
        let content = "fn outer() {\n    let x = 1;\n}\nfn other() {\n    let y = 2;\n}\n";
        std::fs::write(&p, content).unwrap();
        let (session, bloom) = services();

        // Symbol read records the span of `outer` (lines 1-3) as seen.
        let cache = OutlineCache::new();
        let sym_out = crate::mcp::tools::tool_read(
            &json!({"paths": [format!("{}#outer", p.display())], "cwd": root.to_str().unwrap()}),
            &cache,
            &session,
            true,
        )
        .expect("symbol read");
        assert!(
            sym_out.lines().any(|l| l == "2:    let x = 1;"),
            "symbol read must display line 2 of the span, got:\n{sym_out}"
        );
        let tag = format!("{:04X}", compute_file_hash(content));

        // An edit anchored on line 5 (inside `other`, never displayed) is rejected.
        let reject = tool_write(
            &json!({
                "edits": format!("[{}#{tag}]\nSWAP 5:\n+    let y = 9;\n", p.display()),
                "cwd": root.to_str().unwrap()
            }),
            &session,
            &bloom,
        )
        .expect("per-section error returns Ok");
        assert!(
            reject.contains("never displayed"),
            "edit on a line outside the read symbol span must be rejected, got:\n{reject}"
        );
        assert_eq!(
            std::fs::read_to_string(&p).unwrap(),
            content,
            "rejected edit must not touch the file"
        );

        // An edit anchored on line 2 (inside the displayed span) applies.
        let ok = tool_write(
            &json!({
                "edits": format!("[{}#{tag}]\nSWAP 2:\n+    let x = 42;\n", p.display()),
                "cwd": root.to_str().unwrap()
            }),
            &session,
            &bloom,
        )
        .expect("write ok");
        assert!(
            ok.contains("applied"),
            "in-span edit must apply, got:\n{ok}"
        );
        assert_eq!(
            std::fs::read_to_string(&p).unwrap(),
            "fn outer() {\n    let x = 42;\n}\nfn other() {\n    let y = 2;\n}\n"
        );
    }

    /// A `mode:signature` read records nothing, so it must not grant seen-lines
    /// that would poison a later range read's unseen-anchor gate.
    #[test]
    fn signature_read_grants_no_seen_lines() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("sig.rs");
        let content = "fn a() {}\nfn b() {}\nfn c() {}\nfn d() {}\n";
        std::fs::write(&p, content).unwrap();
        let (session, bloom) = services();
        let cache = OutlineCache::new();

        // Signature read (records nothing).
        crate::mcp::tools::tool_read(
            &json!({"paths": [p.to_str().unwrap()], "mode": "signature", "cwd": root.to_str().unwrap()}),
            &cache,
            &session,
            true,
        )
        .expect("signature read");
        // Range read of line 1 only (records seen = {1}).
        crate::mcp::tools::tool_read(
            &json!({"paths": [format!("{}#1-1", p.display())], "cwd": root.to_str().unwrap()}),
            &cache,
            &session,
            true,
        )
        .expect("range read");

        // Line 3 was never displayed by either read → edit must be rejected.
        let tag = format!("{:04X}", compute_file_hash(content));
        let out = tool_write(
            &json!({
                "edits": format!("[{}#{tag}]\nSWAP 3:\n+fn C() {{}}\n", p.display()),
                "cwd": root.to_str().unwrap()
            }),
            &session,
            &bloom,
        )
        .expect("per-section error returns Ok");
        assert!(
            out.contains("never displayed"),
            "signature read must not grant seen-lines; line-3 edit must be rejected, got:\n{out}"
        );
        assert_eq!(std::fs::read_to_string(&p).unwrap(), content);
    }

    /// A file with no trailing newline round-trips: the read mints a tag, and a
    /// tag-matched SWAP lands on the intended line without corrupting the
    /// missing-final-newline shape.
    #[test]
    fn no_trailing_newline_round_trip() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("nonl.rs");
        std::fs::write(&p, "fn a() {}\nfn b() {}").unwrap();
        let (session, bloom) = services();
        let tag = read_for_tag(&session, &p);
        let blob = format!("[{}#{tag}]\nSWAP 2:\n+fn B() {{}}\n", p.display());
        let out = tool_write(
            &json!({"edits": blob, "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("write ok");
        assert!(out.contains("applied"), "expected applied, got:\n{out}");
        assert_eq!(
            std::fs::read_to_string(&p).unwrap(),
            "fn a() {}\nfn B() {}",
            "SWAP 2 replaces line 2 and preserves the no-trailing-newline shape"
        );
    }
}
