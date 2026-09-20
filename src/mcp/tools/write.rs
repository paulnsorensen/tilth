//! `tilth_write` — apply a JSON `edits` array of whole-file-tag sections.
//!
//! The tool takes an `edits` JSON array of `{path, tag?, ops}` section objects
//! ([`crate::edit::json`] lowers them into the grammar-independent
//! [`crate::edit::parser`] `Section`/`Op` types). Each
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

use crate::edit::apply::{ApplyError, FileOp};
use crate::edit::json::{lower_edits, teaching_error_for_string};
use crate::edit::mismatch::MismatchError;
use crate::edit::parser::{Op, Section};
use crate::edit::recovery::{check_seen_lines, gated_apply, try_recover, EditError};
use crate::edit::snapshots::{Snapshot, SnapshotStore};
use crate::edit::tag::{compute_file_hash, format_header, render_numbered_slice};
use crate::error::TilthError;
use crate::index::bloom::BloomFilterCache;
use crate::session::Session;

/// Extract and validate the top-level `tool_write` arguments: the lowered
/// `{path, tag?, ops}` sections, the anchoring `cwd`, and the `diff` flag.
/// A string `edits` (legacy `[path#TAG]` blob or a double-encoded array) is
/// rejected with a teaching error that shows the corrected JSON form.
fn parse_write_args(args: &Value) -> Result<(Vec<Section>, &Path, bool), String> {
    let edits_val = args.get("edits").ok_or(
        "missing required parameter: edits (JSON array of {path, tag?, ops} section objects)",
    )?;
    if let Some(s) = edits_val.as_str() {
        return Err(teaching_error_for_string(s));
    }

    let show_diff = args
        .get("diff")
        .and_then(serde_json::Value::as_bool)
        .unwrap_or(false);

    let cwd = super::require_cwd(args)?;

    // `lower_edits` enforces the 20-section batch cap up front (before lowering).
    let sections = lower_edits(edits_val)?;
    if sections.is_empty() {
        return Err("edits array contained no sections".into());
    }

    Ok((sections, cwd, show_diff))
}

pub(crate) fn tool_write(
    args: &Value,
    session: &Session,
    _bloom: &Arc<BloomFilterCache>,
) -> Result<String, String> {
    let (sections, cwd, show_diff) = parse_write_args(args)?;

    let section_count = sections.len() as u64;
    let metadata_reserve = section_count.saturating_mul(80);
    let section_budget = WRITE_RESPONSE_BUDGET
        .saturating_sub(metadata_reserve)
        .checked_div(section_count.max(1))
        .unwrap_or(0);
    let ctx = SectionCtx {
        cwd,
        session,
        show_diff,
        section_budget,
    };
    let mut results: Vec<String> = Vec::with_capacity(sections.len());
    let mut seen_paths: HashSet<String> = HashSet::new();
    let mut any_ok = false;
    for section in &sections {
        match apply_section(section, &ctx, &mut seen_paths) {
            Ok(block) => {
                any_ok = true;
                results.push(block);
            }
            Err(block) => results.push(block),
        }
    }
    let joined = results.join("\n\n---\n\n");
    // Surface `isError: true` only when EVERY section failed — a mixed call
    // keeps `isError: false` with per-section `error:` lines. All-failed calls
    // were previously invisible to dashboards that key on the MCP error flag.
    if any_ok {
        Ok(joined)
    } else {
        Err(joined)
    }
}

/// Shared per-call context threaded through the section pipeline: the caller's
/// cwd (anchor root for relative paths), the session store, and the diff flag.
struct SectionCtx<'a> {
    cwd: &'a Path,
    session: &'a Session,
    show_diff: bool,
    section_budget: u64,
}

/// Resolve, confine, verify, apply, and commit one `[path#TAG]` section. Always
/// returns a `## <path>` Markdown block, `Ok` on success and `Err` on failure —
/// one failed section never aborts the others, and the variant lets the caller
/// decide the all-failed MCP `isError` flag.
fn apply_section(
    section: &Section,
    ctx: &SectionCtx,
    seen_paths: &mut HashSet<String>,
) -> Result<String, String> {
    let raw = &section.path;
    let path = match super::resolve_anchored(std::path::Path::new(raw), ctx.cwd) {
        Ok(p) => p,
        Err(e) => return Err(format!("## {raw}\nerror: {e}")),
    };
    // Key the duplicate-path guard on the canonical key so `src/a.rs` and
    // `src/./a.rs` collide, preserving the one-section-per-file invariant.
    if !seen_paths.insert(crate::edit::normalize_path_key(&path)) {
        return Err(format!(
            "## {}\nerror: duplicate path in this call — group all ops for a file under one section",
            path.display()
        ));
    }

    match commit_section(section, &path, ctx) {
        Ok(block) => Ok(block),
        Err(e) => Err(format!("## {}\nerror: {e}", path.display())),
    }
}

/// The per-section egress: read live content, verify/recover against the tag,
/// carry out any file op, write, and record the fresh snapshot.
fn commit_section(section: &Section, path: &Path, ctx: &SectionCtx) -> Result<String, TilthError> {
    if section.ops.iter().any(|op| matches!(op, Op::Create { .. })) && section.tag.is_some() {
        return Err(TilthError::EditRejected(format!(
            "create_file requires a tagless section for a new file: {} — use a tagged read with replace instead",
            path.display()
        )));
    }
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

    let (new_text, file_op, normalized) = resolve_edit(section, path, session, &live)?;

    // File ops take precedence over an in-place write.
    if let Some(op) = file_op {
        return commit_file_op(&op, path, &new_text, &live, normalized, ctx);
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

    // Render a bounded post-edit neighborhood before recording provenance.
    // The snapshot keeps the whole source, but only rendered lines become anchors.
    let first_changed = first_changed_line(&live, &new_text);
    let (numbered, seen_lines, reread_hint) = render_changed_window(
        &new_text,
        first_changed,
        path,
        ctx.section_budget.saturating_sub(WRITE_DIFF_BUDGET),
    );
    let new_tag = session.record_snapshot(path, &new_text, seen_lines);

    // Decision 5: when the exact `old` failed and the whitespace-normalized
    // fallback landed the swap, say so on the status line so the model knows its
    // `old` did not match byte-for-byte.
    let status = if normalized {
        "applied (matched with whitespace normalization)"
    } else {
        "applied"
    };
    let mut block = format!("## {}\n{status}", path.display());
    match new_tag {
        Some(tag) => {
            let header = format_header(&path.display().to_string(), tag);
            let _ = write!(block, "\n{header}");
            if !numbered.is_empty() {
                let _ = write!(block, "\n{numbered}");
            }
            if let Some(hint) = reread_hint {
                let _ = write!(block, "\n{hint}");
            }
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
        block.push_str(&render_bounded_diff(Some(&live), &new_text));
    }
    Ok(block)
}

/// The provenance-teaching message for a `replace_text` `old` that did not
/// match against `path` under `tag` (decision 4 / ADR-005).
fn text_unmatched_message(path: &Path, tag: u16, preview: &str) -> String {
    format!(
        "text to replace was not found; copy old verbatim from the numbered lines of \
         {} — do not retype it from memory or shell output (preview: {preview})",
        format_header(&path.display().to_string(), tag)
    )
}

/// Enrich a `replace_text` no-match into decision 4's provenance-teaching error;
/// every other edit failure keeps its own already-actionable message. The tag is
/// the section's whole-file tag — always present when a `replace_text` reaches
/// the matcher, since a tagless text swap is rejected upstream.
fn map_edit_error(e: EditError, path: &Path, tag: u16) -> TilthError {
    if let EditError::Apply(ApplyError::TextUnmatched { preview }) = &e {
        return TilthError::EditRejected(text_unmatched_message(path, tag, preview));
    }
    e.into()
}

/// Verify the section's tag against live content and produce the edited text,
/// any file op, and whether a `replace_text` resolved through whitespace
/// normalization. On a matched tag with intact content, the seen-lines-gated
/// apply runs; on a drifted (or tag-collided) tag, [`recover_edit`] runs; a
/// tagless section seeds/edits against live with synthetic empty provenance.
fn resolve_edit(
    section: &Section,
    path: &Path,
    session: &Session,
    live: &str,
) -> Result<(String, Option<FileOp>, bool), TilthError> {
    let key = crate::edit::normalize_path_key(path);
    let live_tag = compute_file_hash(live);

    match section.tag {
        // Tagless [path]: seed a new file or edit live with no source-line provenance.
        None => {
            if section
                .ops
                .iter()
                .any(|op| matches!(op, Op::TextSwap { .. }))
            {
                // Naming only the requirement sent agents into a full re-read.
                // A section read carries the whole-file tag, so the cheap route
                // has to be part of the rejection.
                return Err(TilthError::EditRejected(
                    "replace_text requires a tag from an edit-mode read; a section read \
                     (path#12-40) carries the whole-file tag without reading the file in \
                     full, but `old` must occur in the lines it displayed. Files over the \
                     tag cap mint no tag — use line ops there."
                        .into(),
                ));
            }
            let snap = synthetic_snapshot(&key, live, live_tag);
            let r = gated_apply(&snap, path, &section.ops)?;
            Ok((r.text, r.file_op, r.normalized_swap))
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
                    let r = gated_apply(&snap, path, &section.ops)
                        .map_err(|e| map_edit_error(e, path, tag))?;
                    return Ok((r.text, r.file_op, r.normalized_swap));
                }
                return recover_edit(&store, section, path, &key, tag, live);
            }
            // The read's snapshot was evicted: preserve the tag and source text,
            // but do not authorize hidden source lines.
            let snap = synthetic_snapshot(&key, live, tag);
            let r =
                gated_apply(&snap, path, &section.ops).map_err(|e| map_edit_error(e, path, tag))?;
            Ok((r.text, r.file_op, r.normalized_swap))
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
/// otherwise 3-way-merge the content edit onto live.
fn recover_edit(
    store: &SnapshotStore,
    section: &Section,
    path: &Path,
    key: &str,
    tag: u16,
    live: &str,
) -> Result<(String, Option<FileOp>, bool), TilthError> {
    // Provenance gate: if the read's snapshot survives, an edit anchored on a
    // never-displayed line is rejected here exactly as on the no-drift path. A
    // missing snapshot means the tag was never recorded this session (fabricated,
    // cross-session replay, or LRU-evicted) — it earns no short-circuit below.
    let snapshot = store.by_tag(key, tag);
    if let Some(snapshot) = &snapshot {
        check_seen_lines(snapshot, path, &section.ops).map_err(EditError::from)?;
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
    if snapshot.is_some() && file_op.is_some() && !has_content {
        return Ok((live.to_string(), file_op, false));
    }
    let (text, normalized) = match try_recover(store, path, tag, &section.ops, live) {
        Ok(t) => t,
        Err(MismatchError::TextMatch {
            path: p,
            source: ApplyError::TextUnmatched { preview },
        }) => {
            return Err(TilthError::EditRejected(format!(
                "Edit rejected for {p}: {}. The file also changed since the read that minted this tag — re-read to refresh it.",
                text_unmatched_message(path, tag, &preview)
            )));
        }
        Err(e) => return Err(e.into()),
    };
    Ok((text, file_op, normalized))
}

fn create_target_exists_error(path: &Path) -> TilthError {
    TilthError::EditRejected(format!(
        "create_file target already exists: {} — use a tagged read with replace instead",
        path.display()
    ))
}

/// Carry out a `CREATE`/`REM`/`MV` file op with confinement, then reconcile the
/// snapshot store (invalidate on remove, relocate on move). CREATE writes exact raw content.
fn commit_file_op(
    op: &FileOp,
    path: &Path,
    new_text: &str,
    live: &str,
    normalized: bool,
    ctx: &SectionCtx,
) -> Result<String, TilthError> {
    let session = ctx.session;
    // Decision 5: mirror the in-place write's whitespace-normalization note
    // when the move/create/remove section also carried a normalized swap.
    let suffix = if normalized {
        " (matched with whitespace normalization)"
    } else {
        ""
    };
    match op {
        FileOp::Create(content) => {
            if let Some(parent) = path.parent() {
                std::fs::create_dir_all(parent).map_err(|e| TilthError::IoError {
                    path: parent.to_path_buf(),
                    source: e,
                })?;
            }
            crate::util::atomic_create_bytes_no_replace(path, content.as_bytes()).map_err(|e| {
                if e.kind() == std::io::ErrorKind::AlreadyExists {
                    create_target_exists_error(path)
                } else {
                    TilthError::IoError {
                        path: path.to_path_buf(),
                        source: e,
                    }
                }
            })?;
            session.record_read(path);
            // CREATE does not echo the caller's source body, so it displays no
            // content lines under the fresh tag.
            let new_tag = session.record_snapshot(path, content, std::iter::empty());
            let mut block = format!("## {}\ncreated{suffix}", path.display());
            if let Some(tag) = new_tag {
                let header = format_header(&path.display().to_string(), tag);
                let _ = write!(block, "\n{header}");
            }
            Ok(block)
        }
        FileOp::Remove => {
            // Capture the canonical key before the fs op: once the file is gone,
            // `normalize_path_key` falls back to a lexical (non-symlink-resolving)
            // spelling that can diverge from the canonical key `record` minted.
            // The already-canonical spelling survives that lexical fallback
            // unchanged, so invalidation still finds the recorded history.
            let canonical = std::fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
            std::fs::remove_file(path).map_err(|e| TilthError::IoError {
                path: path.to_path_buf(),
                source: e,
            })?;
            session.invalidate_snapshot(&canonical);
            Ok(format!("## {}\nremoved{suffix}", path.display()))
        }
        FileOp::Move(dest_raw) => {
            let dest = super::resolve_anchored(std::path::Path::new(dest_raw), ctx.cwd)
                .map_err(TilthError::EditRejected)?;
            if dest.exists()
                && crate::edit::normalize_path_key(&dest) != crate::edit::normalize_path_key(path)
            {
                return Err(TilthError::EditRejected(format!(
                    "move destination already exists: {} — delete it or choose another destination",
                    dest.display()
                )));
            }
            // Capture the canonical source key before the fs op — see the
            // Remove arm above for why this must happen while the file still
            // exists at `path`.
            let canonical_src = std::fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
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
            session.relocate_snapshot(&canonical_src, &dest);
            Ok(format!(
                "## {}\nmoved{suffix} → {}",
                path.display(),
                dest.display()
            ))
        }
    }
}

const WRITE_RESPONSE_BUDGET: u64 = 6_000;
const WRITE_DIFF_BUDGET: u64 = 80;
const WRITE_CONTEXT_LINES: u32 = 2;

fn first_changed_line(before: &str, after: &str) -> Option<u32> {
    let before_rows: Vec<&str> = before.split('\n').collect();
    let after_rows: Vec<&str> = after.split('\n').collect();
    let total = before_rows.len().max(after_rows.len());
    (0..total)
        .find(|&idx| before_rows.get(idx) != after_rows.get(idx))
        .map(|idx| u32::try_from(idx + 1).unwrap_or(u32::MAX))
}

fn render_changed_window(
    text: &str,
    first_changed: Option<u32>,
    path: &Path,
    content_budget: u64,
) -> (String, Vec<u32>, Option<String>) {
    let mut rows: Vec<&str> = text.split('\n').collect();
    if text.ends_with('\n') {
        rows.pop();
    }
    let total = u32::try_from(rows.len()).unwrap_or(u32::MAX);
    let Some(first) = first_changed else {
        return (String::new(), Vec::new(), None);
    };
    if total == 0 {
        return (String::new(), Vec::new(), None);
    }
    let lo = first.saturating_sub(WRITE_CONTEXT_LINES).max(1).min(total);
    let hi = first.saturating_add(WRITE_CONTEXT_LINES).min(total).max(lo);
    let start = usize::try_from(lo - 1).unwrap_or(0);
    let end = usize::try_from(hi).unwrap_or(rows.len()).min(rows.len());
    let focus = first.clamp(lo, hi);
    let mut candidates = vec![focus];
    candidates.extend((lo..=hi).filter(|line| *line != focus));
    let mut rendered_rows: Vec<(u32, String)> = Vec::new();
    let mut seen = Vec::new();
    for line in candidates {
        let index = usize::try_from(line.saturating_sub(1)).unwrap_or(0);
        let Some(row) = rows.get(index) else {
            continue;
        };
        let numbered = render_numbered_slice(row, line);
        let combined_len = rendered_rows
            .iter()
            .map(|(_, part)| part.len())
            .sum::<usize>()
            .saturating_add(numbered.len())
            .saturating_add(rendered_rows.len());
        if crate::types::estimate_tokens(combined_len as u64) <= content_budget {
            rendered_rows.push((line, numbered));
            seen.push(line);
        }
    }
    rendered_rows.sort_unstable_by_key(|(line, _)| *line);
    let rendered = rendered_rows
        .into_iter()
        .map(|(_, row)| row)
        .collect::<Vec<_>>()
        .join("\n");
    let omitted_window = seen.len() < end.saturating_sub(start);
    let hint = (omitted_window || lo > 1 || hi < total).then(|| {
        let reread_lo = if omitted_window {
            lo
        } else if lo > 1 {
            1
        } else {
            hi + 1
        };
        let reread_hi = reread_lo.saturating_add(59).min(total);
        format!(
            "... omitted lines {}-{}; re-read {}#{}-{}",
            reread_lo,
            total,
            path.display(),
            reread_lo,
            reread_hi
        )
    });
    (rendered, seen, hint)
}

fn render_bounded_diff(before: Option<&str>, after: &str) -> String {
    let diff = render_text_diff(before, after);
    crate::budget::apply_item(&diff, WRITE_DIFF_BUDGET, WRITE_RESPONSE_BUDGET)
}

/// A synthetic snapshot preserves the tag and full source after provenance
/// eviction, but it marks no source lines as displayed.
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

    /// Build a one-section `edits` array Value. `ops` is the JSON ops array.
    fn edits(path: &Path, tag: Option<&str>, ops: Value) -> Value {
        let mut sec = serde_json::Map::new();
        sec.insert("path".into(), json!(path.to_str().unwrap()));
        if let Some(t) = tag {
            sec.insert("tag".into(), json!(t));
        }
        sec.insert("ops".into(), ops);
        json!([Value::Object(sec)])
    }

    /// Read a file in edit mode so the session records its whole-file-tag
    /// snapshot, and return the tag hex the read emitted in the `[path#TAG]`
    /// header. Fails the test if the header is absent.
    fn read_for_tag(session: &Session, path: &Path) -> String {
        let cache = OutlineCache::new();
        let out = crate::mcp::tools::tool_read(
            &json!({"paths": [path.to_str().unwrap()], "mode": "full", "cwd": path.parent().unwrap().to_str().unwrap()}),
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
        let ops = json!([{ "op": "replace", "start": 1, "end": 1, "content": "fn A() {}" }]);
        let out = tool_write(
            &json!({"edits": edits(&p, Some(&tag), ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("write ok");
        assert!(out.contains("applied"), "expected applied, got:\n{out}");
        assert_eq!(
            std::fs::read_to_string(&p).unwrap(),
            "fn A() {}\nfn b() {}\n",
            "replace 1 must replace only line 1"
        );
    }

    #[test]
    fn replace_content_ending_in_newline_adds_no_blank_line() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("a.rs");
        std::fs::write(&p, "fn a() {}\nfn b() {}\n").unwrap();
        let (session, bloom) = services();

        let tag = read_for_tag(&session, &p);
        // content ends in "\n" — must not splice an extra blank line, matching
        // the old grammar's finalize_payload trailing-blank strip.
        let ops = json!([{ "op": "replace", "start": 1, "end": 1, "content": "fn A() {}\n" }]);
        let out = tool_write(
            &json!({"edits": edits(&p, Some(&tag), ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("write ok");
        assert!(out.contains("applied"), "expected applied, got:\n{out}");
        assert_eq!(
            std::fs::read_to_string(&p).unwrap(),
            "fn A() {}\nfn b() {}\n",
            "trailing newline in content must not add a blank line"
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

        let ops = json!([{ "op": "replace", "start": 3, "end": 3, "content": "RECOVERED" }]);
        let out = tool_write(
            &json!({"edits": edits(&p, Some(&tag), ops), "cwd": root.to_str().unwrap()}),
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
        let ops = json!([{ "op": "replace", "start": 3, "end": 3, "content": "NEW" }]);
        let out = tool_write(
            &json!({"edits": edits(&p, Some(&tag), ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect_err("all sections failed → isError");
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

    /// Minimal seam for Finding 11: same line is drift-conflicted, not the
    /// whole file. Confirms rejection preserves unrelated surrounding lines
    /// and keeps the external change intact.
    #[test]
    fn same_line_drift_conflict_is_rejected_and_preserves_surrounding_lines() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("same_line_conflict.rs");
        std::fs::write(&p, "a\nb\nTARGET\nd\n").unwrap();
        let (session, bloom) = services();
        let tag = read_for_tag(&session, &p);

        // External process rewrites ONLY line 3, leaving a/b/d untouched.
        std::fs::write(&p, "a\nb\nEXTERNAL\nd\n").unwrap();
        let ops = json!([{ "op": "replace", "start": 3, "end": 3, "content": "NEW" }]);
        let out = tool_write(
            &json!({"edits": edits(&p, Some(&tag), ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect_err("same-line drift must be rejected, not silently applied");
        assert!(
            out.contains("error:") && out.contains("changed between read and edit"),
            "same-line drift must be a Drift rejection, got:\n{out}"
        );
        assert_eq!(
            std::fs::read_to_string(&p).unwrap(),
            "a\nb\nEXTERNAL\nd\n",
            "rejected edit must keep the external change and leave a/b/d untouched"
        );
    }

    /// The #195 regression at the real read-to-write seam: one live Session
    /// reads the file (recording the head tag), an external process changes a
    /// nearby line, and the model edits a different line inside the same patch
    /// context window. The old exact-context patch apply rejected this; the
    /// 3-way merge must land BOTH non-overlapping changes. Runs both op shapes
    /// against the same freshly-read head tag.
    #[test]
    fn nearby_external_drift_merges_both_changes_not_rejects() {
        let base = "one\ntwo\nthree\nfour\nfive\nsix\nseven\neight\nnine\nten\neleven\ntwelve\n";
        let external =
            "one\nTWO_EXTERNAL\nthree\nfour\nfive\nsix\nseven\neight\nnine\nten\neleven\ntwelve\n";
        let expected = "one\nTWO_EXTERNAL\nthree\nfour\nFIVE_MODEL\nsix\nseven\neight\nnine\nten\neleven\ntwelve\n";

        // Both ops target line 5 and must survive the line-2 external change.
        let cases = [
            json!([{ "op": "replace", "start": 5, "end": 5, "content": "FIVE_MODEL" }]),
            json!([{ "op": "replace_text", "old": "five", "new": "FIVE_MODEL" }]),
        ];
        for ops in cases {
            let dir = tempfile::tempdir().unwrap();
            let root = dir.path();
            let p = root.join("nearby.txt");
            std::fs::write(&p, base).unwrap();
            // One Session spans the read and the write, exactly as a live
            // process would; the external mutation lands between them.
            let (session, bloom) = services();
            let tag = read_for_tag(&session, &p);
            std::fs::write(&p, external).unwrap();

            let out = tool_write(
                &json!({"edits": edits(&p, Some(&tag), ops), "cwd": root.to_str().unwrap()}),
                &session,
                &bloom,
            )
            .expect("nearby drift must recover, not reject");
            assert!(out.contains("applied"), "expected applied, got:\n{out}");
            let final_bytes = std::fs::read_to_string(&p).unwrap();
            assert_eq!(
                final_bytes, expected,
                "3-way merge must land both non-overlapping changes"
            );
            assert!(
                final_bytes.ends_with('\n'),
                "fixture trailing newline must survive"
            );
        }
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
            &json!({"paths": [format!("{}#outer", p.display())], "cwd": p.parent().unwrap().to_str().unwrap()}),
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
        let ops = json!([{ "op": "replace", "start": 5, "end": 5, "content": "    let y = 9;" }]);
        let out = tool_write(
            &json!({"edits": edits(&p, Some(&tag), ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect_err("all sections failed → isError");
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

    /// A pure `delete_file` against an externally-drifted file must succeed:
    /// file-level intent is independent of content drift.
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
        let ops = json!([{ "op": "delete_file" }]);
        let out = tool_write(
            &json!({"edits": edits(&p, Some(&tag), ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("write ok");
        assert!(
            out.contains("removed"),
            "pure delete_file on a drifted file must remove, not reject, got:\n{out}"
        );
        assert!(!p.exists(), "file must be deleted despite content drift");
    }

    /// A pure `move_file` against an externally-drifted file must move the
    /// (drifted) file rather than hard-reject.
    #[test]
    fn pure_mv_on_drifted_file_moves() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("mvdrift.rs");
        std::fs::write(&p, "one\ntwo\n").unwrap();
        let (session, bloom) = services();
        let tag = read_for_tag(&session, &p);

        std::fs::write(&p, "one\ntwo\nthree\n").unwrap();
        let ops = json!([{ "op": "move_file", "dest": "moved.rs" }]);
        let out = tool_write(
            &json!({"edits": edits(&p, Some(&tag), ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("write ok");
        assert!(
            out.contains("moved"),
            "pure move_file on a drifted file must move, not reject, got:\n{out}"
        );
        assert!(!p.exists(), "source must be gone after move");
        assert_eq!(
            std::fs::read_to_string(root.join("moved.rs")).unwrap(),
            "one\ntwo\nthree\n",
            "the drifted live content is what moves"
        );
    }

    /// The drift path derives its file op through the canonical `FileOp::from_ops`
    /// guard, so two file ops in one drifted section are rejected as a conflict.
    #[test]
    fn conflicting_file_ops_on_drift_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("conflictops.rs");
        std::fs::write(&p, "x\ny\n").unwrap();
        let (session, bloom) = services();
        let tag = read_for_tag(&session, &p);

        std::fs::write(&p, "x\ny\nz\n").unwrap();
        let ops = json!([{ "op": "delete_file" }, { "op": "move_file", "dest": "other.rs" }]);
        let out = tool_write(
            &json!({"edits": edits(&p, Some(&tag), ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect_err("all sections failed → isError");
        assert!(
            out.contains("at most one file op (CREATE/REM/MV)"),
            "delete_file + move_file in one drifted section must be a FileOpConflict, got:\n{out}"
        );
        assert!(p.exists(), "rejected conflict must not remove the file");
        assert!(
            !root.join("other.rs").exists(),
            "rejected conflict must not move the file"
        );
    }

    /// A pure `delete_file` carrying a tag that was never recorded this session
    /// must be rejected as Fabricated and must not delete the file.
    #[test]
    fn pure_rem_with_fabricated_tag_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("neverread.rs");
        std::fs::write(&p, "alpha\nbeta\n").unwrap();
        let (session, bloom) = services();
        let bogus = format!(
            "{:04X}",
            crate::edit::tag::compute_file_hash("alpha\nbeta\n") ^ 0x1
        );
        let ops = json!([{ "op": "delete_file" }]);
        let out = tool_write(
            &json!({"edits": edits(&p, Some(&bogus), ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect_err("all sections failed → isError");
        assert!(
            out.contains("not from this session"),
            "pure delete_file with a never-recorded tag must be Fabricated, got:\n{out}"
        );
        assert!(
            p.exists(),
            "fabricated-tag delete_file must not delete the file"
        );
        assert_eq!(
            std::fs::read_to_string(&p).unwrap(),
            "alpha\nbeta\n",
            "fabricated-tag delete_file must leave the file untouched"
        );
    }

    /// A pure `move_file` carrying a never-recorded tag must be rejected as
    /// Fabricated and must not move the file.
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
        let ops = json!([{ "op": "move_file", "dest": "stolen.rs" }]);
        let out = tool_write(
            &json!({"edits": edits(&p, Some(&bogus), ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect_err("all sections failed → isError");
        assert!(
            out.contains("not from this session"),
            "pure move_file with a never-recorded tag must be Fabricated, got:\n{out}"
        );
        assert!(
            p.exists(),
            "fabricated-tag move_file must not move the source"
        );
        assert!(
            !root.join("stolen.rs").exists(),
            "fabricated-tag move_file must not create the destination"
        );
    }

    /// A 16-bit tag collision after external drift must not silently overwrite
    /// the live drift: when the recorded snapshot's content differs from live
    /// despite equal tags, the edit routes through recovery and is rejected.
    #[test]
    fn tag_collision_after_drift_does_not_silently_overwrite() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("collide.rs");
        let original = "line one\n";
        std::fs::write(&p, original).unwrap();
        let (session, bloom) = services();

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

        std::fs::write(&p, &colliding).unwrap();
        let ops = json!([{ "op": "replace", "start": 1, "end": 1, "content": "overwrite" }]);
        let out = tool_write(
            &json!({"edits": edits(&p, Some(&tag), ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect_err("all sections failed → isError");
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
        let ops = json!([{ "op": "replace", "start": 1, "end": 1, "content": "X" }]);
        let out = tool_write(
            &json!({"edits": edits(&p, Some(&bogus), ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect_err("all sections failed → isError");
        assert!(
            out.contains("not from this session"),
            "unknown tag must be a Fabricated rejection, got:\n{out}"
        );
        assert_eq!(std::fs::read_to_string(&p).unwrap(), "x\ny\n");
    }

    #[test]
    fn path_escape_via_dotdot_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let (session, bloom) = services();
        let evil = Path::new("../evil.rs");
        let ops = json!([{ "op": "replace", "start": 1, "end": 1, "content": "x" }]);
        let out = tool_write(
            &json!({"edits": edits(evil, Some("0000"), ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect_err("all sections failed → isError");
        assert!(
            out.contains("escapes") && out.contains(".."),
            "`..` traversal in a section path must be rejected, got:\n{out}"
        );
        assert!(
            !root.parent().unwrap().join("evil.rs").exists(),
            "no file may be created outside the root"
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
        let ops = json!([{ "op": "replace", "start": 1, "end": 1, "content": "X" }]);
        let out = tool_write(
            &json!({"edits": edits(&target, Some(&tag), ops), "cwd": checkout.path().to_str().unwrap()}),
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
        let ops = json!([{ "op": "move_file", "dest": "../escaped.rs" }]);
        let out = tool_write(
            &json!({"edits": edits(&p, Some(&tag), ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect_err("all sections failed → isError");
        assert!(
            out.contains("escapes") && out.contains(".."),
            "move_file dest with `..` must be rejected, got:\n{out}"
        );
        assert!(p.exists(), "source file must remain after a rejected move");
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
            &json!({"paths": [format!("{}#1-2", p.display())], "cwd": p.parent().unwrap().to_str().unwrap()}),
            &cache,
            &session,
            true,
        )
        .expect("range read");
        let tag = format!("{:04X}", compute_file_hash("a\nb\nc\nd\n"));
        let ops = json!([{ "op": "move_file", "dest": "dest.rs" }]);
        let out = tool_write(
            &json!({"edits": edits(&p, Some(&tag), ops), "cwd": root.to_str().unwrap()}),
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
        let ops2 = json!([{ "op": "replace", "start": 4, "end": 4, "content": "D" }]);
        let rej = tool_write(
            &json!({"edits": edits(&dest, Some(&tag), ops2), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect_err("all sections failed → isError");
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

    #[cfg(unix)]
    #[test]
    fn mv_moves_file_and_relocates_snapshot_through_symlinked_dir() {
        // `link/` is a directory symlink to `real/`; every path passed to the
        // tools uses the `link/...` spelling so the canonical key minted at
        // record time (`real/...`, via canonicalize) diverges from the lexical
        // spelling. Only the parent dir is symlinked, so the fs ops still hit
        // the real file.
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let real = root.join("real");
        std::fs::create_dir(&real).unwrap();
        let link = root.join("link");
        std::os::unix::fs::symlink(&real, &link).unwrap();
        let p = link.join("src.rs");
        std::fs::write(real.join("src.rs"), "a\nb\nc\nd\n").unwrap();
        let (session, bloom) = services();
        let cache = OutlineCache::new();
        // Range read via the `link/` spelling records seen-lines {1,2} under
        // the canonical `real/src.rs` key.
        crate::mcp::tools::tool_read(
            &json!({"paths": [format!("{}#1-2", p.display())], "cwd": link.to_str().unwrap()}),
            &cache,
            &session,
            true,
        )
        .expect("range read");
        let tag = format!("{:04X}", compute_file_hash("a\nb\nc\nd\n"));
        let ops = json!([{ "op": "move_file", "dest": "dest.rs" }]);
        let out = tool_write(
            &json!({"edits": edits(&p, Some(&tag), ops), "cwd": link.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("write ok");
        assert!(out.contains("moved"), "expected moved, got:\n{out}");
        let dest_link = link.join("dest.rs");
        assert_eq!(
            std::fs::read_to_string(real.join("dest.rs")).unwrap(),
            "a\nb\nc\nd\n"
        );

        // The relocated snapshot must carry src's seen-lines {1,2} even though
        // src and dest were only ever addressed via the `link/` spelling: an
        // edit on the never-displayed line 4 of dest is rejected by the
        // seen-lines gate. Pre-fix, canonicalizing after the rename falls back
        // to the unresolved `link/src.rs` lexical spelling, misses the
        // `real/src.rs` canonical key `record()` used, and the gate is
        // silently skipped.
        let ops2 = json!([{ "op": "replace", "start": 4, "end": 4, "content": "D" }]);
        let rej = tool_write(
            &json!({"edits": edits(&dest_link, Some(&tag), ops2), "cwd": link.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect_err("all sections failed → isError");
        assert!(
            rej.contains("never displayed"),
            "relocated snapshot must gate an unseen line at dest, got:\n{rej}"
        );
        assert_eq!(
            std::fs::read_to_string(real.join("dest.rs")).unwrap(),
            "a\nb\nc\nd\n",
            "rejected edit must not touch dest"
        );
    }

    #[test]
    fn mv_onto_existing_different_file_is_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let src = root.join("src.rs");
        let dest = root.join("dest.rs");
        std::fs::write(&src, "source content\n").unwrap();
        std::fs::write(&dest, "dest content\n").unwrap();
        let (session, bloom) = services();
        let tag = read_for_tag(&session, &src);
        let ops = json!([{ "op": "move_file", "dest": "dest.rs" }]);
        let out = tool_write(
            &json!({"edits": edits(&src, Some(&tag), ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect_err("all sections failed → isError");
        assert!(
            out.contains("already exists"),
            "move onto an existing different file must be rejected, got:\n{out}"
        );
        assert!(src.exists(), "source must remain after a rejected move");
        assert_eq!(
            std::fs::read_to_string(&dest).unwrap(),
            "dest content\n",
            "destination content must be untouched by the rejected move"
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
        let ops = json!([{ "op": "delete_file" }]);
        let out = tool_write(
            &json!({"edits": edits(&p, Some(&tag), ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("write ok");
        assert!(out.contains("removed"), "expected removed, got:\n{out}");
        assert!(!p.exists(), "file must be deleted by delete_file");

        // Recreate the path with fresh content. The pre-delete snapshot must have
        // been invalidated: the old tag is no longer known, so a stale-tag edit
        // is a Fabricated rejection ("not from this session").
        std::fs::write(&p, "fresh content here\n").unwrap();
        let stale = json!([{ "op": "replace", "start": 1, "end": 1, "content": "X" }]);
        let out2 = tool_write(
            &json!({"edits": edits(&p, Some(&tag), stale), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect_err("all sections failed → isError");
        assert!(
            out2.contains("not from this session"),
            "post-delete edit with the old tag must be Fabricated, got:\n{out2}"
        );
        assert_eq!(
            std::fs::read_to_string(&p).unwrap(),
            "fresh content here\n",
            "rejected stale edit must not touch the recreated file"
        );
    }

    #[cfg(unix)]
    #[test]
    fn rem_removes_file_and_invalidates_snapshot_through_symlinked_dir() {
        // `link/` is a directory symlink to `real/`; every path passed to the
        // tools uses the `link/...` spelling so the canonical key minted at
        // record time (`real/...`, via canonicalize) diverges from the lexical
        // spelling. Only the parent dir is symlinked, so the fs op still hits
        // the real file.
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let real = root.join("real");
        std::fs::create_dir(&real).unwrap();
        let link = root.join("link");
        std::os::unix::fs::symlink(&real, &link).unwrap();
        let p = link.join("gone.rs");
        std::fs::write(real.join("gone.rs"), "bye\n").unwrap();
        let (session, bloom) = services();
        let tag = read_for_tag(&session, &p);
        let ops = json!([{ "op": "delete_file" }]);
        let out = tool_write(
            &json!({"edits": edits(&p, Some(&tag), ops), "cwd": link.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("write ok");
        assert!(out.contains("removed"), "expected removed, got:\n{out}");
        assert!(
            !real.join("gone.rs").exists(),
            "file must be deleted by delete_file"
        );

        // Recreate the path (via `real/`, since `link/` still resolves there)
        // with fresh content. The pre-delete snapshot must have been
        // invalidated: the old tag is no longer known, so a stale-tag edit is
        // a Fabricated rejection ("not from this session"). Pre-fix,
        // canonicalizing after the remove falls back to the unresolved
        // `link/gone.rs` lexical spelling, misses the `real/gone.rs` canonical
        // key `record()` used, so invalidation misses and the stale edit is
        // wrongly recovered instead.
        std::fs::write(real.join("gone.rs"), "fresh content here\n").unwrap();
        let stale = json!([{ "op": "replace", "start": 1, "end": 1, "content": "X" }]);
        let out2 = tool_write(
            &json!({"edits": edits(&p, Some(&tag), stale), "cwd": link.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect_err("all sections failed → isError");
        assert!(
            out2.contains("not from this session"),
            "post-delete edit with the old tag must be Fabricated, got:\n{out2}"
        );
        assert_eq!(
            std::fs::read_to_string(real.join("gone.rs")).unwrap(),
            "fresh content here\n",
            "rejected stale edit must not touch the recreated file"
        );
    }

    #[test]
    fn tagless_section_seeds_new_file() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let (session, bloom) = services();
        let p = root.join("new.rs");
        let ops = json!([{ "op": "prepend", "content": "fn seeded() {}" }]);
        let out = tool_write(
            &json!({"edits": edits(&p, None, ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("write ok");
        assert!(out.contains("applied"), "expected applied, got:\n{out}");
        assert_eq!(
            std::fs::read_to_string(&p).unwrap(),
            "fn seeded() {}\n",
            "tagless section seeds the file with the inserted content"
        );
    }

    #[test]
    fn tagless_text_swap_is_rejected_without_modifying_file() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("tagless.rs");
        let original = "head target\n";
        std::fs::write(&p, original).unwrap();
        let (session, _bloom) = services();
        let section = Section {
            path: p.to_string_lossy().into_owned(),
            tag: None,
            ops: vec![Op::TextSwap {
                old: "target".into(),
                new: "replacement".into(),
            }],
        };
        let err = resolve_edit(&section, &p, &session, original).unwrap_err();
        match err {
            // The rejection must name the cheap route to a tag; stating only
            // the requirement drove agents into a full re-read of a large file
            // when a section read would have supplied the same whole-file tag.
            TilthError::EditRejected(message) => {
                assert!(
                    message.starts_with("replace_text requires a tag from an edit-mode read"),
                    "unexpected rejection: {message}"
                );
                assert!(
                    message.contains("section read"),
                    "rejection must point at the section-read route: {message}"
                );
                assert!(
                    message.contains("over the tag cap"),
                    "rejection must name the over-cap case that has no tag: {message}"
                );
                // Pointing at the section read without this constraint sent
                // agents into a second failed round trip via UnseenAnchor.
                assert!(
                    message.contains("must occur in the lines it displayed"),
                    "rejection must state the seen-lines constraint: {message}"
                );
            }
            other => panic!("expected EditRejected, got {other:?}"),
        }
        assert_eq!(std::fs::read_to_string(&p).unwrap(), original);
    }

    #[test]
    fn replace_text_applies_through_tool_write_and_writes_exact_bytes() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("swap.rs");
        std::fs::write(&p, "fn a() { target(); }\n").unwrap();
        let (session, bloom) = services();
        let tag = read_for_tag(&session, &p);
        let ops = json!([{ "op": "replace_text", "old": "target", "new": "renamed" }]);
        let out = tool_write(
            &json!({"edits": edits(&p, Some(&tag), ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("write ok");
        assert!(out.contains("applied"), "expected applied, got:\n{out}");
        assert_eq!(
            std::fs::read_to_string(&p).unwrap(),
            "fn a() { renamed(); }\n",
            "replace_text must substitute the matched text exactly once, in place"
        );
    }

    #[test]
    fn replace_text_with_absent_old_is_a_per_section_error_and_leaves_file_untouched() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("absent.rs");
        let original = "fn a() { present(); }\n";
        std::fs::write(&p, original).unwrap();
        let (session, bloom) = services();
        let tag = read_for_tag(&session, &p);
        let ops = json!([{ "op": "replace_text", "old": "missing", "new": "renamed" }]);
        let out = tool_write(
            &json!({"edits": edits(&p, Some(&tag), ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect_err("all sections failed → isError");
        // Decision 4: the not-found message teaches provenance — copy `old`
        // verbatim from the numbered read, naming the exact [path#TAG] source.
        assert_eq!(
            out,
            format!(
                "## {p}\nerror: text to replace was not found; copy old verbatim from the numbered lines of [{p}#{tag}] — do not retype it from memory or shell output (preview: missing)",
                p = p.display()
            )
        );
        assert_eq!(std::fs::read_to_string(&p).unwrap(), original);
    }

    #[test]
    fn replace_text_with_ambiguous_old_is_a_per_section_error_and_leaves_file_untouched() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("ambiguous.rs");
        let original = "fn a() { target(); }\nfn b() { target(); }\n";
        std::fs::write(&p, original).unwrap();
        let (session, bloom) = services();
        let tag = read_for_tag(&session, &p);
        let ops = json!([{ "op": "replace_text", "old": "target", "new": "renamed" }]);
        let out = tool_write(
            &json!({"edits": edits(&p, Some(&tag), ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect_err("all sections failed → isError");
        assert_eq!(
            out,
            format!(
                "## {}\nerror: text to replace matched at least 2 times; add context so it matches once",
                p.display()
            )
        );
        assert_eq!(std::fs::read_to_string(&p).unwrap(), original);
    }

    #[test]
    fn drift_with_unmatched_replace_text_names_text_match_not_bare_drift() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("drift_swap.rs");
        std::fs::write(&p, "alpha\nbeta\ngamma\n").unwrap();
        let (session, bloom) = services();
        let tag = read_for_tag(&session, &p);

        // Drift the file so its hash no longer matches the tag, and make sure
        // the replace_text anchor is absent from both the snapshot and the
        // drifted live text so recovery cannot land the edit either way.
        std::fs::write(&p, "alpha\nCHANGED\ngamma\n").unwrap();
        let ops = json!([{ "op": "replace_text", "old": "missing-text", "new": "replacement" }]);
        let out = tool_write(
            &json!({"edits": edits(&p, Some(&tag), ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect_err("all sections failed → isError");
        assert!(
            out.contains("Edit rejected for"),
            "expected an Edit rejected message, got:\n{out}"
        );
        assert!(
            out.contains("The file also changed since the read that minted this tag"),
            "a drifted unmatched replace_text must report TextMatch, not bare Drift: {out}"
        );
        assert!(
            out.contains("copy old verbatim from the numbered lines of"),
            "drift + text-unmatched must still teach provenance: {out}"
        );
        assert_eq!(
            std::fs::read_to_string(&p).unwrap(),
            "alpha\nCHANGED\ngamma\n",
            "rejected edit must not touch the file"
        );
    }

    #[test]
    fn create_file_creates_missing_parent_directory() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("newmod").join("thing.rs");
        let content = "fn thing() {}\n";
        let (session, bloom) = services();
        let ops = json!([{ "op": "create_file", "content": content }]);

        tool_write(
            &json!({"edits": edits(&p, None, ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("create_file creates missing parent directory");

        assert_eq!(std::fs::read_to_string(&p).unwrap(), content);
    }

    #[test]
    fn create_file_new_file_succeeds() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("created.rs");
        let content = "fn created() {}\n// exact\n";
        let (session, bloom) = services();
        let ops = json!([{ "op": "create_file", "content": content }]);

        let out = tool_write(
            &json!({"edits": edits(&p, None, ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("create_file succeeds");

        let header_line = format!("[{}#", p.display());
        assert!(
            out.starts_with(&format!("## {}\ncreated\n{header_line}", p.display())),
            "expected created output with tag header, got: {out}"
        );
        assert!(
            !out.contains(content),
            "created output must not echo file body: {out}"
        );
        assert_eq!(std::fs::read_to_string(&p).unwrap(), content);
    }

    #[test]
    fn create_file_existing_file_errors() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("existing.rs");
        let original = "original\n";
        std::fs::write(&p, original).unwrap();
        let (session, bloom) = services();
        let ops = json!([{ "op": "create_file", "content": "replacement\n" }]);

        let out = tool_write(
            &json!({"edits": edits(&p, None, ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect_err("all sections failed → isError");

        assert_eq!(
            out,
            format!(
                "## {}\nerror: create_file target already exists: {} — use a tagged read with replace instead",
                p.display(),
                p.display()
            )
        );
        assert_eq!(std::fs::read_to_string(&p).unwrap(), original);
    }

    #[cfg(unix)]
    #[test]
    fn create_file_dangling_symlink_rejected_without_target_creation() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let target = root.join("missing.rs");
        let link = root.join("link.rs");
        std::os::unix::fs::symlink(&target, &link).unwrap();
        let (session, bloom) = services();
        let ops = json!([{ "op": "create_file", "content": "replacement\n" }]);

        let out = tool_write(
            &json!({"edits": edits(&link, None, ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect_err("all sections failed → isError");

        assert_eq!(
            out,
            format!(
                "## {}\nerror: create_file target already exists: {} — use a tagged read with replace instead",
                link.display(),
                link.display()
            )
        );
        assert!(link.is_symlink(), "dangling symlink must remain untouched");
        assert_eq!(std::fs::read_link(&link).unwrap(), target);
        assert!(
            !target.exists(),
            "create_file must not create the symlink target"
        );
    }

    #[test]
    fn create_file_tag_present_errors() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("tagged.rs");
        let (session, bloom) = services();
        let ops = json!([{ "op": "create_file", "content": "new\n" }]);

        let out = tool_write(
            &json!({
                "edits": edits(&p, Some("ABCD"), ops),
                "cwd": root.to_str().unwrap()
            }),
            &session,
            &bloom,
        )
        .expect_err("all sections failed → isError");

        assert_eq!(
            out,
            format!(
                "## {}\nerror: create_file requires a tagless section for a new file: {} — use a tagged read with replace instead",
                p.display(),
                p.display()
            )
        );
        assert!(!p.exists(), "tagged create must not create the file");
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

    /// A string `edits` (the legacy `[path#TAG]` grammar) is rejected with a
    /// teaching error that shows the JSON translation — before any file work.
    #[test]
    fn legacy_blob_string_yields_teaching_error() {
        let (session, bloom) = services();
        let err = tool_write(&json!({"edits": "[a.rs#0000]\nDEL 1\n"}), &session, &bloom)
            .expect_err("legacy blob string must be a teaching error");
        assert!(
            err.contains("JSON array"),
            "must teach the new shape: {err}"
        );
        assert!(
            err.contains("\"op\": \"delete\""),
            "must render the DEL as a delete op: {err}"
        );
    }

    /// A double-encoded array (JSON payload wrapped in a string) is rejected
    /// with an error naming the double-encoding and showing the unwrapped form.
    #[test]
    fn double_encoded_string_yields_teaching_error() {
        let (session, bloom) = services();
        let encoded = "[{\"path\":\"a.rs\",\"tag\":\"0000\",\"ops\":[]}]";
        let err = tool_write(&json!({"edits": encoded}), &session, &bloom)
            .expect_err("double-encoded array must be a teaching error");
        assert!(
            err.contains("double-encoded"),
            "must name the mistake: {err}"
        );
        assert!(
            err.contains("\"path\": \"a.rs\""),
            "must show the unwrapped form: {err}"
        );
    }

    /// An op that fails validation is rejected at the deserialize layer, naming
    /// the op and the offending field, before any file is touched.
    #[test]
    fn schema_rejection_names_op_and_field_before_file_touched() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("keep.rs");
        std::fs::write(&p, "untouched\n").unwrap();
        let (session, bloom) = services();
        // `replace` missing its `content` field.
        let ops = json!([{ "op": "replace", "start": 1, "end": 2 }]);
        let err = tool_write(
            &json!({"edits": edits(&p, Some("0000"), ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect_err("invalid op must be a top-level deserialize error");
        assert!(err.contains("replace"), "must name the op: {err}");
        assert!(err.contains("content"), "must name the field: {err}");
        assert_eq!(
            std::fs::read_to_string(&p).unwrap(),
            "untouched\n",
            "no file may be touched when the op fails validation"
        );
    }

    /// More than 20 sections is rejected at the batch cap before any apply.
    #[test]
    fn batch_over_twenty_sections_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let (session, bloom) = services();
        let sections: Vec<Value> = (0..21)
            .map(|i| {
                json!({
                    "path": format!("f{i}.rs"),
                    "tag": "0000",
                    "ops": [{ "op": "delete_file" }]
                })
            })
            .collect();
        let err = tool_write(
            &json!({"edits": Value::Array(sections), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect_err("21 sections must exceed the cap");
        assert!(err.contains("20 sections"), "must name the cap: {err}");
    }

    #[test]
    fn multi_section_write_lands_edits_in_both_files() {
        // Wiring seam: the section loop must apply every section in one array,
        // landing independent edits in independent files.
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let a = root.join("one.rs");
        let b = root.join("two.rs");
        std::fs::write(&a, "fn one() {}\n").unwrap();
        std::fs::write(&b, "fn two() {}\n").unwrap();
        let (session, bloom) = services();
        let tag_a = read_for_tag(&session, &a);
        let tag_b = read_for_tag(&session, &b);
        let edits_val = json!([
            {
                "path": a.to_str().unwrap(),
                "tag": tag_a,
                "ops": [{ "op": "replace", "start": 1, "end": 1, "content": "fn ONE() {}" }]
            },
            {
                "path": b.to_str().unwrap(),
                "tag": tag_b,
                "ops": [{ "op": "replace", "start": 1, "end": 1, "content": "fn TWO() {}" }]
            }
        ]);
        let out = tool_write(
            &json!({"edits": edits_val, "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("write ok");
        assert_eq!(
            out.matches("applied").count(),
            2,
            "both sections must apply, got:\n{out}"
        );
        assert!(out.contains(&format!("## {}", a.display())));
        assert!(out.contains(&format!("## {}", b.display())));
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
        let edits_val = json!([
            {
                "path": p.to_str().unwrap(),
                "tag": tag,
                "ops": [{ "op": "replace", "start": 1, "end": 1, "content": "fn A() {}" }]
            },
            {
                "path": p.to_str().unwrap(),
                "tag": tag,
                "ops": [{ "op": "replace", "start": 2, "end": 2, "content": "fn B() {}" }]
            }
        ]);
        let out = tool_write(
            &json!({"edits": edits_val, "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("per-section error returns Ok");
        assert!(
            out.contains("duplicate path"),
            "second section on same path must be a duplicate-path error, got:\n{out}"
        );
        assert_eq!(
            std::fs::read_to_string(&p).unwrap(),
            "fn A() {}\nfn b() {}\n",
            "only the first section's edit lands; the duplicate is dropped"
        );
    }

    /// A `#symbol` edit-mode read records only the symbol's span as seen, so a
    /// tag-matched edit anchored INSIDE that span applies but one anchored on a
    /// never-displayed line is rejected.
    #[test]
    fn symbol_read_gates_edit_to_displayed_span() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("sym.rs");
        let content = "fn outer() {\n    let x = 1;\n}\nfn other() {\n    let y = 2;\n}\n";
        std::fs::write(&p, content).unwrap();
        let (session, bloom) = services();

        let cache = OutlineCache::new();
        let sym_out = crate::mcp::tools::tool_read(
            &json!({"paths": [format!("{}#outer", p.display())], "cwd": p.parent().unwrap().to_str().unwrap()}),
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
        let reject_ops =
            json!([{ "op": "replace", "start": 5, "end": 5, "content": "    let y = 9;" }]);
        let reject = tool_write(
            &json!({"edits": edits(&p, Some(&tag), reject_ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect_err("all sections failed → isError");
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
        let ok_ops =
            json!([{ "op": "replace", "start": 2, "end": 2, "content": "    let x = 42;" }]);
        let ok = tool_write(
            &json!({"edits": edits(&p, Some(&tag), ok_ops), "cwd": root.to_str().unwrap()}),
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

        crate::mcp::tools::tool_read(
            &json!({"paths": [p.to_str().unwrap()], "mode": "signature", "cwd": p.parent().unwrap().to_str().unwrap()}),
            &cache,
            &session,
            true,
        )
        .expect("signature read");
        crate::mcp::tools::tool_read(
            &json!({"paths": [format!("{}#1-1", p.display())], "cwd": p.parent().unwrap().to_str().unwrap()}),
            &cache,
            &session,
            true,
        )
        .expect("range read");

        let tag = format!("{:04X}", compute_file_hash(content));
        let ops = json!([{ "op": "replace", "start": 3, "end": 3, "content": "fn C() {}" }]);
        let out = tool_write(
            &json!({"edits": edits(&p, Some(&tag), ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect_err("all sections failed → isError");
        assert!(
            out.contains("never displayed"),
            "signature read must not grant seen-lines; line-3 edit must be rejected, got:\n{out}"
        );
        assert_eq!(std::fs::read_to_string(&p).unwrap(), content);
    }

    /// A file with no trailing newline round-trips: the read mints a tag, and a
    /// tag-matched replace lands on the intended line without corrupting the
    /// missing-final-newline shape.
    #[test]
    fn no_trailing_newline_round_trip() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("nonl.rs");
        std::fs::write(&p, "fn a() {}\nfn b() {}").unwrap();
        let (session, bloom) = services();
        let tag = read_for_tag(&session, &p);
        let ops = json!([{ "op": "replace", "start": 2, "end": 2, "content": "fn B() {}" }]);
        let out = tool_write(
            &json!({"edits": edits(&p, Some(&tag), ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("write ok");
        assert!(out.contains("applied"), "expected applied, got:\n{out}");
        assert_eq!(
            std::fs::read_to_string(&p).unwrap(),
            "fn a() {}\nfn B() {}",
            "replace 2 replaces line 2 and preserves the no-trailing-newline shape"
        );
    }

    /// An integer op field beyond u32 range is rejected at the deserialize
    /// layer — naming the op — before any file is touched. Locks the numeric
    /// bound of the "reject before any file work" acceptance criterion.
    #[test]
    fn u32_out_of_range_op_field_rejected_before_file_touched() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("bounds.rs");
        std::fs::write(&p, "untouched\n").unwrap();
        let (session, bloom) = services();
        // start = u32::MAX + 1 — out of range for the wire field.
        let ops = json!([{ "op": "replace", "start": 4_294_967_296i64, "end": 1, "content": "x" }]);
        let err = tool_write(
            &json!({"edits": edits(&p, Some("0000"), ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect_err("out-of-range integer must be a top-level deserialize error");
        assert!(err.contains("replace"), "must name the op: {err}");
        assert!(err.contains("u32"), "must name the expected type: {err}");
        assert_eq!(
            std::fs::read_to_string(&p).unwrap(),
            "untouched\n",
            "no file may be touched when an op field is out of range"
        );
    }

    /// An unrecognized `op` verb is rejected at the deserialize layer, echoing
    /// the offending verb, before any file is touched.
    #[test]
    fn unknown_op_verb_rejected_naming_the_verb() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("keepverb.rs");
        std::fs::write(&p, "stable\n").unwrap();
        let (session, bloom) = services();
        let ops = json!([{ "op": "frobnicate", "start": 1, "end": 1 }]);
        let err = tool_write(
            &json!({"edits": edits(&p, Some("0000"), ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect_err("unknown verb must be a top-level deserialize error");
        assert!(err.contains("frobnicate"), "must echo the bad verb: {err}");
        assert_eq!(
            std::fs::read_to_string(&p).unwrap(),
            "stable\n",
            "no file may be touched when the op verb is unknown"
        );
    }

    /// Deserialize is all-or-nothing: an invalid op in a LATER section aborts
    /// the whole call before the apply loop, so a valid earlier section's file
    /// is left untouched. Best-effort per-section reporting begins only at the
    /// apply stage, never at the deserialize gate.
    #[test]
    fn deserialize_failure_in_later_section_leaves_earlier_file_untouched() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let a = root.join("first.rs");
        let b = root.join("second.rs");
        std::fs::write(&a, "fn keep() {}\n").unwrap();
        std::fs::write(&b, "fn other() {}\n").unwrap();
        let (session, bloom) = services();
        let tag_a = read_for_tag(&session, &a);
        // Section 0 is valid and would apply; section 1 carries an invalid op.
        let edits_val = json!([
            {
                "path": a.to_str().unwrap(),
                "tag": tag_a,
                "ops": [{ "op": "replace", "start": 1, "end": 1, "content": "fn KEEP() {}" }]
            },
            {
                "path": b.to_str().unwrap(),
                "tag": "0000",
                "ops": [{ "op": "replace", "start": 1, "end": 1 }]
            }
        ]);
        let err = tool_write(
            &json!({"edits": edits_val, "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect_err("invalid op in a later section must abort the whole call");
        assert!(
            err.contains("content"),
            "must name the missing field: {err}"
        );
        assert_eq!(
            std::fs::read_to_string(&a).unwrap(),
            "fn keep() {}\n",
            "the valid earlier section's file must be untouched when a later section fails to deserialize"
        );
    }

    #[test]
    fn create_file_combined_with_content_op_is_a_file_op_conflict_and_does_not_create() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("conflict_create.rs");
        let (session, bloom) = services();
        let ops = json!([
            { "op": "create_file", "content": "fn created() {}\n" },
            { "op": "append", "content": "fn extra() {}" }
        ]);
        let out = tool_write(
            &json!({"edits": edits(&p, None, ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect_err("all sections failed → isError");
        assert_eq!(
            out,
            format!(
                "## {}\nerror: CREATE/REM cannot combine with content ops; at most one file op (CREATE/REM/MV) per section",
                p.display()
            )
        );
        assert!(
            !p.exists(),
            "create_file combined with a content op must not create the file"
        );
    }

    #[test]
    fn two_create_file_ops_in_one_section_is_a_file_op_conflict() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("double_create.rs");
        let (session, bloom) = services();
        let ops = json!([
            { "op": "create_file", "content": "first\n" },
            { "op": "create_file", "content": "second\n" }
        ]);
        let out = tool_write(
            &json!({"edits": edits(&p, None, ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect_err("all sections failed → isError");
        assert_eq!(
            out,
            format!(
                "## {}\nerror: CREATE/REM cannot combine with content ops; at most one file op (CREATE/REM/MV) per section",
                p.display()
            )
        );
        assert!(
            !p.exists(),
            "two create_file ops in one section must not create the file"
        );
    }

    /// Decision 5: an exact-miss `replace_text` that resolves only through the
    /// whitespace-normalized fallback applies over the original span and flags
    /// the normalization on the status line.
    #[test]
    fn replace_text_normalized_match_notes_whitespace_normalization() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("ws.rs");
        // The file indents with a tab; the edit's `old` uses spaces, so the
        // exact match fails and only the normalized fallback can land it.
        std::fs::write(&p, "fn a() {\n\tlet y = 2;\n}\n").unwrap();
        let (session, bloom) = services();
        let tag = read_for_tag(&session, &p);
        let ops = json!([{ "op": "replace_text", "old": "    let y = 2;", "new": "let y = 42;" }]);
        let out = tool_write(
            &json!({"edits": edits(&p, Some(&tag), ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("write ok");
        assert!(
            out.contains("applied (matched with whitespace normalization)"),
            "status line must flag the normalized fallback, got:\n{out}"
        );
        assert_eq!(
            std::fs::read_to_string(&p).unwrap(),
            "fn a() {\n\tlet y = 42;\n}\n",
            "new splices over the original span, preserving the file's tab indent"
        );
    }

    /// F6/F7 regression: a drifted tag plus an `old` that only matches after
    /// whitespace normalization must recover through the 3-way-merge path AND
    /// still carry the normalized-match note, not just "applied".
    #[test]
    fn drifted_tag_with_normalized_replace_text_notes_whitespace_normalization() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("drift_ws.rs");
        std::fs::write(&p, "fn a() {\n\tlet y = 2;\n}\n").unwrap();
        let (session, bloom) = services();
        let tag = read_for_tag(&session, &p);

        // External edit prepends an unrelated line, drifting the tag while
        // leaving the target line's tab indent (and its surrounding context)
        // untouched, so the 3-way merge still lands cleanly.
        std::fs::write(&p, "// note\nfn a() {\n\tlet y = 2;\n}\n").unwrap();

        // `old` uses spaces where the file uses a tab, so only the
        // whitespace-normalized fallback can resolve the swap.
        let ops = json!([{ "op": "replace_text", "old": "    let y = 2;", "new": "let y = 42;" }]);
        let out = tool_write(
            &json!({"edits": edits(&p, Some(&tag), ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("drifted write recovers");
        assert!(
            out.contains("applied (matched with whitespace normalization)"),
            "recovered status line must flag the normalized fallback, got:\n{out}"
        );
        assert_eq!(
            std::fs::read_to_string(&p).unwrap(),
            "// note\nfn a() {\n\tlet y = 42;\n}\n",
            "3-way merge must land the normalized swap at the shifted position"
        );
    }

    /// F6/F7 regression: one section combining `move_file` with a
    /// whitespace-normalized `replace_text` must carry the normalization
    /// suffix on the `moved` status line, not just the bare move.
    #[test]
    fn move_file_combined_with_normalized_replace_text_notes_whitespace_normalization() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("mv_ws.rs");
        std::fs::write(&p, "fn a() {\n\tlet y = 2;\n}\n").unwrap();
        let (session, bloom) = services();
        let tag = read_for_tag(&session, &p);

        let ops = json!([
            { "op": "move_file", "dest": "mv_ws_dest.rs" },
            { "op": "replace_text", "old": "    let y = 2;", "new": "let y = 42;" }
        ]);
        let out = tool_write(
            &json!({"edits": edits(&p, Some(&tag), ops), "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("write ok");
        assert!(
            out.contains("moved (matched with whitespace normalization)"),
            "status line must carry both the move and the normalization suffix, got:\n{out}"
        );
        assert!(!p.exists(), "source removed after move");
        assert_eq!(
            std::fs::read_to_string(root.join("mv_ws_dest.rs")).unwrap(),
            "fn a() {\n\tlet y = 42;\n}\n",
            "the normalized swap must land in the moved content"
        );
    }

    /// Decision 6: a call whose only section is rejected surfaces `isError: true`
    /// (an `Err` from `tool_write`), so dashboards keyed on the MCP flag see it.
    #[test]
    fn sole_rejected_section_surfaces_is_error() {
        // Two sections, both rejected: the call must surface isError (Err) AND
        // the joined body must name both files, not just report the flag.
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let a = root.join("a.rs");
        let b = root.join("b.rs");
        std::fs::write(&a, "fn a() {}\n").unwrap();
        std::fs::write(&b, "fn b() {}\n").unwrap();
        let (session, bloom) = services();
        let bogus_a = format!("{:04X}", compute_file_hash("fn a() {}\n") ^ 0x1);
        let bogus_b = format!("{:04X}", compute_file_hash("fn b() {}\n") ^ 0x1);
        let edits_val = json!([
            {
                "path": a.to_str().unwrap(),
                "tag": bogus_a,
                "ops": [{ "op": "replace", "start": 1, "end": 1, "content": "X" }]
            },
            {
                "path": b.to_str().unwrap(),
                "tag": bogus_b,
                "ops": [{ "op": "replace", "start": 1, "end": 1, "content": "Y" }]
            }
        ]);
        let out = tool_write(
            &json!({"edits": edits_val, "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect_err("every section failed → isError:true");
        assert!(
            out.contains(&format!("## {}\nerror:", a.display())),
            "missing a's block, got:\n{out}"
        );
        assert!(
            out.contains(&format!("## {}\nerror:", b.display())),
            "missing b's block, got:\n{out}"
        );
        assert_eq!(std::fs::read_to_string(&a).unwrap(), "fn a() {}\n");
        assert_eq!(std::fs::read_to_string(&b).unwrap(), "fn b() {}\n");
    }

    /// Decision 6: a mixed call (one section applies, one is rejected) stays
    /// `isError: false` (an `Ok`) and carries both section blocks.
    #[test]
    fn mixed_call_one_rejection_stays_ok_with_both_blocks() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let a = root.join("good.rs");
        let b = root.join("bad.rs");
        std::fs::write(&a, "fn a() {}\n").unwrap();
        std::fs::write(&b, "fn b() {}\n").unwrap();
        let (session, bloom) = services();
        let tag_a = read_for_tag(&session, &a);
        let bogus = format!("{:04X}", compute_file_hash("fn b() {}\n") ^ 0x1);
        let edits_val = json!([
            {
                "path": a.to_str().unwrap(),
                "tag": tag_a,
                "ops": [{ "op": "replace", "start": 1, "end": 1, "content": "fn A() {}" }]
            },
            {
                "path": b.to_str().unwrap(),
                "tag": bogus,
                "ops": [{ "op": "replace", "start": 1, "end": 1, "content": "fn B() {}" }]
            }
        ]);
        let out = tool_write(
            &json!({"edits": edits_val, "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("a mixed call keeps isError:false (Ok)");
        assert!(
            out.contains("applied"),
            "the good section must apply, got:\n{out}"
        );
        assert!(
            out.contains("not from this session"),
            "the bad section must still report its error, got:\n{out}"
        );
        assert_eq!(std::fs::read_to_string(&a).unwrap(), "fn A() {}\n");
        assert_eq!(
            std::fs::read_to_string(&b).unwrap(),
            "fn b() {}\n",
            "the rejected section must not touch its file"
        );
    }

    #[test]
    fn large_one_line_edit_does_not_echo_the_whole_file() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("large.txt");
        let original = (1..=1031)
            .map(|i| format!("line {i}"))
            .collect::<Vec<_>>()
            .join("\n");
        std::fs::write(&p, &original).unwrap();
        let (session, bloom) = services();
        let tag = read_for_tag(&session, &p);
        let out = tool_write(
            &json!({
                "edits": edits(
                    &p,
                    Some(&tag),
                    json!([{ "op": "replace", "start": 1, "end": 1, "content": "changed" }])
                ),
                "cwd": root.to_str().unwrap()
            }),
            &session,
            &bloom,
        )
        .expect("write ok");
        assert!(out.contains("applied"), "expected applied, got:\n{out}");
        assert!(
            out.contains("1:changed"),
            "changed line must remain visible: {out}"
        );
        assert!(
            !out.contains("1031:line 1031"),
            "write output must not echo the whole file: {} bytes",
            out.len()
        );
        assert!(
            out.len() < 20_000,
            "bounded output grew to {} bytes",
            out.len()
        );
    }

    fn tag_from_output(path: &Path, output: &str) -> String {
        let marker = format!("{}#", path.display());
        let idx = output
            .find(&marker)
            .unwrap_or_else(|| panic!("write must emit [path#TAG] header, got:\n{output}"));
        output[idx + marker.len()..].chars().take(4).collect()
    }

    #[test]
    fn diff_output_stays_bounded_for_a_large_one_line_edit() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("large-diff.txt");
        let original = (1..=1031)
            .map(|i| format!("line {i}"))
            .collect::<Vec<_>>()
            .join("\n");
        std::fs::write(&p, &original).unwrap();
        let (session, bloom) = services();
        let tag = read_for_tag(&session, &p);
        let out = tool_write(
            &json!({
                "edits": edits(
                    &p,
                    Some(&tag),
                    json!([{ "op": "replace", "start": 1, "end": 1, "content": "changed" }])
                ),
                "diff": true,
                "cwd": root.to_str().unwrap()
            }),
            &session,
            &bloom,
        )
        .expect("write with diff");
        assert!(
            out.contains("── diff ──"),
            "diff heading must remain visible: {out}"
        );
        assert!(
            !out.contains("1031:line 1031"),
            "diff must not echo the whole file"
        );
        assert!(
            out.len() < 20_000,
            "bounded diff output grew to {} bytes",
            out.len()
        );
    }

    #[test]
    fn omitted_lines_are_not_authorized_by_the_fresh_write_tag() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("hidden.rs");
        let original = (1..=100)
            .map(|i| format!("line {i}"))
            .collect::<Vec<_>>()
            .join("\n");
        std::fs::write(&p, &original).unwrap();
        let (session, bloom) = services();
        let initial_tag = read_for_tag(&session, &p);
        let out = tool_write(
            &json!({
                "edits": edits(
                    &p,
                    Some(&initial_tag),
                    json!([{ "op": "replace", "start": 1, "end": 1, "content": "changed" }])
                ),
                "cwd": root.to_str().unwrap()
            }),
            &session,
            &bloom,
        )
        .expect("first write");
        let fresh_tag = tag_from_output(&p, &out);
        let err = tool_write(
            &json!({
                "edits": edits(
                    &p,
                    Some(&fresh_tag),
                    json!([{ "op": "replace", "start": 50, "end": 50, "content": "hidden" }])
                ),
                "cwd": root.to_str().unwrap()
            }),
            &session,
            &bloom,
        )
        .expect_err("hidden line must require a reread");
        assert!(
            err.contains("never displayed"),
            "expected provenance error: {err}"
        );
        assert_eq!(
            std::fs::read_to_string(&p).unwrap(),
            format!(
                "changed\n{}",
                (2..=100)
                    .map(|i| format!("line {i}"))
                    .collect::<Vec<_>>()
                    .join("\n")
            )
        );
    }

    #[test]
    fn create_tag_does_not_authorize_source_lines() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("created.rs");
        let content = (1..=10)
            .map(|i| format!("line {i}"))
            .collect::<Vec<_>>()
            .join("\n");
        let (session, bloom) = services();
        let created = tool_write(
            &json!({
                "edits": edits(
                    &p,
                    None,
                    json!([{ "op": "create_file", "content": content }])
                ),
                "cwd": root.to_str().unwrap()
            }),
            &session,
            &bloom,
        )
        .expect("create");
        let tag = tag_from_output(&p, &created);
        let err = tool_write(
            &json!({
                "edits": edits(
                    &p,
                    Some(&tag),
                    json!([{ "op": "replace", "start": 2, "end": 2, "content": "hidden" }])
                ),
                "cwd": root.to_str().unwrap()
            }),
            &session,
            &bloom,
        )
        .expect_err("create source body was not displayed");
        assert!(
            err.contains("never displayed"),
            "expected create provenance error: {err}"
        );
        assert_eq!(std::fs::read_to_string(&p).unwrap(), content);
    }
    #[test]
    fn huge_single_line_edit_stays_bounded() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("huge.txt");
        let original = "x".repeat(200_000);
        std::fs::write(&p, &original).unwrap();
        let (session, bloom) = services();
        let tag = session
            .record_snapshot(&p, &original, [1])
            .map(|tag| format!("{tag:04X}"))
            .unwrap();
        let out = tool_write(
            &json!({
                "edits": edits(
                    &p,
                    Some(&tag),
                    json!([{ "op": "replace", "start": 1, "end": 1, "content": "changed" }])
                ),
                "cwd": root.to_str().unwrap()
            }),
            &session,
            &bloom,
        )
        .expect("huge line write");
        assert!(
            out.contains("1:changed"),
            "changed line must remain visible: {out}"
        );
        assert!(
            !out.contains(&original[..1024]),
            "source line leaked into output"
        );
        assert!(
            crate::types::estimate_tokens(out.len() as u64) <= WRITE_RESPONSE_BUDGET,
            "single-line response exceeded budget: {} bytes",
            out.len()
        );
        assert_eq!(
            session.snapshots().head(&p).unwrap().seen_lines,
            HashSet::from([1]),
            "provenance must match the displayed line"
        );
    }

    #[test]
    fn twenty_sections_bound_source_and_diff_and_preserve_each_result() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let (session, bloom) = services();
        let mut sections = Vec::new();
        let mut paths = Vec::new();
        for index in 0..20 {
            let p = root.join(format!("batch-{index}.txt"));
            let original = (1..=50)
                .map(|line| format!("line {line}"))
                .collect::<Vec<_>>()
                .join("\n");
            std::fs::write(&p, &original).unwrap();
            let tag = session
                .record_snapshot(&p, &original, [1])
                .map(|tag| format!("{tag:04X}"))
                .unwrap();
            sections.push(json!({
                "path": p.to_str().unwrap(),
                "tag": tag,
                "ops": [{ "op": "replace", "start": 1, "end": 1, "content": format!("changed {index}") }]
            }));
            paths.push(p);
        }
        let out = tool_write(
            &json!({"edits": sections, "diff": true, "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
        )
        .expect("twenty-section write");
        assert!(
            crate::types::estimate_tokens(out.len() as u64) <= WRITE_RESPONSE_BUDGET,
            "aggregate response exceeded budget: {} bytes",
            out.len()
        );
        for p in paths {
            let marker = format!("## {}\n", p.display());
            let start = out.find(&marker).unwrap();
            let rest = &out[start..];
            let block = rest.split("\n\n---\n\n").next().unwrap();
            assert!(
                block.contains("applied"),
                "missing success for {p:?}: {block}"
            );
            let fresh_tag = format!("{:04X}", session.snapshots().head(&p).unwrap().tag);
            assert!(
                block.contains(&format!("[{}#{}]", p.display(), fresh_tag)),
                "missing fresh tag for {p:?}: {block}"
            );
            assert!(
                block.contains("── diff ──"),
                "missing diff for {p:?}: {block}"
            );
            let displayed: HashSet<u32> = block
                .lines()
                .filter_map(|line| line.split_once(':'))
                .filter_map(|(line, _)| line.parse().ok())
                .collect();
            assert_eq!(
                session.snapshots().head(&p).unwrap().seen_lines,
                displayed,
                "seen lines must equal displayed lines for {p:?}"
            );
        }
    }

    #[test]
    fn mixed_failure_keeps_long_error_and_bounded_success() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let good = root.join("good.txt");
        let original = "one\ntwo\nthree\n";
        std::fs::write(&good, original).unwrap();
        let bad = std::path::PathBuf::from(format!("{}bad.txt", "../".repeat(180)));
        let (session, bloom) = services();
        let tag = session
            .record_snapshot(&good, original, [1])
            .map(|tag| format!("{tag:04X}"))
            .unwrap();
        let out = tool_write(
            &json!({
                "edits": [
                    {"path": good.to_str().unwrap(), "tag": tag, "ops": [{"op": "replace", "start": 1, "end": 1, "content": "ONE"}]},
                    {"path": bad.to_str().unwrap(), "tag": "0000", "ops": [{"op": "replace", "start": 1, "end": 1, "content": "bad"}]}
                ],
                "cwd": root.to_str().unwrap()
            }),
            &session,
            &bloom,
        )
        .expect("mixed calls return successful result");
        assert!(
            out.contains("## "),
            "section statuses must remain visible: {out}"
        );
        assert!(out.contains("applied"), "success status was lost: {out}");
        assert!(
            out.contains(bad.to_string_lossy().as_ref()),
            "long error path was lost"
        );
        assert!(out.contains("escapes"), "failure reason was lost: {out}");
        assert!(
            out.len() < 20_000,
            "successful output was not bounded: {}",
            out.len()
        );
    }

    #[test]
    fn stale_recovery_emits_fresh_tag_and_keeps_hidden_anchors_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("stale.txt");
        let original = (1..=100)
            .map(|line| format!("line {line}"))
            .collect::<Vec<_>>()
            .join("\n");
        std::fs::write(&p, &original).unwrap();
        let (session, bloom) = services();
        let initial_tag = session
            .record_snapshot(&p, &original, [2])
            .map(|tag| format!("{tag:04X}"))
            .unwrap();
        std::fs::write(&p, format!("external\n{original}")).unwrap();
        let out = tool_write(
            &json!({
                "edits": edits(
                    &p,
                    Some(&initial_tag),
                    json!([{ "op": "replace", "start": 2, "end": 2, "content": "updated" }])
                ),
                "cwd": root.to_str().unwrap()
            }),
            &session,
            &bloom,
        )
        .expect("stale edit recovers");
        let fresh_tag = tag_from_output(&p, &out);
        let current = std::fs::read_to_string(&p).unwrap();
        assert_eq!(
            fresh_tag,
            format!("{:04X}", compute_file_hash(&current)),
            "write tag must match recovered full source"
        );
        assert!(
            out.contains("updated"),
            "recovered output lost status/content: {out}"
        );
        let err = tool_write(
            &json!({
                "edits": edits(
                    &p,
                    Some(&fresh_tag),
                    json!([{ "op": "replace", "start": 90, "end": 90, "content": "hidden" }])
                ),
                "cwd": root.to_str().unwrap()
            }),
            &session,
            &bloom,
        )
        .expect_err("hidden stale anchor must require a reread");
        assert!(
            err.contains("never displayed"),
            "missing provenance error: {err}"
        );
        assert_eq!(std::fs::read_to_string(&p).unwrap(), current);
    }
}
