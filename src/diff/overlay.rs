use std::collections::HashMap;
use std::path::Path;
use std::process::Command;

use crate::lang::detect_file_type;
use crate::lang::outline::get_outline_entries;
use crate::types::{FileType, OutlineEntry, OutlineKind};

use super::matching::{build_diff_symbols, match_symbols};
use super::{
    AttributedDiffLine, ChangeType, Conflict, DiffLineKind, DiffSource, FileDiff, FileOverlay,
    FileStatus, Hunk, MatchConfidence, SymbolAttributionKey, SymbolChange, SymbolIdentity,
    UnattributedLine,
};

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/// Build a structural overlay for a single file diff.
///
/// Fetches old/new content based on `source`, outlines both versions,
/// runs three-phase symbol matching, and attributes diff hunks to functions.
pub(crate) fn compute_overlay(
    file_diff: &FileDiff,
    source: &DiffSource,
    checkout: &Path,
) -> FileOverlay {
    let path = &file_diff.path;
    let (insertions, deletions) = raw_insertions_deletions(&file_diff.hunks);

    // Binary or generated files — empty overlay, formatter handles display.
    if file_diff.is_binary || file_diff.is_generated {
        return FileOverlay {
            insertions,
            deletions,
            ..FileOverlay::empty(path.clone())
        };
    }

    let mut overlay = match file_diff.status {
        FileStatus::Modified => compute_modified(file_diff, source, checkout),
        FileStatus::Added => compute_added(file_diff, source, checkout),
        FileStatus::Deleted => compute_deleted(file_diff, source, checkout),
        FileStatus::Renamed => compute_renamed(file_diff, source, checkout),
    };
    overlay.insertions = insertions;
    overlay.deletions = deletions;
    overlay
}

/// Count raw insertions/deletions across all hunks, ignoring context lines.
fn raw_insertions_deletions(hunks: &[Hunk]) -> (usize, usize) {
    let mut insertions = 0usize;
    let mut deletions = 0usize;
    for hunk in hunks {
        for line in &hunk.lines {
            match line.kind {
                DiffLineKind::Added => insertions += 1,
                DiffLineKind::Removed => deletions += 1,
                DiffLineKind::Context => {}
            }
        }
    }
    (insertions, deletions)
}

/// Cross-file move detection: match Deleted symbols in one file with Added
/// symbols in another by (kind, name). Unique pairs become `Moved{old_path}`.
pub(crate) fn cross_file_matching(overlays: &mut [FileOverlay]) {
    // Collect all Deleted and Added symbols with their overlay index + change index.
    let mut deleted: HashMap<(OutlineKind, String), Vec<(usize, usize)>> = HashMap::new();
    let mut added: HashMap<(OutlineKind, String), Vec<(usize, usize)>> = HashMap::new();

    for (oi, overlay) in overlays.iter().enumerate() {
        for (ci, change) in overlay.symbol_changes.iter().enumerate() {
            match &change.change {
                ChangeType::Deleted => {
                    deleted
                        .entry((change.kind, change.name.clone()))
                        .or_default()
                        .push((oi, ci));
                }
                ChangeType::Added => {
                    added
                        .entry((change.kind, change.name.clone()))
                        .or_default()
                        .push((oi, ci));
                }
                _ => {}
            }
        }
    }

    // Collect mutations: (overlay_idx, change_idx, new_change_type, new_confidence)
    let mut mutations: Vec<(usize, usize, ChangeType, MatchConfidence)> = Vec::new();

    for (key, del_locs) in &deleted {
        if let Some(add_locs) = added.get(key) {
            if del_locs.len() == 1 && add_locs.len() == 1 {
                let (del_oi, del_ci) = del_locs[0];
                let (add_oi, add_ci) = add_locs[0];
                // Must be in different files to be a cross-file move.
                if del_oi != add_oi {
                    let old_path = overlays[del_oi].path.clone();
                    mutations.push((
                        add_oi,
                        add_ci,
                        ChangeType::Moved {
                            old_path: old_path.clone(),
                        },
                        MatchConfidence::Exact,
                    ));
                    // Mark the deleted side as Moved too (so formatter can show it).
                    mutations.push((
                        del_oi,
                        del_ci,
                        ChangeType::Moved { old_path },
                        MatchConfidence::Exact,
                    ));
                }
            } else {
                // Ambiguous — multiple candidates.
                let count = (del_locs.len() + add_locs.len()) as u32;
                for &(oi, ci) in add_locs {
                    mutations.push((oi, ci, ChangeType::Added, MatchConfidence::Ambiguous(count)));
                }
            }
        }
    }

    // Apply deferred mutations.
    for (oi, ci, change, confidence) in mutations {
        overlays[oi].symbol_changes[ci].change = change;
        overlays[oi].symbol_changes[ci].match_confidence = confidence;
    }
}

/// Warn when the same symbol name has multiple signature changes across files.
pub(crate) fn signature_warnings(overlays: &[FileOverlay]) -> Vec<String> {
    let mut counts: HashMap<String, u32> = HashMap::new();
    for overlay in overlays {
        for change in &overlay.symbol_changes {
            if matches!(change.change, ChangeType::SignatureChanged) {
                *counts.entry(change.name.clone()).or_default() += 1;
            }
        }
    }

    counts
        .into_iter()
        .filter(|(_, count)| *count > 1)
        .map(|(name, count)| {
            format!("warning: `{name}` signature changed in {count} locations — check callers")
        })
        .collect()
}

/// Scan a file for merge conflict markers and extract conflict blocks.
pub(crate) fn detect_conflicts(path: &Path, checkout: &Path) -> Vec<Conflict> {
    // The overlay path is repo-relative; read it under the requested checkout,
    // never the server's process cwd. `join` keeps an absolute path as-is.
    let Ok(content) = std::fs::read_to_string(checkout.join(path)) else {
        return Vec::new();
    };

    let lines: Vec<&str> = content.lines().collect();
    let mut conflicts = Vec::new();
    let mut i = 0;

    while i < lines.len() {
        if lines[i].starts_with("<<<<<<<") {
            let start = i;
            let mut separator = None;
            let mut end = None;

            // Find ======= and >>>>>>>.
            let mut j = i + 1;
            while j < lines.len() {
                if lines[j].starts_with("=======") {
                    separator = Some(j);
                } else if lines[j].starts_with(">>>>>>>") {
                    end = Some(j);
                    break;
                }
                j += 1;
            }

            if let (Some(sep), Some(e)) = (separator, end) {
                let ours = lines[start + 1..sep].join("\n");
                let theirs = lines[sep + 1..e].join("\n");

                // Find enclosing function via outline.
                let ft = detect_file_type(path);
                let enclosing_fn = if let FileType::Code(lang) = ft {
                    let entries = get_outline_entries(&content, lang);
                    find_enclosing_function(&entries, (start + 1) as u32)
                } else {
                    None
                };

                conflicts.push(Conflict {
                    line: (start + 1) as u32,
                    ours,
                    theirs,
                    enclosing_fn,
                });

                i = e + 1;
                continue;
            }
        }
        i += 1;
    }

    conflicts
}

// ---------------------------------------------------------------------------
// Per-status overlay builders
// ---------------------------------------------------------------------------

fn compute_modified(file_diff: &FileDiff, source: &DiffSource, checkout: &Path) -> FileOverlay {
    let path = &file_diff.path;
    let Ok(old_content) = get_old_content(path, file_diff.old_path.as_deref(), source, checkout)
    else {
        // git error fetching old side — skip symbol analysis to avoid
        // confidently-wrong all-Added overlay.
        return FileOverlay::empty(path.clone());
    };
    let Ok(new_content) = get_new_content(path, source, checkout) else {
        return FileOverlay::empty(path.clone());
    };

    let ft = detect_file_type(path);
    let (symbol_changes, attributed_hunks, unattributed) = if let FileType::Code(lang) = ft {
        let old_entries = get_outline_entries(&old_content, lang);
        let new_entries = get_outline_entries(&new_content, lang);

        if old_entries.is_empty() && new_entries.is_empty() {
            // No grammar support or empty outlines — every line is unattributed.
            let (_, unattributed) = attribute_hunks(&file_diff.hunks, &[]);
            (Vec::new(), Vec::new(), unattributed)
        } else {
            let old_syms = build_diff_symbols(&old_entries, &old_content, lang);
            let new_syms = build_diff_symbols(&new_entries, &new_content, lang);
            let changes = match_symbols(&old_syms, &new_syms);
            let (attributed, unattributed) = attribute_hunks(&file_diff.hunks, &changes);
            (changes, attributed, unattributed)
        }
    } else {
        // Non-code file — no symbol analysis, every line is unattributed.
        let (_, unattributed) = attribute_hunks(&file_diff.hunks, &[]);
        (Vec::new(), Vec::new(), unattributed)
    };

    let (unattributed_hunks, unattributed_omitted) = cap_unattributed(unattributed);

    FileOverlay {
        symbol_changes,
        attributed_hunks,
        unattributed_hunks,
        unattributed_omitted,
        ..FileOverlay::empty(path.clone())
    }
}

fn compute_added(file_diff: &FileDiff, source: &DiffSource, checkout: &Path) -> FileOverlay {
    let path = &file_diff.path;
    let Ok(new_content) = get_new_content(path, source, checkout) else {
        return FileOverlay::empty(path.clone());
    };

    let symbol_changes = entries_to_changes(&new_content, path, &ChangeType::Added);

    let (_, unattributed) = attribute_hunks(&file_diff.hunks, &[]);
    let (unattributed_hunks, unattributed_omitted) = cap_unattributed(unattributed);

    FileOverlay {
        symbol_changes,
        unattributed_hunks,
        unattributed_omitted,
        ..FileOverlay::empty(path.clone())
    }
}

fn compute_deleted(file_diff: &FileDiff, source: &DiffSource, checkout: &Path) -> FileOverlay {
    let path = &file_diff.path;
    let Ok(old_content) = get_old_content(path, file_diff.old_path.as_deref(), source, checkout)
    else {
        return FileOverlay::empty(path.clone());
    };

    let symbol_changes = entries_to_changes(&old_content, path, &ChangeType::Deleted);

    let (_, unattributed) = attribute_hunks(&file_diff.hunks, &[]);
    let (unattributed_hunks, unattributed_omitted) = cap_unattributed(unattributed);

    FileOverlay {
        symbol_changes,
        unattributed_hunks,
        unattributed_omitted,
        ..FileOverlay::empty(path.clone())
    }
}

fn compute_renamed(file_diff: &FileDiff, source: &DiffSource, checkout: &Path) -> FileOverlay {
    let path = &file_diff.path;
    let Ok(old_content) = get_old_content(path, file_diff.old_path.as_deref(), source, checkout)
    else {
        return FileOverlay::empty(path.clone());
    };
    let Ok(new_content) = get_new_content(path, source, checkout) else {
        return FileOverlay::empty(path.clone());
    };

    let ft = detect_file_type(path);
    let (symbol_changes, attributed_hunks, unattributed) = if let FileType::Code(lang) = ft {
        let old_entries = get_outline_entries(&old_content, lang);
        let new_entries = get_outline_entries(&new_content, lang);

        if old_entries.is_empty() && new_entries.is_empty() {
            let (_, unattributed) = attribute_hunks(&file_diff.hunks, &[]);
            (Vec::new(), Vec::new(), unattributed)
        } else {
            let old_syms = build_diff_symbols(&old_entries, &old_content, lang);
            let new_syms = build_diff_symbols(&new_entries, &new_content, lang);
            let changes = match_symbols(&old_syms, &new_syms);
            let (attributed, unattributed) = attribute_hunks(&file_diff.hunks, &changes);
            (changes, attributed, unattributed)
        }
    } else {
        let (_, unattributed) = attribute_hunks(&file_diff.hunks, &[]);
        (Vec::new(), Vec::new(), unattributed)
    };

    let (unattributed_hunks, unattributed_omitted) = cap_unattributed(unattributed);

    FileOverlay {
        symbol_changes,
        attributed_hunks,
        unattributed_hunks,
        unattributed_omitted,
        ..FileOverlay::empty(path.clone())
    }
}

// ---------------------------------------------------------------------------
// Content fetching helpers
// ---------------------------------------------------------------------------

/// Fetch the old-side content for a file diff.
///
/// Returns `Ok(content)` on success (including legitimately empty for new files).
/// Returns `Err(reason)` when the git command OR a filesystem read fails — the caller should
fn get_old_content(
    path: &Path,
    old_path: Option<&Path>,
    source: &DiffSource,
    checkout: &Path,
) -> Result<String, String> {
    let effective_path = old_path.unwrap_or(path);
    let path_str = effective_path.to_string_lossy();

    match source {
        DiffSource::GitUncommitted | DiffSource::GitStaged => {
            git_show(&format!("HEAD:{path_str}"), checkout)
        }
        DiffSource::GitRef(r) => {
            if let Some((left, _)) = r.split_once("..") {
                git_show(&format!("{left}:{path_str}"), checkout)
            } else {
                // `git diff <ref>` compares <ref> (old) against the working tree
                // (new), so the old content is the file at the ref itself.
                git_show(&format!("{r}:{path_str}"), checkout)
            }
        }
        DiffSource::Files(a, _) => {
            std::fs::read_to_string(a).map_err(|e| format!("read {}: {e}", a.display()))
        }
        DiffSource::Patch(_) | DiffSource::Log(_) => Ok(String::new()),
    }
}

/// Where the "new" side of a `GitRef` diff reads its content from.
enum GitRefNewSide {
    /// A range ref (`a..b`): the committed blob at the right side, via `git show`.
    Committed(String),
    /// A bare ref: `git diff <ref>` compares against the working tree on disk.
    WorkingTree,
}

/// Decide how a `GitRef`'s new-side content is sourced.
///
/// A range ref (`a..b`) reads the committed blob at `b` via `git show b:<path>`;
/// a bare ref reads the working tree, because `git diff <ref>` compares `<ref>`
/// against the working tree rather than against `HEAD`.
fn resolve_git_ref_new_side(reff: &str, path_str: &str) -> GitRefNewSide {
    match reff.split_once("..") {
        Some((_, right)) => GitRefNewSide::Committed(format!("{right}:{path_str}")),
        None => GitRefNewSide::WorkingTree,
    }
}

fn get_new_content(path: &Path, source: &DiffSource, checkout: &Path) -> Result<String, String> {
    let path_str = path.to_string_lossy();

    // Working-tree reads use the repo-relative diff path; anchor them under the
    // requested checkout so a bare ref or uncommitted diff reads the caller's
    // repository, not the server's process cwd. `join` keeps absolutes as-is.
    let read_worktree = || {
        let full = checkout.join(path);
        std::fs::read_to_string(&full).map_err(|e| format!("read {}: {e}", full.display()))
    };

    match source {
        DiffSource::GitUncommitted => read_worktree(),
        DiffSource::GitStaged => git_show(&format!(":{path_str}"), checkout),
        DiffSource::GitRef(r) => match resolve_git_ref_new_side(r, &path_str) {
            GitRefNewSide::Committed(spec) => git_show(&spec, checkout),
            GitRefNewSide::WorkingTree => read_worktree(),
        },
        DiffSource::Files(_, b) => {
            std::fs::read_to_string(b).map_err(|e| format!("read {}: {e}", b.display()))
        }
        DiffSource::Patch(_) | DiffSource::Log(_) => Ok(String::new()),
    }
}

fn git_show(spec: &str, checkout: &Path) -> Result<String, String> {
    let output = Command::new("git")
        .current_dir(checkout)
        .args(["-c", "core.quotePath=false", "show", spec])
        .output()
        .map_err(|e| format!("git show failed: {e}"))?;
    if output.status.success() {
        Ok(String::from_utf8_lossy(&output.stdout).into_owned())
    } else {
        let stderr = String::from_utf8_lossy(&output.stderr);
        Err(format!("git show {spec}: {}", stderr.trim()))
    }
}

fn get_entries_for_path(path: &Path, content: &str) -> Vec<OutlineEntry> {
    match detect_file_type(path) {
        FileType::Code(lang) => get_outline_entries(content, lang),
        _ => Vec::new(),
    }
}

// ---------------------------------------------------------------------------
// Hunk-to-function attribution
// ---------------------------------------------------------------------------

struct SymRange {
    key: SymbolAttributionKey,
    old_span: Option<(u32, u32)>,
    new_span: Option<(u32, u32)>,
}

/// Occurrence-keyed symbol lines and the hunk lines no symbol claimed.
type Attribution = (
    Vec<(SymbolAttributionKey, Vec<AttributedDiffLine>)>,
    Vec<Vec<UnattributedLine>>,
);

/// For each symbol change that has a line range, find which diff lines from
/// the hunks fall within each symbol. Returns occurrence-keyed lines plus the
/// hunk lines no symbol claimed, grouped per hunk.
fn attribute_hunks(hunks: &[Hunk], changes: &[SymbolChange]) -> Attribution {
    let mut result: Vec<(SymbolAttributionKey, Vec<AttributedDiffLine>)> = Vec::new();
    let mut unattributed: Vec<Vec<UnattributedLine>> = Vec::new();

    let active_symbols: Vec<&SymbolChange> = changes
        .iter()
        .filter(|c| !matches!(c.change, ChangeType::Unchanged))
        .collect();

    let mut sym_ranges: Vec<SymRange> = Vec::new();
    for change in &active_symbols {
        sym_ranges.push(SymRange {
            key: change.attribution_key(),
            old_span: change.old_span,
            new_span: change.new_span,
        });
    }

    // No active symbols means every hunk line falls through as unattributed.
    let mut buckets: Vec<Vec<AttributedDiffLine>> =
        (0..sym_ranges.len()).map(|_| Vec::new()).collect();

    for hunk in hunks {
        let mut old_line = hunk.old_start;
        let mut new_line = hunk.new_start;
        let mut hunk_unattributed: Vec<UnattributedLine> = Vec::new();

        for diff_line in &hunk.lines {
            match diff_line.kind {
                DiffLineKind::Context => {
                    // Context is attributed by new-file line.
                    let mut claimed = false;
                    for (si, sr) in sym_ranges.iter().enumerate() {
                        if sr
                            .new_span
                            .is_some_and(|(start, end)| new_line >= start && new_line <= end)
                        {
                            buckets[si].push(AttributedDiffLine {
                                kind: diff_line.kind,
                                content: diff_line.content.clone(),
                                old_line: Some(old_line),
                                new_line: Some(new_line),
                            });
                            claimed = true;
                        }
                    }
                    if !claimed {
                        hunk_unattributed.push(UnattributedLine {
                            line: new_line,
                            kind: diff_line.kind,
                            content: diff_line.content.clone(),
                        });
                    }
                    old_line += 1;
                    new_line += 1;
                }
                DiffLineKind::Added => {
                    // Added lines belong to the new-file range.
                    let mut claimed = false;
                    for (si, sr) in sym_ranges.iter().enumerate() {
                        if sr
                            .new_span
                            .is_some_and(|(start, end)| new_line >= start && new_line <= end)
                        {
                            buckets[si].push(AttributedDiffLine {
                                kind: diff_line.kind,
                                content: diff_line.content.clone(),
                                old_line: None,
                                new_line: Some(new_line),
                            });
                            claimed = true;
                        }
                    }
                    if !claimed {
                        hunk_unattributed.push(UnattributedLine {
                            line: new_line,
                            kind: diff_line.kind,
                            content: diff_line.content.clone(),
                        });
                    }
                    new_line += 1;
                }
                DiffLineKind::Removed => {
                    // Removed lines belong to the old-file range, including
                    // removed adornments on matched symbols.
                    let mut claimed = false;
                    for (si, sr) in sym_ranges.iter().enumerate() {
                        if sr
                            .old_span
                            .is_some_and(|(start, end)| old_line >= start && old_line <= end)
                        {
                            buckets[si].push(AttributedDiffLine {
                                kind: diff_line.kind,
                                content: diff_line.content.clone(),
                                old_line: Some(old_line),
                                new_line: None,
                            });
                            claimed = true;
                        }
                    }
                    if !claimed {
                        hunk_unattributed.push(UnattributedLine {
                            line: old_line,
                            kind: diff_line.kind,
                            content: diff_line.content.clone(),
                        });
                    }
                    old_line += 1;
                }
            }
        }

        // Context that spills past a symbol's range is not a change. Keep a
        // leftover group only when it holds an added or removed line.
        if hunk_unattributed
            .iter()
            .any(|l| l.kind != DiffLineKind::Context)
        {
            unattributed.push(hunk_unattributed);
        }
    }

    // Collect non-empty buckets.
    for (si, lines) in buckets.into_iter().enumerate() {
        if !lines.is_empty() {
            result.push((sym_ranges[si].key.clone(), lines));
        }
    }

    (result, unattributed)
}

/// Cap the total unattributed-line count across all hunks at
/// `MAX_UNATTRIBUTED_LINES`, dropping lines from later hunks first.
fn cap_unattributed(hunks: Vec<Vec<UnattributedLine>>) -> (Vec<Vec<UnattributedLine>>, usize) {
    let mut kept: Vec<Vec<UnattributedLine>> = Vec::new();
    let mut total = 0usize;
    let mut omitted = 0usize;

    for hunk in hunks {
        if total >= super::MAX_UNATTRIBUTED_LINES {
            omitted += hunk.len();
            continue;
        }
        let remaining = super::MAX_UNATTRIBUTED_LINES - total;
        if hunk.len() <= remaining {
            total += hunk.len();
            kept.push(hunk);
        } else {
            let (keep, drop) = hunk.split_at(remaining);
            omitted += drop.len();
            total += keep.len();
            if !keep.is_empty() {
                kept.push(keep.to_vec());
            }
        }
    }

    (kept, omitted)
}

// ---------------------------------------------------------------------------
// Outline → SymbolChange helpers
// ---------------------------------------------------------------------------

/// Convert outline entries to symbol changes of a single type (Added or Deleted).
fn entries_to_changes(content: &str, path: &Path, change_type: &ChangeType) -> Vec<SymbolChange> {
    let entries = get_entries_for_path(path, content);
    let mut changes = Vec::new();
    let mut occurrences: HashMap<(OutlineKind, String, String), u32> = HashMap::new();
    collect_entries_recursive(&entries, change_type, "", &mut occurrences, &mut changes);
    changes
}

fn collect_entries_recursive(
    entries: &[OutlineEntry],
    change_type: &ChangeType,
    parent_path: &str,
    occurrences: &mut HashMap<(OutlineKind, String, String), u32>,
    out: &mut Vec<SymbolChange>,
) {
    for entry in entries {
        // Skip imports/exports — not interesting for symbol-level diff.
        if matches!(entry.kind, OutlineKind::Import | OutlineKind::Export) {
            continue;
        }

        let (old_sig, new_sig) = match change_type {
            ChangeType::Added => (None, entry.signature.clone()),
            ChangeType::Deleted => (entry.signature.clone(), None),
            _ => (None, None),
        };

        let occurrence_key = (entry.kind, parent_path.to_string(), entry.name.clone());
        let occurrence = occurrences.entry(occurrence_key).or_default();
        out.push(SymbolChange {
            identity: SymbolIdentity {
                kind: entry.kind,
                parent_path: parent_path.to_string(),
                name: entry.name.clone(),
                occurrence: *occurrence,
            },
            name: entry.name.clone(),
            kind: entry.kind,
            change: change_type.clone(),
            match_confidence: MatchConfidence::Exact,
            line: entry.start_line,
            span_start_line: entry.span_start_line,
            old_span: match change_type {
                ChangeType::Added => None,
                _ => Some((entry.span_start_line, entry.end_line)),
            },
            new_span: match change_type {
                ChangeType::Deleted => None,
                _ => Some((entry.span_start_line, entry.end_line)),
            },
            old_sig,
            new_sig,
            size_delta: Some((
                entry.end_line.saturating_sub(entry.span_start_line) + 1,
                entry.end_line.saturating_sub(entry.span_start_line) + 1,
            )),
        });
        *occurrence += 1;

        if !entry.children.is_empty() {
            let child_parent = if parent_path.is_empty() {
                entry.name.clone()
            } else {
                format!("{parent_path}::{}", entry.name)
            };
            collect_entries_recursive(
                &entry.children,
                change_type,
                &child_parent,
                occurrences,
                out,
            );
        }
    }
}

// ---------------------------------------------------------------------------
// Conflict helpers
// ---------------------------------------------------------------------------

/// Find the enclosing function for a given line number by walking the outline.
fn find_enclosing_function(entries: &[OutlineEntry], line: u32) -> Option<String> {
    for entry in entries {
        if line >= entry.span_start_line && line <= entry.end_line {
            // Check children first for more specific match.
            if let Some(child_name) = find_enclosing_function(&entry.children, line) {
                return Some(child_name);
            }
            if matches!(entry.kind, OutlineKind::Function) {
                return Some(entry.name.clone());
            }
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::diff::DiffLine;

    fn identity(name: &str) -> SymbolIdentity {
        SymbolIdentity {
            kind: OutlineKind::Function,
            parent_path: String::new(),
            name: name.into(),
            occurrence: 0,
        }
    }

    #[test]
    fn bare_git_ref_new_content_reads_working_tree() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("sample.rs");
        std::fs::write(&file, "fn worktree_only() {}\n").unwrap();

        // A single ref (no `..`) diffs against the working tree, so the new
        // content must come from the file on disk — not `git show HEAD:<path>`,
        // which would return empty here and silently mis-attribute the diff.
        let content = get_new_content(&file, &DiffSource::GitRef("HEAD".to_string()), dir.path())
            .expect("working-tree read must succeed");
        assert!(
            content.contains("worktree_only"),
            "bare GitRef new content must be the working tree, got {content:?}"
        );

        // Lock the routing decision too: a bare ref must take the working-tree branch.
        match resolve_git_ref_new_side("HEAD", "src/lib.rs") {
            GitRefNewSide::WorkingTree => {}
            GitRefNewSide::Committed(spec) => {
                panic!("bare ref must read the working tree, not `git show {spec}`")
            }
        }
    }

    #[test]
    fn range_git_ref_new_content_reads_committed_blob() {
        // Dual-path lock: a RANGE ref (`a..b`) must read the committed blob at the
        // right side via `git show b:<path>`, NOT the working tree. Without this a
        // regression collapsing both branches into the working-tree read would pass
        // the bare-ref test above while silently breaking range diffs.
        match resolve_git_ref_new_side("HEAD..feature", "src/lib.rs") {
            GitRefNewSide::Committed(spec) => {
                assert_eq!(spec, "feature:src/lib.rs", "wrong git show spec");
            }
            GitRefNewSide::WorkingTree => {
                panic!("range ref must read the committed blob, not the working tree")
            }
        }
    }

    #[test]
    fn git_show_error_skips_symbol_analysis() {
        // When get_old_content returns Err, compute_modified must return an
        // empty overlay rather than a confidently-wrong all-Added result.
        // Simulate with Files source pointing at a missing old-side file.
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("foo.rs");
        std::fs::write(&file, "fn new_fn() {}\n").unwrap();

        let missing = dir.path().join("does_not_exist.rs");
        let result = get_old_content(
            &file,
            None,
            &DiffSource::Files(missing.clone(), file.clone()),
            dir.path(),
        );
        assert!(result.is_err(), "missing file path must yield Err, got Ok");

        // Confirm that compute_modified skips symbol analysis when old side is unavailable.
        let file_diff = FileDiff {
            path: file.clone(),
            old_path: None,
            status: FileStatus::Modified,
            hunks: Vec::new(),
            is_generated: false,
            is_binary: false,
        };
        let overlay = compute_modified(
            &file_diff,
            &DiffSource::Files(missing.clone(), file.clone()),
            dir.path(),
        );
        assert!(
            overlay.symbol_changes.is_empty(),
            "overlay must be empty when old side is unavailable"
        );
    }

    #[test]
    fn added_and_deleted_same_line_duplicates_keep_distinct_buckets() {
        let source = "fn run() {} fn run() {}\n";
        let path = Path::new("dupes.rs");

        for change_type in [ChangeType::Added, ChangeType::Deleted] {
            let changes = entries_to_changes(source, path, &change_type);
            let runs: Vec<_> = changes
                .iter()
                .filter(|change| change.name == "run")
                .collect();

            assert_eq!(runs.len(), 2, "both declarations must produce changes");
            assert_eq!(runs[0].identity.occurrence, 0);
            assert_eq!(runs[1].identity.occurrence, 1);
            assert_ne!(
                runs[0].identity, runs[1].identity,
                "same-name declarations need distinct attribution buckets"
            );
        }
    }

    #[test]
    fn diff_attribution_includes_leading_rust_attributes() {
        let source = "#[inline]\nfn alpha() {\n    let value = 1;\n}\n";
        let entries = get_entries_for_path(Path::new("alpha.rs"), source);
        let entry = entries
            .iter()
            .find(|entry| entry.name == "alpha")
            .expect("alpha should be outlined");

        let changes = vec![SymbolChange {
            identity: identity(&entry.name),
            name: entry.name.clone(),
            kind: entry.kind,
            change: ChangeType::BodyChanged,
            match_confidence: MatchConfidence::Exact,
            line: entry.start_line,
            span_start_line: entry.span_start_line,
            old_span: Some((entry.span_start_line, entry.end_line)),
            new_span: Some((entry.span_start_line, entry.end_line)),
            old_sig: None,
            new_sig: None,
            size_delta: Some((entry.end_line - entry.span_start_line + 1, 3)),
        }];
        let hunks = vec![super::super::Hunk {
            old_start: 1,
            old_count: 0,
            new_start: 1,
            new_count: 1,
            lines: vec![DiffLine {
                kind: DiffLineKind::Added,
                content: "#[inline]".to_string(),
            }],
        }];

        let (attributed, unattributed) = attribute_hunks(&hunks, &changes);
        assert_eq!(attributed.len(), 1);
        assert_eq!(attributed[0].0.identity.name, "alpha");
        assert_eq!(attributed[0].1[0].content, "#[inline]");
        assert!(unattributed.is_empty());
        assert_eq!(
            find_enclosing_function(&entries, 1),
            Some("alpha".to_string())
        );
    }
    #[test]
    fn diff_attribution_uses_old_and_new_ranges_for_attribute_replacement() {
        let changes = vec![SymbolChange {
            identity: identity("alpha"),
            name: "alpha".to_string(),
            kind: OutlineKind::Function,
            change: ChangeType::BodyChanged,
            match_confidence: MatchConfidence::Exact,
            line: 2,
            span_start_line: 2,
            old_span: Some((1, 4)),
            new_span: Some((1, 4)),
            old_sig: None,
            new_sig: None,
            size_delta: Some((4, 4)),
        }];
        let hunks = vec![super::super::Hunk {
            old_start: 1,
            old_count: 1,
            new_start: 1,
            new_count: 1,
            lines: vec![
                DiffLine {
                    kind: DiffLineKind::Removed,
                    content: "#[inline]".to_string(),
                },
                DiffLine {
                    kind: DiffLineKind::Added,
                    content: "#[cold]".to_string(),
                },
            ],
        }];

        let (attributed, unattributed) = attribute_hunks(&hunks, &changes);
        assert_eq!(attributed.len(), 1);
        assert_eq!(attributed[0].1.len(), 2);
        assert_eq!(attributed[0].1[0].kind, DiffLineKind::Removed);
        assert_eq!(attributed[0].1[1].kind, DiffLineKind::Added);
        assert_eq!(attributed[0].1[0].old_line, Some(1));
        assert_eq!(attributed[0].1[0].new_line, None);
        assert_eq!(attributed[0].1[1].old_line, None);
        assert_eq!(attributed[0].1[1].new_line, Some(1));
        assert!(unattributed.is_empty());
    }
    #[test]
    fn compute_modified_non_code_file_preserves_unattributed_hunk() {
        let dir = tempfile::tempdir().unwrap();
        let old_file = dir.path().join("old.md");
        let new_file = dir.path().join("notes.md");
        std::fs::write(&old_file, "one\ntwo\nthree\n").unwrap();
        std::fs::write(&new_file, "one\nCHANGED\nthree\n").unwrap();

        let file_diff = FileDiff {
            path: new_file.clone(),
            old_path: None,
            status: FileStatus::Modified,
            hunks: vec![Hunk {
                old_start: 2,
                old_count: 1,
                new_start: 2,
                new_count: 1,
                lines: vec![
                    DiffLine {
                        kind: DiffLineKind::Removed,
                        content: "two".to_string(),
                    },
                    DiffLine {
                        kind: DiffLineKind::Added,
                        content: "CHANGED".to_string(),
                    },
                ],
            }],
            is_generated: false,
            is_binary: false,
        };

        let overlay = compute_modified(
            &file_diff,
            &DiffSource::Files(old_file, new_file),
            dir.path(),
        );

        assert!(overlay.symbol_changes.is_empty(), "markdown has no symbols");
        assert_eq!(
            overlay.unattributed_hunks.len(),
            1,
            "expected one unattributed hunk, got {:?}",
            overlay.unattributed_hunks
        );
        let lines: Vec<&str> = overlay.unattributed_hunks[0]
            .iter()
            .map(|l| l.content.as_str())
            .collect();
        assert_eq!(lines, vec!["two", "CHANGED"]);
    }
    // An added import is an outline symbol and is attributed to it. A
    // top-level comment belongs to no symbol, so it must stay visible.
    #[test]
    fn compute_modified_code_line_outside_every_symbol_stays_unattributed() {
        let dir = tempfile::tempdir().unwrap();
        let old_file = dir.path().join("old.rs");
        let new_file = dir.path().join("lib.rs");
        std::fs::write(&old_file, "fn foo() {}\n").unwrap();
        std::fs::write(&new_file, "// top-level note\nfn foo() {}\n").unwrap();

        let file_diff = FileDiff {
            path: new_file.clone(),
            old_path: None,
            status: FileStatus::Modified,
            hunks: vec![Hunk {
                old_start: 1,
                old_count: 1,
                new_start: 1,
                new_count: 2,
                lines: vec![
                    DiffLine {
                        kind: DiffLineKind::Added,
                        content: "// top-level note".to_string(),
                    },
                    DiffLine {
                        kind: DiffLineKind::Context,
                        content: "fn foo() {}".to_string(),
                    },
                ],
            }],
            is_generated: false,
            is_binary: false,
        };

        let overlay = compute_modified(
            &file_diff,
            &DiffSource::Files(old_file, new_file),
            dir.path(),
        );

        assert_eq!(
            overlay.unattributed_hunks.len(),
            1,
            "expected the comment line to be unattributed, got {:?}",
            overlay.unattributed_hunks
        );
        let added: Vec<&str> = overlay.unattributed_hunks[0]
            .iter()
            .filter(|l| l.kind == DiffLineKind::Added)
            .map(|l| l.content.as_str())
            .collect();
        assert_eq!(added, vec!["// top-level note"]);
    }
    #[test]
    fn context_outside_a_changed_symbol_is_not_an_unattributed_change() {
        let change = SymbolChange {
            identity: identity("foo"),
            name: "foo".to_string(),
            kind: OutlineKind::Function,
            change: ChangeType::BodyChanged,
            match_confidence: MatchConfidence::Exact,
            line: 2,
            span_start_line: 2,
            old_span: Some((2, 2)),
            new_span: Some((2, 2)),
            old_sig: None,
            new_sig: None,
            size_delta: Some((1, 1)),
        };
        let line = |kind, content: &str| DiffLine {
            kind,
            content: content.to_string(),
        };
        let hunk = Hunk {
            old_start: 1,
            old_count: 3,
            new_start: 1,
            new_count: 3,
            lines: vec![
                line(DiffLineKind::Context, "// before"),
                line(DiffLineKind::Removed, "fn foo() { 1 }"),
                line(DiffLineKind::Added, "fn foo() { 2 }"),
                line(DiffLineKind::Context, "// after"),
            ],
        };
        // The removed line belongs to the surviving symbol at the removal
        // position, so only context is left over and no group is kept.
        let (attributed, unattributed) = attribute_hunks(&[hunk], &[change]);
        assert_eq!(attributed.len(), 1);
        assert_eq!(attributed[0].1.len(), 2, "removed and added line");
        assert!(unattributed.is_empty(), "{unattributed:?}");

        let context_only = Hunk {
            old_start: 10,
            old_count: 1,
            new_start: 10,
            new_count: 1,
            lines: vec![line(DiffLineKind::Context, "// far away")],
        };
        let (_, unattributed) = attribute_hunks(&[context_only], &[]);
        assert!(unattributed.is_empty(), "context alone is not a change");
    }
    #[test]
    fn cap_unattributed_lines_caps_at_max_and_reports_omitted() {
        let lines: Vec<DiffLine> = (0..250)
            .map(|i| DiffLine {
                kind: DiffLineKind::Added,
                content: format!("line {i}"),
            })
            .collect();
        let hunk = Hunk {
            old_start: 1,
            old_count: 0,
            new_start: 1,
            new_count: 250,
            lines,
        };

        let (_, unattributed) = attribute_hunks(&[hunk], &[]);
        let (kept, omitted) = cap_unattributed(unattributed);

        let total_kept: usize = kept.iter().map(Vec::len).sum();
        assert_eq!(total_kept, crate::diff::MAX_UNATTRIBUTED_LINES);
        assert_eq!(omitted, 50);
    }
}
