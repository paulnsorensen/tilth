pub mod format;
pub mod matching;
pub mod overlay;
pub mod parse;

use std::collections::HashSet;
use std::fmt::Write as _;
use std::path::{Path, PathBuf};
use std::process::Command;

use rayon::prelude::*;

use crate::types::OutlineKind;

#[derive(Debug)]
pub enum DiffSource {
    GitUncommitted,
    GitStaged,
    GitRef(String),
    Files(PathBuf, PathBuf),
    Patch(PathBuf),
    Log(String),
}

#[derive(Debug)]
pub struct FileDiff {
    pub path: PathBuf,
    pub old_path: Option<PathBuf>,
    pub status: FileStatus,
    pub hunks: Vec<Hunk>,
    pub is_generated: bool,
    pub is_binary: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FileStatus {
    Added,
    Modified,
    Deleted,
    Renamed,
}

#[derive(Debug)]
pub struct Hunk {
    pub old_start: u32,
    pub old_count: u32,
    pub new_start: u32,
    pub new_count: u32,
    pub lines: Vec<DiffLine>,
}

#[derive(Debug)]
pub struct DiffLine {
    pub kind: DiffLineKind,
    pub content: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DiffLineKind {
    Context,
    Added,
    Removed,
}

#[derive(Debug)]
pub struct DiffSymbol {
    pub entry: crate::types::OutlineEntry,
    pub identity: SymbolIdentity,
    pub content_hash: u64,
    pub structural_hash: u64,
    pub source_text: String,
}

#[derive(Debug, Clone, Hash, Eq, PartialEq)]
pub struct SymbolIdentity {
    pub kind: OutlineKind,
    pub parent_path: String,
    pub name: String,
}

#[derive(Debug)]
pub struct SymbolChange {
    pub name: String,
    pub kind: OutlineKind,
    pub change: ChangeType,
    pub match_confidence: MatchConfidence,
    pub line: u32,
    pub old_sig: Option<String>,
    pub new_sig: Option<String>,
    pub size_delta: Option<(u32, u32)>,
}

#[derive(Debug, Clone)]
pub enum ChangeType {
    Added,
    Deleted,
    BodyChanged,
    SignatureChanged,
    Renamed { old_name: String },
    Moved { old_path: PathBuf },
    Unchanged,
}

#[derive(Debug, Clone)]
pub enum MatchConfidence {
    Exact,
    Structural,
    Fuzzy(f32),
    Ambiguous(u32),
}

#[derive(Debug)]
pub struct FileOverlay {
    pub path: PathBuf,
    pub symbol_changes: Vec<SymbolChange>,
    pub attributed_hunks: Vec<(String, Vec<DiffLine>)>,
}

#[derive(Debug)]
pub struct Conflict {
    pub line: u32,
    pub ours: String,
    pub theirs: String,
    pub enclosing_fn: Option<String>,
}

#[derive(Debug)]
pub struct CommitSummary {
    pub hash: String,
    pub timestamp: i64,
    pub message: String,
    pub author: String,
    pub overlays: Vec<FileOverlay>,
}

/// Resolve the diff source from CLI/MCP parameters.
///
/// Priority: patch > log > a+b > source > default (uncommitted).
/// Returns an error if only one of `a` or `b` is provided.
pub fn resolve_source(
    source: Option<&str>,
    a: Option<&str>,
    b: Option<&str>,
    patch: Option<&str>,
    log: Option<&str>,
) -> Result<DiffSource, String> {
    if let Some(p) = patch {
        return Ok(DiffSource::Patch(PathBuf::from(p)));
    }
    if let Some(l) = log {
        return Ok(DiffSource::Log(l.to_string()));
    }
    match (a, b) {
        (Some(fa), Some(fb)) => return Ok(DiffSource::Files(PathBuf::from(fa), PathBuf::from(fb))),
        (Some(_), None) | (None, Some(_)) => {
            return Err("both --a and --b must be provided together".to_string());
        }
        (None, None) => {}
    }
    if let Some(s) = source {
        let ds = match s {
            "staged" => DiffSource::GitStaged,
            "uncommitted" | "working" => DiffSource::GitUncommitted,
            r => DiffSource::GitRef(r.to_string()),
        };
        return Ok(ds);
    }
    Ok(DiffSource::GitUncommitted)
}

/// Reject a git ref/range whose first character is `-`, guarding against
/// argument injection (e.g. `--output=/path` overwriting an arbitrary file
/// via `git diff`/`git log`).
fn reject_leading_dash(s: &str) -> Result<(), String> {
    if s.starts_with('-') {
        Err(format!(
            "diff ref/range may not begin with '-' (arg-injection guard): {s}"
        ))
    } else {
        Ok(())
    }
}

/// Execute a git diff command and return raw unified diff output.
fn run_git_diff(source: &DiffSource) -> Result<String, String> {
    use std::process::Command;

    match source {
        DiffSource::Log(_) => {
            return Err("log mode should not call run_git_diff directly".to_string());
        }
        DiffSource::Patch(path) => {
            let content = std::fs::read_to_string(path)
                .map_err(|e| format!("failed to read patch file: {e}"))?;
            return Ok(content);
        }
        _ => {}
    }

    let mut cmd = Command::new("git");
    cmd.args(["-c", "core.quotePath=false"]);
    cmd.arg("diff");

    match source {
        DiffSource::GitUncommitted => {
            // working tree vs HEAD (unstaged + staged)
            cmd.arg("HEAD");
        }
        DiffSource::GitStaged => {
            cmd.arg("--staged");
        }
        DiffSource::GitRef(r) => {
            reject_leading_dash(r)?;
            cmd.arg(r);
        }
        DiffSource::Files(fa, fb) => {
            cmd.arg("--no-index").arg("--").arg(fa).arg(fb);
            if fa.is_absolute() && fb.is_absolute() {
                // Absolute paths do not need the inherited cwd, which may have been deleted.
                // Relative paths keep the inherited cwd so CLI resolution remains unchanged.
                if let Some(root) = fa.ancestors().last() {
                    cmd.current_dir(root);
                }
            }
        }
        // Patch and Log are handled above
        DiffSource::Patch(_) | DiffSource::Log(_) => unreachable!(),
    }

    let output = cmd
        .output()
        .map_err(|e| format!("failed to run git diff: {e}"))?;

    // git diff exits 0 (no diff) or 1 (diff found) on success; --no-index uses
    // the same convention. Anything else (typically 128/129) is a fatal error,
    // for every source — not just refs.
    if !matches!(output.status.code(), Some(0 | 1)) {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        let msg = match source {
            DiffSource::GitRef(r) => {
                append_default_branch_hint(format!("git diff failed for '{r}': {stderr}"), &stderr)
            }
            DiffSource::GitUncommitted => format!("git diff against HEAD failed: {stderr}"),
            DiffSource::GitStaged => format!("git diff --staged failed: {stderr}"),
            DiffSource::Files(fa, fb) => format!(
                "git diff --no-index failed for '{}' vs '{}': {stderr}",
                fa.display(),
                fb.display()
            ),
            DiffSource::Patch(_) | DiffSource::Log(_) => unreachable!(),
        };
        return Err(msg);
    }

    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}

fn parse_origin_head_ref(s: &str) -> Option<String> {
    s.strip_prefix("refs/remotes/origin/")
        .filter(|n| !n.is_empty())
        .map(str::to_string)
}

/// Detect the repo's default branch: prefer `origin/HEAD`'s symbolic-ref
/// target, falling back to a local `main`/`master` branch if there is no
/// configured remote. Returns `None` rather than guessing at an arbitrary
/// branch.
fn default_branch_hint() -> Option<String> {
    if let Ok(output) = Command::new("git")
        .args(["symbolic-ref", "refs/remotes/origin/HEAD"])
        .output()
    {
        if output.status.success() {
            let s = String::from_utf8_lossy(&output.stdout).trim().to_string();
            if let Some(name) = parse_origin_head_ref(&s) {
                return Some(name);
            }
        }
    }

    let output = Command::new("git")
        .args(["branch", "--format=%(refname:short)"])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .map(str::trim)
        .find(|b| *b == "main" || *b == "master")
        .map(str::to_string)
}

/// Append a "this repo's default branch is ..." hint to an error message when
/// `stderr` indicates an unresolvable revision and the default branch can be
/// detected; otherwise return `msg` unchanged.
fn append_default_branch_hint(mut msg: String, stderr: &str) -> String {
    if !(stderr.contains("unknown revision") || stderr.contains("ambiguous argument")) {
        return msg;
    }
    if let Some(branch) = default_branch_hint() {
        let _ = write!(
            msg,
            "; this repo's default branch is '{branch}' — try '{branch}..HEAD'"
        );
    }
    msg
}

/// Normalize an absolute scope under the repo root to repo-relative, strip a
/// leading `./`, and trim a trailing slash. A scope equal to the repo root
/// normalizes to the empty string. Non-absolute scopes, and scopes outside
/// the repo root, are returned trimmed but otherwise unchanged.
fn normalize_scope(scope: &str) -> String {
    let trimmed = scope.trim_end_matches('/');
    let trimmed = trimmed.strip_prefix("./").unwrap_or(trimmed);
    let path = Path::new(trimmed);
    if path.is_absolute() {
        if let Some(root) = repo_root() {
            if let Ok(rel) = path.strip_prefix(&root) {
                return rel.to_string_lossy().into_owned();
            }
        }
    }
    trimmed.to_string()
}

/// The repo root via `git rev-parse --show-toplevel`, run against the current
/// process cwd.
fn repo_root() -> Option<PathBuf> {
    let output = Command::new("git")
        .args(["rev-parse", "--show-toplevel"])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let s = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if s.is_empty() {
        None
    } else {
        Some(PathBuf::from(s))
    }
}

/// True when `path` sits under `scope` at a component boundary — scope
/// `src/fanout` matches `src/fanout/a.rs` but not `src/fanout_extra/a.rs`.
/// `scope` must be non-empty; an empty scope is the caller's signal to fall
/// through to the unscoped full overview, not to filter here.
fn path_has_scope_prefix(path: &Path, scope: &str) -> bool {
    path.starts_with(Path::new(scope))
}

/// Find the overlay whose path exactly matches or ends with `query`.
fn find_overlay_by_path<'a>(overlays: &'a [FileOverlay], query: &str) -> Option<&'a FileOverlay> {
    overlays.iter().find(|o| {
        let p = o.path.to_string_lossy();
        p == query || p.ends_with(query)
    })
}

const MAX_EXTENSION_MATCHES: usize = 5;
const MAX_TOP_DIRS: usize = 8;

/// Extracts the extension from an extension-shaped scope (`.tf` or `*.tf`).
/// Returns `None` for anything that also looks like a path (contains `/`), or
/// when an overlay's first path component equals `scope` — a dot-directory
/// like `.github` that genuinely has changes is a directory, not a bare
/// extension, so this falls through to the fuzzy path instead.
fn extension_shaped<'a>(scope: &'a str, overlays: &[FileOverlay]) -> Option<&'a str> {
    let ext = if let Some(rest) = scope.strip_prefix("*.") {
        (!rest.is_empty() && !rest.contains('/')).then_some(rest)
    } else {
        scope
            .strip_prefix('.')
            .filter(|rest| !rest.is_empty() && !rest.contains('/'))
    }?;
    let has_matching_dir = overlays.iter().any(|o| {
        o.path
            .components()
            .next()
            .is_some_and(|c| c.as_os_str() == scope)
    });
    (!has_matching_dir).then_some(ext)
}

/// True when `scope` names a directory: either a trailing slash, or (when
/// the repo root is resolvable) an existing directory on disk under it.
fn directory_shaped(scope: &str) -> bool {
    if scope.ends_with('/') {
        return true;
    }
    repo_root().is_some_and(|root| root.join(scope).is_dir())
}

/// Build the "extension, not a path" error, listing changed files with that
/// extension (capped at `MAX_EXTENSION_MATCHES`, then `+N more`).
fn extension_not_found_error(scope: &str, ext: &str, overlays: &[FileOverlay]) -> String {
    let mut matches: Vec<String> = overlays
        .iter()
        .filter(|o| o.path.extension().and_then(|e| e.to_str()) == Some(ext))
        .map(|o| o.path.to_string_lossy().into_owned())
        .collect();
    matches.sort();
    let prefix = format!(
        "'{scope}' is an extension, not a path; tilth_diff scopes are a file, file:function, or directory."
    );
    if matches.is_empty() {
        format!("{prefix} no changed files have that extension")
    } else {
        let shown = matches.len().min(MAX_EXTENSION_MATCHES);
        let mut list = matches[..shown].join(", ");
        if matches.len() > shown {
            let _ = write!(list, ", +{} more", matches.len() - shown);
        }
        format!("{prefix} Changed files with that extension: {list}")
    }
}

/// Build the "no changed files under this directory" error, listing the
/// top-level directories that do have changes (capped at `MAX_TOP_DIRS`).
fn directory_not_found_error(scope: &str, overlays: &[FileOverlay]) -> String {
    let trimmed = scope.trim_end_matches('/');
    let mut top_dirs: Vec<String> = overlays
        .iter()
        .filter_map(|o| {
            let mut components = o.path.components();
            let first = components.next()?;
            components.next()?;
            Some(first.as_os_str().to_string_lossy().into_owned())
        })
        .collect();
    top_dirs.sort();
    top_dirs.dedup();
    top_dirs.truncate(MAX_TOP_DIRS);
    format!(
        "no changed files under '{trimmed}/'; changed top-level dirs: {}",
        top_dirs.join(", ")
    )
}

/// Build a "not found" error for `missing`, classifying extension- and
/// directory-shaped misses before falling back to the fuzzy "did you mean"
/// clause (up to 3 similar overlay paths) for file-shaped misses.
fn not_found_error(missing: &str, overlays: &[FileOverlay]) -> String {
    if directory_shaped(missing) {
        return directory_not_found_error(missing, overlays);
    }
    if let Some(ext) = extension_shaped(missing, overlays) {
        return extension_not_found_error(missing, ext, overlays);
    }
    let candidates: Vec<String> = overlays
        .iter()
        .map(|o| o.path.to_string_lossy().into_owned())
        .collect();
    let suggestions = crate::read::fuzzy_path::rank_path_suggestions(missing, &candidates);
    if suggestions.is_empty() {
        format!("file '{missing}' not found in diff")
    } else {
        format!(
            "file '{missing}' not found in diff; did you mean: {}",
            suggestions.join(", ")
        )
    }
}

/// Extract the backtick-quoted symbol name from a `signature_warnings`/
/// `compute_blast` warning string (`` warning: `name` ... `` / `` blast: `name` ... ``).
fn warning_symbol_name(warning: &str) -> Option<&str> {
    let rest = warning.split_once('`')?.1;
    rest.split_once('`').map(|(name, _)| name)
}

/// True when `scope`'s first `:` is the `file:function` separator rather
/// than a Windows drive-letter colon (`C:/repo/src`, index 1).
fn is_file_function_scope(scope: &str) -> bool {
    let rest = if scope.as_bytes().get(1) == Some(&b':') {
        &scope[2..]
    } else {
        scope
    };
    rest.contains(':')
}

/// Build `file_meta` parallel to `overlays`: `(path, is_generated, is_binary)`.
fn build_file_meta<'a>(
    overlays: &'a [FileOverlay],
    file_diffs: &[FileDiff],
) -> Vec<(&'a Path, bool, bool)> {
    overlays
        .iter()
        .map(|o| {
            let fd = file_diffs.iter().find(|fd| fd.path == o.path);
            let (is_generated, is_binary) =
                fd.map_or((false, false), |f| (f.is_generated, f.is_binary));
            (o.path.as_path(), is_generated, is_binary)
        })
        .collect()
}

/// Full diff orchestrator — parse → overlay → format pipeline.
pub fn diff(
    source: &DiffSource,
    scope: Option<&str>,
    search: Option<&str>,
    blast: bool,
    _expand: usize,
    budget: Option<u64>,
) -> Result<String, String> {
    // Log mode has its own pipeline.
    if let DiffSource::Log(range) = source {
        return diff_log(range, scope, budget);
    }

    let raw = run_git_diff(source)?;
    if raw.is_empty() {
        return Ok("No changes.".to_string());
    }

    // 1. Parse raw unified diff.
    let file_diffs = parse::parse_unified_diff(&raw);
    if file_diffs.is_empty() {
        return Ok("No changes.".to_string());
    }

    // 2. Build structural overlays in parallel — each FileDiff is independent
    // and `compute_overlay` constructs its own tree-sitter parser per call
    // (see `lang::outline::get_outline_entries`), so no shared mutable state
    // crosses worker boundaries.
    let mut overlays: Vec<FileOverlay> = file_diffs
        .par_iter()
        .map(|fd| overlay::compute_overlay(fd, source))
        .collect();

    // 3. Cross-file move detection.
    overlay::cross_file_matching(&mut overlays);

    // 4. Signature warnings.
    let mut warnings = overlay::signature_warnings(&overlays);

    // 5. Search filter.
    if let Some(term) = search {
        filter_by_search(&mut overlays, term);
        if overlays.is_empty() {
            return Ok(format!("No changes matching '{term}'."));
        }
    }

    // 6. Blast radius.
    if blast {
        let mut blast_warnings = compute_blast(&overlays);
        warnings.append(&mut blast_warnings);
    }

    // 7. Format based on scope.
    let label = source_label(source);
    let mut output = match scope {
        None => {
            let file_meta = build_file_meta(&overlays, &file_diffs);
            format::format_overview(&overlays, &file_meta, &warnings, &label, budget)
        }
        Some(raw_scope) => {
            let scope_norm = normalize_scope(raw_scope);
            if scope_norm.is_empty() {
                let file_meta = build_file_meta(&overlays, &file_diffs);
                format::format_overview(&overlays, &file_meta, &warnings, &label, budget)
            } else if is_file_function_scope(&scope_norm) {
                // file:function scope
                let (file_part, fn_name) = scope_norm.split_once(':').unwrap();
                match find_overlay_by_path(&overlays, file_part) {
                    Some(o) => format::format_function_detail(o, fn_name),
                    None => return Err(not_found_error(file_part, &overlays)),
                }
            } else if let Some(o) = find_overlay_by_path(&overlays, &scope_norm) {
                format::format_file_detail(o, budget)
            } else {
                if !overlays
                    .iter()
                    .any(|o| path_has_scope_prefix(&o.path, &scope_norm))
                {
                    return Err(not_found_error(raw_scope, &overlays));
                }
                overlays.retain(|o| path_has_scope_prefix(&o.path, &scope_norm));
                let retained_names: HashSet<&str> = overlays
                    .iter()
                    .flat_map(|o| o.symbol_changes.iter())
                    .filter(|c| matches!(c.change, ChangeType::SignatureChanged))
                    .map(|c| c.name.as_str())
                    .collect();
                warnings
                    .retain(|w| warning_symbol_name(w).is_none_or(|n| retained_names.contains(n)));
                let file_meta = build_file_meta(&overlays, &file_diffs);
                let overview =
                    format::format_overview(&overlays, &file_meta, &warnings, &label, budget);
                format!("Scope filter: files under '{scope_norm}/'\n\n{overview}")
            }
        }
    };

    // 8. Conflict detection for uncommitted diffs.
    if matches!(source, DiffSource::GitUncommitted) {
        let mut all_conflicts = Vec::new();
        for overlay in &overlays {
            let conflicts = overlay::detect_conflicts(&overlay.path);
            if !conflicts.is_empty() {
                all_conflicts.push((&overlay.path, conflicts));
            }
        }
        if !all_conflicts.is_empty() {
            for (path, conflicts) in &all_conflicts {
                output.push('\n');
                output.push_str(&format::format_conflicts(conflicts, path));
            }
            if let Some(b) = budget {
                output = crate::budget::apply(&output, b);
            }
        }
    }

    Ok(output)
}

// ---------------------------------------------------------------------------
// Helper functions
// ---------------------------------------------------------------------------

/// Human-readable label for a diff source.
fn source_label(source: &DiffSource) -> String {
    match source {
        DiffSource::GitUncommitted => "uncommitted".to_string(),
        DiffSource::GitStaged => "staged".to_string(),
        DiffSource::GitRef(r) => r.clone(),
        DiffSource::Files(a, b) => format!("{} vs {}", a.display(), b.display()),
        DiffSource::Patch(p) => format!("patch: {}", p.display()),
        DiffSource::Log(r) => format!("log: {r}"),
    }
}

/// Filter overlays to only symbols whose diff lines contain the search term
/// (case-insensitive substring match). Removes files with no matches.
fn filter_by_search(overlays: &mut Vec<FileOverlay>, term: &str) {
    let lower_term = term.to_lowercase();

    overlays.retain_mut(|overlay| {
        // Keep symbol changes that have matching diff lines.
        let matching_symbols: HashSet<String> = overlay
            .attributed_hunks
            .iter()
            .filter(|(_, lines)| {
                lines
                    .iter()
                    .any(|l| l.content.to_lowercase().contains(&lower_term))
            })
            .map(|(name, _)| name.clone())
            .collect();

        // Also match on symbol names themselves.
        let matching_names: HashSet<String> = overlay
            .symbol_changes
            .iter()
            .filter(|c| c.name.to_lowercase().contains(&lower_term))
            .map(|c| c.name.clone())
            .collect();

        let all_matching: HashSet<String> =
            matching_symbols.union(&matching_names).cloned().collect();

        if all_matching.is_empty() {
            return false;
        }

        overlay
            .symbol_changes
            .retain(|c| all_matching.contains(&c.name));
        overlay
            .attributed_hunks
            .retain(|(name, _)| all_matching.contains(name));

        true
    });
}

/// Find callers of signature-changed symbols and return warnings.
fn compute_blast(overlays: &[FileOverlay]) -> Vec<String> {
    let sig_changed: HashSet<String> = overlays
        .iter()
        .flat_map(|o| o.symbol_changes.iter())
        .filter(|c| matches!(c.change, ChangeType::SignatureChanged))
        .map(|c| c.name.clone())
        .collect();

    if sig_changed.is_empty() {
        return Vec::new();
    }

    let scope = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    let bloom = crate::index::bloom::BloomFilterCache::new();

    match crate::search::callers::find_callers_batch(
        &sig_changed,
        &scope,
        &bloom,
        None,
        crate::search::callers::BATCH_EARLY_QUIT,
    ) {
        Ok((matches, _)) => {
            let mut counts: std::collections::HashMap<String, usize> =
                std::collections::HashMap::new();
            for (target, _) in &matches {
                *counts.entry(target.clone()).or_default() += 1;
            }
            counts
                .into_iter()
                .map(|(name, count)| {
                    format!(
                        "blast: `{name}` signature changed — {count} caller{} may need updating",
                        if count == 1 { "" } else { "s" }
                    )
                })
                .collect()
        }
        Err(_) => Vec::new(),
    }
}

/// Git's well-known empty-tree object hash — the "old" side of a root
/// commit's diff, which has no parent to diff against.
const EMPTY_TREE_SHA1: &str = "4b825dc642cb6eb9a060e54bf8d69288fbee4904";

/// Log mode pipeline: run per-commit diffs and format as commit summaries.
fn diff_log(range: &str, scope: Option<&str>, budget: Option<u64>) -> Result<String, String> {
    reject_leading_dash(range)?;
    let scope_norm = scope.map(normalize_scope);

    // Get commit list, including parent hashes to detect root commits.
    let output = Command::new("git")
        .args(["log", "--format=%H %at %P%x01%s%x00%an", range])
        .output()
        .map_err(|e| format!("failed to run git log: {e}"))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        return Err(append_default_branch_hint(
            format!("git log failed: {stderr}"),
            &stderr,
        ));
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    let mut summaries: Vec<CommitSummary> = Vec::new();

    for line in stdout.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }

        // Format: "<hash> <timestamp> <parents>\x01<subject>\0<author>"
        let Some((rest, author)) = line.split_once('\0') else {
            continue;
        };
        let Some((meta, message)) = rest.split_once('\x01') else {
            continue;
        };

        let mut parts = meta.splitn(3, ' ');
        let Some(hash) = parts.next() else {
            continue;
        };
        let timestamp: i64 = parts.next().and_then(|s| s.parse().ok()).unwrap_or(0);
        let has_parent = !parts.next().unwrap_or("").trim().is_empty();

        // Run diff for this commit. A root commit has no parent to diff
        // against, so diff it against the empty tree instead.
        let ref_str = if has_parent {
            format!("{hash}^..{hash}")
        } else {
            format!("{EMPTY_TREE_SHA1}..{hash}")
        };
        let commit_source = DiffSource::GitRef(ref_str);
        let raw = run_git_diff(&commit_source)?;
        let file_diffs = parse::parse_unified_diff(&raw);

        let mut overlays: Vec<FileOverlay> = file_diffs
            .iter()
            .map(|fd| overlay::compute_overlay(fd, &commit_source))
            .collect();
        overlay::cross_file_matching(&mut overlays);

        summaries.push(CommitSummary {
            hash: hash.to_string(),
            timestamp,
            message: message.to_string(),
            author: author.to_string(),
            overlays,
        });
    }

    // Filter by scope if set.
    if let Some(file_scope) = scope_norm.as_deref() {
        for summary in &mut summaries {
            summary.overlays.retain(|o| {
                let p = o.path.to_string_lossy();
                p == file_scope || p.ends_with(file_scope)
            });
        }
        summaries.retain(|s| !s.overlays.is_empty());
    }

    if summaries.is_empty() {
        return Ok("No commits found.".to_string());
    }

    Ok(format::format_log(&summaries, range, budget))
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::path::Path;
    use std::sync::Mutex;

    /// Mutex to serialize tests that change process cwd.
    static CWD_LOCK: Mutex<()> = Mutex::new(());

    /// Create a test git repo with an initial commit containing a Rust file.
    fn setup_test_repo() -> tempfile::TempDir {
        let dir = tempfile::tempdir().expect("failed to create tempdir");
        let p = dir.path();

        git(p, &["init"]);
        git(p, &["config", "user.email", "test@test.com"]);
        git(p, &["config", "user.name", "Test"]);

        let src = p.join("src");
        fs::create_dir_all(&src).unwrap();

        let main_rs = src.join("main.rs");
        fs::write(
            &main_rs,
            "fn hello() {\n    println!(\"hello\");\n}\n\nfn goodbye() {\n    println!(\"bye\");\n}\n\nfn main() {\n    hello();\n    goodbye();\n}\n",
        )
        .unwrap();

        git(p, &["add", "-A"]);
        git(p, &["commit", "-m", "initial"]);

        dir
    }

    /// Run a git command in the given directory.
    /// Replace the relative commit age in a log header (`(0s ago, @Test)`)
    /// with a placeholder. Two runs can straddle a second boundary, so tests
    /// that compare log output must compare structure, not wall-clock timing.
    fn mask_commit_age(out: &str) -> String {
        out.lines()
            .map(|line| {
                let Some(ago) = line.find(" ago, @") else {
                    return line.to_string();
                };
                let Some(open) = line[..ago].rfind(" (") else {
                    return line.to_string();
                };
                format!("{}AGE{}", &line[..open + 2], &line[ago..])
            })
            .collect::<Vec<_>>()
            .join("\n")
    }

    fn git(dir: &Path, args: &[&str]) -> String {
        let output = Command::new("git")
            .args(args)
            .current_dir(dir)
            .env("GIT_AUTHOR_NAME", "Test")
            .env("GIT_AUTHOR_EMAIL", "test@test.com")
            .env("GIT_COMMITTER_NAME", "Test")
            .env("GIT_COMMITTER_EMAIL", "test@test.com")
            .output()
            .expect("failed to run git");
        String::from_utf8_lossy(&output.stdout).into_owned()
    }

    /// Run `diff()` from within the test repo directory, serialized via `CWD_LOCK`.
    fn run_diff_in(
        dir: &Path,
        source: &DiffSource,
        scope: Option<&str>,
        search: Option<&str>,
        blast: bool,
        budget: Option<u64>,
    ) -> Result<String, String> {
        let _lock = CWD_LOCK.lock().unwrap();
        let prev = std::env::current_dir().unwrap();
        std::env::set_current_dir(dir).unwrap();
        let result = diff(source, scope, search, blast, 0, budget);
        std::env::set_current_dir(&prev).unwrap();
        result
    }

    // 1. test_empty_diff
    #[test]
    fn test_empty_diff() {
        let dir = setup_test_repo();
        let result = run_diff_in(
            dir.path(),
            &DiffSource::GitUncommitted,
            None,
            None,
            false,
            None,
        )
        .unwrap();
        assert_eq!(result, "No changes.");
    }

    // 2. test_overview_modified
    #[test]
    fn test_overview_modified() {
        let dir = setup_test_repo();
        let main_rs = dir.path().join("src/main.rs");
        let content = fs::read_to_string(&main_rs).unwrap();
        fs::write(
            &main_rs,
            content.replace("println!(\"hello\")", "println!(\"hi there\")"),
        )
        .unwrap();

        let result = run_diff_in(
            dir.path(),
            &DiffSource::GitUncommitted,
            None,
            None,
            false,
            None,
        )
        .unwrap();
        assert!(result.contains("[~]"), "expected [~] marker in:\n{result}");
    }

    // 3. test_overview_added
    #[test]
    fn test_overview_added() {
        let dir = setup_test_repo();
        let main_rs = dir.path().join("src/main.rs");
        let mut content = fs::read_to_string(&main_rs).unwrap();
        content.push_str("\nfn new_function() {\n    println!(\"new\");\n}\n");
        fs::write(&main_rs, content).unwrap();

        let result = run_diff_in(
            dir.path(),
            &DiffSource::GitUncommitted,
            None,
            None,
            false,
            None,
        )
        .unwrap();
        assert!(result.contains("[+]"), "expected [+] marker in:\n{result}");
    }

    // 4. test_overview_deleted
    #[test]
    fn test_overview_deleted() {
        let dir = setup_test_repo();
        let main_rs = dir.path().join("src/main.rs");
        // Remove the goodbye function entirely.
        fs::write(
            &main_rs,
            "fn hello() {\n    println!(\"hello\");\n}\n\nfn main() {\n    hello();\n}\n",
        )
        .unwrap();

        let result = run_diff_in(
            dir.path(),
            &DiffSource::GitUncommitted,
            None,
            None,
            false,
            None,
        )
        .unwrap();
        assert!(result.contains("[-]"), "expected [-] marker in:\n{result}");
    }

    // 5. test_overview_signature_changed
    #[test]
    fn test_overview_signature_changed() {
        let dir = setup_test_repo();
        let main_rs = dir.path().join("src/main.rs");
        let content = fs::read_to_string(&main_rs).unwrap();
        // Change hello() to hello(name: &str)
        let new_content = content
            .replace("fn hello() {", "fn hello(name: &str) {")
            .replace("println!(\"hello\")", "println!(\"hello {}\", name)")
            .replace("hello();", "hello(\"world\");");
        fs::write(&main_rs, new_content).unwrap();

        let result = run_diff_in(
            dir.path(),
            &DiffSource::GitUncommitted,
            None,
            None,
            false,
            None,
        )
        .unwrap();
        assert!(
            result.contains("[~:sig]"),
            "expected [~:sig] marker in:\n{result}"
        );
    }

    // 6. test_file_detail_scope
    #[test]
    fn test_file_detail_scope() {
        let dir = setup_test_repo();
        let main_rs = dir.path().join("src/main.rs");
        let content = fs::read_to_string(&main_rs).unwrap();
        fs::write(
            &main_rs,
            content.replace("println!(\"hello\")", "println!(\"hi\")"),
        )
        .unwrap();

        let result = run_diff_in(
            dir.path(),
            &DiffSource::GitUncommitted,
            Some("src/main.rs"),
            None,
            false,
            None,
        )
        .unwrap();
        assert!(
            result.contains("# Diff: src/main.rs"),
            "expected file detail header in:\n{result}"
        );
        assert!(
            result.contains("symbols touched"),
            "expected symbols touched in:\n{result}"
        );
    }

    // 7. test_function_detail_scope
    #[test]
    fn test_function_detail_scope() {
        let dir = setup_test_repo();
        let main_rs = dir.path().join("src/main.rs");
        let content = fs::read_to_string(&main_rs).unwrap();
        fs::write(
            &main_rs,
            content.replace("println!(\"hello\")", "println!(\"hi\")"),
        )
        .unwrap();

        let result = run_diff_in(
            dir.path(),
            &DiffSource::GitUncommitted,
            Some("src/main.rs:hello"),
            None,
            false,
            None,
        )
        .unwrap();
        assert!(
            result.contains("hello"),
            "expected hello function in:\n{result}"
        );
    }

    // 8. test_staged_diff
    #[test]
    fn test_staged_diff() {
        let dir = setup_test_repo();
        let main_rs = dir.path().join("src/main.rs");
        let content = fs::read_to_string(&main_rs).unwrap();
        fs::write(
            &main_rs,
            content.replace("println!(\"hello\")", "println!(\"staged\")"),
        )
        .unwrap();
        git(dir.path(), &["add", "src/main.rs"]);

        let result =
            run_diff_in(dir.path(), &DiffSource::GitStaged, None, None, false, None).unwrap();
        assert!(
            result.contains("main.rs") || result.contains("[~]"),
            "expected staged changes in:\n{result}"
        );
    }

    // 9. test_ref_diff
    #[test]
    fn test_ref_diff() {
        let dir = setup_test_repo();
        let main_rs = dir.path().join("src/main.rs");
        let content = fs::read_to_string(&main_rs).unwrap();
        fs::write(
            &main_rs,
            content.replace("println!(\"hello\")", "println!(\"ref\")"),
        )
        .unwrap();
        git(dir.path(), &["add", "-A"]);
        git(dir.path(), &["commit", "-m", "change hello"]);

        let result = run_diff_in(
            dir.path(),
            &DiffSource::GitRef("HEAD~1..HEAD".to_string()),
            None,
            None,
            false,
            None,
        )
        .unwrap();
        assert!(
            result.contains("main.rs"),
            "expected main.rs in ref diff:\n{result}"
        );
    }

    // 10. test_generated_file
    #[test]
    fn test_generated_file() {
        let dir = setup_test_repo();
        let lock = dir.path().join("package-lock.json");
        fs::write(&lock, "{}").unwrap();
        git(dir.path(), &["add", "-A"]);
        git(dir.path(), &["commit", "-m", "add lock"]);

        fs::write(&lock, "{ \"version\": 2 }").unwrap();

        let result = run_diff_in(
            dir.path(),
            &DiffSource::GitUncommitted,
            None,
            None,
            false,
            None,
        )
        .unwrap();
        assert!(
            result.contains("generated"),
            "expected 'generated' in:\n{result}"
        );
    }

    // 11. test_multiple_files
    #[test]
    fn test_multiple_files() {
        let dir = setup_test_repo();
        let main_rs = dir.path().join("src/main.rs");
        let content = fs::read_to_string(&main_rs).unwrap();
        fs::write(
            &main_rs,
            content.replace("println!(\"hello\")", "println!(\"hi\")"),
        )
        .unwrap();

        let lib_rs = dir.path().join("src/lib.rs");
        fs::write(&lib_rs, "pub fn lib_fn() {\n    42\n}\n").unwrap();
        git(dir.path(), &["add", "src/lib.rs"]);
        git(dir.path(), &["commit", "-m", "add lib"]);
        fs::write(&lib_rs, "pub fn lib_fn() {\n    99\n}\n").unwrap();

        let result = run_diff_in(
            dir.path(),
            &DiffSource::GitUncommitted,
            None,
            None,
            false,
            None,
        )
        .unwrap();
        assert!(result.contains("main.rs"), "expected main.rs in:\n{result}");
        assert!(result.contains("lib.rs"), "expected lib.rs in:\n{result}");
        assert!(
            result.contains("2 files"),
            "expected '2 files' in:\n{result}"
        );
    }

    // 12. test_search_filter
    #[test]
    fn test_search_filter() {
        let dir = setup_test_repo();
        let main_rs = dir.path().join("src/main.rs");
        let content = fs::read_to_string(&main_rs).unwrap();
        // Modify both functions.
        let new_content = content
            .replace("println!(\"hello\")", "println!(\"UNIQUE_MARKER\")")
            .replace("println!(\"bye\")", "println!(\"other change\")");
        fs::write(&main_rs, new_content).unwrap();

        let result = run_diff_in(
            dir.path(),
            &DiffSource::GitUncommitted,
            None,
            Some("UNIQUE_MARKER"),
            false,
            None,
        )
        .unwrap();
        assert!(
            result.contains("hello"),
            "expected hello (matching) in:\n{result}"
        );
    }

    // 13. test_search_no_matches
    #[test]
    fn test_search_no_matches() {
        let dir = setup_test_repo();
        let main_rs = dir.path().join("src/main.rs");
        let content = fs::read_to_string(&main_rs).unwrap();
        fs::write(
            &main_rs,
            content.replace("println!(\"hello\")", "println!(\"hi\")"),
        )
        .unwrap();

        let result = run_diff_in(
            dir.path(),
            &DiffSource::GitUncommitted,
            None,
            Some("NONEXISTENT_TERM_XYZ"),
            false,
            None,
        )
        .unwrap();
        assert!(
            result.contains("No changes matching"),
            "expected no-match message in:\n{result}"
        );
    }

    // 14. test_file_scope_not_found
    #[test]
    fn test_file_scope_not_found() {
        let dir = setup_test_repo();
        let main_rs = dir.path().join("src/main.rs");
        let content = fs::read_to_string(&main_rs).unwrap();
        fs::write(
            &main_rs,
            content.replace("println!(\"hello\")", "println!(\"hi\")"),
        )
        .unwrap();

        let result = run_diff_in(
            dir.path(),
            &DiffSource::GitUncommitted,
            Some("nonexistent.rs"),
            None,
            false,
            None,
        );
        assert!(result.is_err(), "expected error for missing file scope");
        assert!(
            result.unwrap_err().contains("not found"),
            "expected 'not found' in error"
        );
    }

    // 15. test_patch_file
    #[test]
    fn test_patch_file() {
        let dir = setup_test_repo();
        let patch = dir.path().join("test.patch");
        let patch_content = "\
diff --git a/src/main.rs b/src/main.rs
--- a/src/main.rs
+++ b/src/main.rs
@@ -1,3 +1,3 @@
 fn hello() {
-    println!(\"hello\");
+    println!(\"patched\");
 }
";
        fs::write(&patch, patch_content).unwrap();

        let result = run_diff_in(
            dir.path(),
            &DiffSource::Patch(patch.clone()),
            None,
            None,
            false,
            None,
        )
        .unwrap();
        assert!(
            result.contains("main.rs"),
            "expected main.rs in patch result:\n{result}"
        );
    }

    // 16. test_file_to_file
    #[test]
    fn test_file_to_file() {
        let dir = setup_test_repo();
        let file_a = dir.path().join("a.txt");
        let file_b = dir.path().join("b.txt");
        fs::write(&file_a, "line one\nline two\n").unwrap();
        fs::write(&file_b, "line one\nline three\n").unwrap();

        let result = run_diff_in(
            dir.path(),
            &DiffSource::Files(file_a, file_b),
            None,
            None,
            false,
            None,
        )
        .unwrap();
        // The diff should contain something — the files differ.
        assert!(
            !result.contains("No changes"),
            "expected changes between files:\n{result}"
        );
    }

    #[cfg(unix)]
    #[test]
    fn file_diff_cwd_regression() {
        const MODE: &str = "TILTH_FILE_DIFF_TEST_MODE";
        const ROOT: &str = "TILTH_FILE_DIFF_TEST_ROOT";
        if let Ok(mode) = std::env::var(MODE) {
            let root = PathBuf::from(std::env::var_os(ROOT).unwrap());
            let mut first = root.join("first.txt");
            let mut second = root.join("second.txt");
            match mode.as_str() {
                "deleted" => fs::remove_dir(std::env::current_dir().unwrap()).unwrap(),
                "missing" => first = root.join("missing/first.txt"),
                "unusable" => first = root.join("not-a-directory/first.txt"),
                "relative" => {
                    first = PathBuf::from("first.txt");
                    second = PathBuf::from("second.txt");
                }
                "mixed" => first = PathBuf::from("first.txt"),
                _ => panic!("unknown child mode: {mode}"),
            }
            let result = run_git_diff(&DiffSource::Files(first, second));
            if matches!(mode.as_str(), "missing" | "unusable") {
                // Git uses exit 1 for inaccessible operands; preserve the existing empty result.
                assert_eq!(result.unwrap(), "");
            } else {
                let diff = result.unwrap();
                assert!(diff.contains("-one\n+two\n"), "{diff}");
            }
            return;
        }

        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().canonicalize().unwrap();
        fs::write(root.join("first.txt"), "one\n").unwrap();
        fs::write(root.join("second.txt"), "two\n").unwrap();
        fs::write(root.join("not-a-directory"), "file\n").unwrap();
        let deleted = root.join("deleted-cwd");
        fs::create_dir(&deleted).unwrap();
        for mode in ["deleted", "missing", "unusable", "relative", "mixed"] {
            let output = Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "diff::tests::file_diff_cwd_regression",
                    "--nocapture",
                    "--test-threads=1",
                ])
                .envs([(MODE, mode), (ROOT, root.to_str().unwrap())])
                .current_dir(if mode == "deleted" { &deleted } else { &root })
                .output()
                .unwrap();
            let stdout = String::from_utf8_lossy(&output.stdout);
            let stderr = String::from_utf8_lossy(&output.stderr);
            assert!(output.status.success(), "{mode}: {stdout}\n{stderr}");
            assert!(stdout.contains("1 passed; 0 failed;"), "{mode}: {stdout}");
        }
    }

    // 17. test_log_mode
    #[test]
    fn test_log_mode() {
        let dir = setup_test_repo();
        let main_rs = dir.path().join("src/main.rs");

        // Make a second commit.
        let content = fs::read_to_string(&main_rs).unwrap();
        fs::write(
            &main_rs,
            content.replace("println!(\"hello\")", "println!(\"log test\")"),
        )
        .unwrap();
        git(dir.path(), &["add", "-A"]);
        git(dir.path(), &["commit", "-m", "second commit"]);

        let result = run_diff_in(
            dir.path(),
            &DiffSource::Log("HEAD~1..HEAD".to_string()),
            None,
            None,
            false,
            None,
        )
        .unwrap();
        assert!(
            result.contains("# Log:"),
            "expected log header in:\n{result}"
        );
        assert!(
            result.contains("second commit"),
            "expected commit message in:\n{result}"
        );
    }

    // 18. test_resolve_source_variants
    #[test]
    fn test_resolve_source_variants() {
        // Default → uncommitted.
        assert!(matches!(
            resolve_source(None, None, None, None, None).unwrap(),
            DiffSource::GitUncommitted
        ));

        // Staged.
        assert!(matches!(
            resolve_source(Some("staged"), None, None, None, None).unwrap(),
            DiffSource::GitStaged
        ));

        // Working.
        assert!(matches!(
            resolve_source(Some("working"), None, None, None, None).unwrap(),
            DiffSource::GitUncommitted
        ));

        // Ref.
        match resolve_source(Some("HEAD~3..HEAD"), None, None, None, None).unwrap() {
            DiffSource::GitRef(r) => assert_eq!(r, "HEAD~3..HEAD"),
            other => panic!("expected GitRef, got {other:?}"),
        }

        // Files.
        match resolve_source(None, Some("a.rs"), Some("b.rs"), None, None).unwrap() {
            DiffSource::Files(a, b) => {
                assert_eq!(a, PathBuf::from("a.rs"));
                assert_eq!(b, PathBuf::from("b.rs"));
            }
            other => panic!("expected Files, got {other:?}"),
        }

        // Error: only one of a/b.
        assert!(resolve_source(None, Some("a.rs"), None, None, None).is_err());

        // Patch.
        match resolve_source(None, None, None, Some("test.patch"), None).unwrap() {
            DiffSource::Patch(p) => assert_eq!(p, PathBuf::from("test.patch")),
            other => panic!("expected Patch, got {other:?}"),
        }

        // Log.
        match resolve_source(None, None, None, None, Some("HEAD~5..HEAD")).unwrap() {
            DiffSource::Log(r) => assert_eq!(r, "HEAD~5..HEAD"),
            other => panic!("expected Log, got {other:?}"),
        }

        // Patch takes priority over source.
        assert!(matches!(
            resolve_source(Some("staged"), None, None, Some("x.patch"), None).unwrap(),
            DiffSource::Patch(_)
        ));
    }

    // 19. test_git_ref_rejects_leading_dash
    #[test]
    fn test_git_ref_rejects_leading_dash() {
        let dir = setup_test_repo();
        let result = run_diff_in(
            dir.path(),
            &DiffSource::GitRef("--output=/tmp/pwned".to_string()),
            None,
            None,
            false,
            None,
        );
        assert!(result.is_err(), "expected leading-dash ref to be rejected");
        assert!(
            result.unwrap_err().contains("arg-injection guard"),
            "expected arg-injection guard message"
        );
    }

    // 20. test_log_rejects_leading_dash
    #[test]
    fn test_log_rejects_leading_dash() {
        let dir = setup_test_repo();
        let result = run_diff_in(
            dir.path(),
            &DiffSource::Log("--output=/tmp/pwned".to_string()),
            None,
            None,
            false,
            None,
        );
        assert!(
            result.is_err(),
            "expected leading-dash range to be rejected"
        );
        assert!(
            result.unwrap_err().contains("arg-injection guard"),
            "expected arg-injection guard message"
        );
    }

    // 21. test_dir_prefix_scope_filters_overview
    #[test]
    fn test_dir_prefix_scope_filters_overview() {
        let dir = setup_test_repo();
        let fanout_dir = dir.path().join("src/fanout");
        fs::create_dir_all(&fanout_dir).unwrap();
        let a_rs = fanout_dir.join("a.rs");
        fs::write(&a_rs, "fn a() {\n    1\n}\n").unwrap();
        let other_rs = dir.path().join("src/other.rs");
        fs::write(&other_rs, "fn b() {\n    2\n}\n").unwrap();
        git(dir.path(), &["add", "-A"]);
        git(dir.path(), &["commit", "-m", "add fanout+other"]);

        fs::write(&a_rs, "fn a() {\n    99\n}\n").unwrap();
        fs::write(&other_rs, "fn b() {\n    99\n}\n").unwrap();

        let result = run_diff_in(
            dir.path(),
            &DiffSource::GitUncommitted,
            Some("src/fanout"),
            None,
            false,
            None,
        )
        .unwrap();
        assert!(
            result.contains("fanout/a.rs"),
            "expected fanout file in filtered overview:\n{result}"
        );
        assert!(
            !result.contains("other.rs"),
            "expected other.rs excluded from filtered overview:\n{result}"
        );
        assert!(
            result.contains("Scope filter"),
            "expected scope filter header:\n{result}"
        );
    }

    // 22. test_dir_prefix_boundary_no_match
    #[test]
    fn test_dir_prefix_boundary_no_match() {
        let dir = setup_test_repo();
        let fanout_dir = dir.path().join("src/fanout");
        fs::create_dir_all(&fanout_dir).unwrap();
        let a_rs = fanout_dir.join("a.rs");
        fs::write(&a_rs, "fn a() {\n    1\n}\n").unwrap();
        git(dir.path(), &["add", "-A"]);
        git(dir.path(), &["commit", "-m", "add fanout"]);

        fs::write(&a_rs, "fn a() {\n    99\n}\n").unwrap();

        let result = run_diff_in(
            dir.path(),
            &DiffSource::GitUncommitted,
            Some("src/fan"),
            None,
            false,
            None,
        );
        assert!(
            result.is_err(),
            "expected 'src/fan' to NOT component-boundary-match 'src/fanout/a.rs'"
        );
        assert!(
            result.unwrap_err().contains("not found"),
            "expected not-found error for non-matching prefix"
        );
    }

    // 23. test_scope_not_found_suggests_similar
    #[test]
    fn test_scope_not_found_suggests_similar() {
        let dir = setup_test_repo();
        let main_rs = dir.path().join("src/main.rs");
        let content = fs::read_to_string(&main_rs).unwrap();
        fs::write(
            &main_rs,
            content.replace("println!(\"hello\")", "println!(\"hi\")"),
        )
        .unwrap();

        let result = run_diff_in(
            dir.path(),
            &DiffSource::GitUncommitted,
            Some("src/man.rs"),
            None,
            false,
            None,
        );
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert!(
            err.contains("did you mean"),
            "expected did-you-mean suggestion in:\n{err}"
        );
        assert!(
            err.contains("main.rs"),
            "expected main.rs suggested in:\n{err}"
        );
    }

    // 24. test_scope_not_found_no_suggestion_for_garbage
    #[test]
    fn test_scope_not_found_no_suggestion_for_garbage() {
        let dir = setup_test_repo();
        let main_rs = dir.path().join("src/main.rs");
        let content = fs::read_to_string(&main_rs).unwrap();
        fs::write(
            &main_rs,
            content.replace("println!(\"hello\")", "println!(\"hi\")"),
        )
        .unwrap();

        let result = run_diff_in(
            dir.path(),
            &DiffSource::GitUncommitted,
            Some("zzqqxx/nonexistent_totally_unrelated.xyz"),
            None,
            false,
            None,
        );
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert!(
            !err.contains("did you mean"),
            "expected no suggestion for unrelated garbage path:\n{err}"
        );
    }

    fn overlay_at(path: &str) -> FileOverlay {
        FileOverlay {
            path: PathBuf::from(path),
            symbol_changes: Vec::new(),
            attributed_hunks: Vec::new(),
        }
    }

    // 25b. test_not_found_error_extension_shaped_lists_matches
    #[test]
    fn test_not_found_error_extension_shaped_lists_matches() {
        let overlays = vec![
            overlay_at("infra/a.tf"),
            overlay_at("infra/b.tf"),
            overlay_at("src/main.rs"),
        ];
        let err = not_found_error(".tf", &overlays);
        assert!(
            err.contains("'.tf' is an extension, not a path"),
            "expected extension classification in:\n{err}"
        );
        assert!(
            err.contains("tilth_diff scopes are a file, file:function, or directory"),
            "expected scope-shape teaching in:\n{err}"
        );
        assert!(
            err.contains("Changed files with that extension: infra/a.tf, infra/b.tf"),
            "expected matching .tf files listed in:\n{err}"
        );
    }

    // 25c. test_not_found_error_extension_shaped_no_matches
    #[test]
    fn test_not_found_error_extension_shaped_no_matches() {
        let overlays = vec![overlay_at("src/main.rs")];
        let err = not_found_error("*.tf", &overlays);
        assert!(
            err.contains("'*.tf' is an extension, not a path"),
            "expected extension classification in:\n{err}"
        );
        assert!(
            err.contains("no changed files have that extension"),
            "expected no-matches clause in:\n{err}"
        );
    }

    // 25d. test_not_found_error_extension_shaped_caps_list
    #[test]
    fn test_not_found_error_extension_shaped_caps_list() {
        let overlays = vec![
            overlay_at("a.tf"),
            overlay_at("b.tf"),
            overlay_at("c.tf"),
            overlay_at("d.tf"),
            overlay_at("e.tf"),
            overlay_at("f.tf"),
            overlay_at("g.tf"),
        ];
        let err = not_found_error(".tf", &overlays);
        assert!(
            err.contains("a.tf, b.tf, c.tf, d.tf, e.tf, +2 more"),
            "expected capped list with '+2 more' in:\n{err}"
        );
    }

    // 25d2. test_not_found_error_dot_directory_trailing_slash_no_changes
    #[test]
    fn test_not_found_error_dot_directory_trailing_slash_no_changes() {
        let overlays = vec![overlay_at("src/main.rs")];
        let err = not_found_error(".github/", &overlays);
        assert!(
            err.contains("no changed files under '.github/'"),
            "expected directory classification for trailing-slash dot-dir in:\n{err}"
        );
        assert!(
            !err.contains("is an extension, not a path"),
            "must not misclassify a dot-directory as an extension:\n{err}"
        );
    }

    // 25d3. test_not_found_error_dot_directory_with_changes_falls_through
    #[test]
    fn test_not_found_error_dot_directory_with_changes_falls_through() {
        let _lock = CWD_LOCK.lock().unwrap();
        let tmp = tempfile::tempdir().expect("failed to create tempdir");
        let prev = std::env::current_dir().unwrap();
        // An empty tempdir outside any git repo: `repo_root()` returns None,
        // so `directory_shaped` falls back to trailing-slash-only and can't
        // find `.github` on disk. The overlay-based guard in
        // `extension_shaped` must still refuse to treat `.github` as an
        // extension, since an overlay lives under `.github/`.
        std::env::set_current_dir(tmp.path()).unwrap();
        let overlays = vec![overlay_at(".github/workflows/ci.yml")];
        let err = not_found_error(".github", &overlays);
        std::env::set_current_dir(&prev).unwrap();
        assert!(
            !err.contains("is an extension, not a path"),
            "expected no extension misclassification when an overlay has changes under the dir:\n{err}"
        );
    }

    // 25e. test_not_found_error_directory_shaped_trailing_slash
    #[test]
    fn test_not_found_error_directory_shaped_trailing_slash() {
        let overlays = vec![
            overlay_at("src/main.rs"),
            overlay_at("tests/foo.rs"),
            overlay_at("infra/main.tf"),
        ];
        let err = not_found_error("docs/", &overlays);
        assert!(
            err.contains("no changed files under 'docs/'"),
            "expected directory classification in:\n{err}"
        );
        assert!(
            err.contains("changed top-level dirs: infra, src, tests"),
            "expected sorted top-level dirs in:\n{err}"
        );
    }

    // 25f. test_not_found_error_directory_shaped_exists_on_disk
    #[test]
    fn test_not_found_error_directory_shaped_exists_on_disk() {
        // No trailing slash; classified as directory-shaped because `src` is a
        // real directory under this crate's repo root at test time. Hold
        // CWD_LOCK so a concurrent run_diff_in test can't chdir underneath us.
        let _lock = CWD_LOCK.lock().unwrap();
        let overlays = vec![overlay_at("tests/foo.rs")];
        let err = not_found_error("src", &overlays);
        assert!(
            err.contains("no changed files under 'src/'"),
            "expected directory classification for an on-disk dir in:\n{err}"
        );
        assert!(
            err.contains("changed top-level dirs: tests"),
            "expected top-level dirs in:\n{err}"
        );
    }

    // 25. test_bad_ref_teaches_default_branch
    #[test]
    fn test_bad_ref_teaches_default_branch() {
        let dir = tempfile::tempdir().expect("failed to create tempdir");
        let p = dir.path();
        git(p, &["init", "-b", "trunk"]);
        git(p, &["config", "user.email", "test@test.com"]);
        git(p, &["config", "user.name", "Test"]);
        git(p, &["commit", "--allow-empty", "-m", "initial"]);
        git(p, &["branch", "main"]);

        let result = run_diff_in(
            p,
            &DiffSource::GitRef("nonexistent-ref..HEAD".to_string()),
            None,
            None,
            false,
            None,
        );
        assert!(
            result.is_err(),
            "expected error for unresolvable ref 'nonexistent-ref'"
        );
        let err = result.unwrap_err();
        assert!(
            err.contains("default branch is 'main'"),
            "expected default-branch teaching hint in:\n{err}"
        );
        assert!(
            err.contains("main..HEAD"),
            "expected suggested range in:\n{err}"
        );
    }

    // 26. test_log_bad_ref_teaches_default_branch
    #[test]
    fn test_log_bad_ref_teaches_default_branch() {
        let dir = tempfile::tempdir().expect("failed to create tempdir");
        let p = dir.path();
        git(p, &["init", "-b", "trunk"]);
        git(p, &["config", "user.email", "test@test.com"]);
        git(p, &["config", "user.name", "Test"]);
        git(p, &["commit", "--allow-empty", "-m", "initial"]);
        git(p, &["branch", "main"]);

        let result = run_diff_in(
            p,
            &DiffSource::Log("nonexistent-ref..HEAD".to_string()),
            None,
            None,
            false,
            None,
        );
        assert!(
            result.is_err(),
            "expected error for unresolvable log range 'nonexistent-ref'"
        );
        let err = result.unwrap_err();
        assert!(
            err.contains("default branch is 'main'"),
            "expected default-branch teaching hint in:\n{err}"
        );
    }

    // 27. test_no_default_branch_hint_without_main_or_master
    #[test]
    fn test_no_default_branch_hint_without_main_or_master() {
        let dir = tempfile::tempdir().expect("failed to create tempdir");
        let p = dir.path();
        git(p, &["init", "-b", "trunk"]);
        git(p, &["config", "user.email", "test@test.com"]);
        git(p, &["config", "user.name", "Test"]);
        git(p, &["commit", "--allow-empty", "-m", "initial"]);

        let result = run_diff_in(
            p,
            &DiffSource::GitRef("nonexistent-ref..HEAD".to_string()),
            None,
            None,
            false,
            None,
        );
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert!(
            !err.contains("default branch"),
            "expected no default-branch hint without a local main/master:\n{err}"
        );
    }

    // 28. test_uncommitted_diff_errors_outside_repo
    #[test]
    fn test_uncommitted_diff_errors_outside_repo() {
        let dir = tempfile::tempdir().expect("failed to create tempdir");
        let result = run_diff_in(
            dir.path(),
            &DiffSource::GitUncommitted,
            None,
            None,
            false,
            None,
        );
        assert!(
            result.is_err(),
            "expected an error diffing outside a git repo, got: {result:?}"
        );
        let err = result.unwrap_err();
        assert!(
            err.to_lowercase().contains("git diff"),
            "expected the error to name the failing git command:\n{err}"
        );
    }

    // 29. test_log_mode_root_commit
    #[test]
    fn test_log_mode_root_commit() {
        let dir = setup_test_repo();
        let main_rs = dir.path().join("src/main.rs");

        let content = fs::read_to_string(&main_rs).unwrap();
        fs::write(
            &main_rs,
            content.replace("println!(\"hello\")", "println!(\"root test\")"),
        )
        .unwrap();
        git(dir.path(), &["add", "-A"]);
        git(dir.path(), &["commit", "-m", "second commit"]);

        let result = run_diff_in(
            dir.path(),
            &DiffSource::Log("HEAD".to_string()),
            None,
            None,
            false,
            None,
        )
        .unwrap();
        assert!(
            result.contains("initial"),
            "expected the root commit's message in log output:\n{result}"
        );
        assert!(
            result.contains("main.rs"),
            "expected the root commit's added file in log output:\n{result}"
        );
    }

    // 30. test_append_default_branch_hint_gated_by_stderr
    #[test]
    fn test_append_default_branch_hint_gated_by_stderr() {
        let _lock = CWD_LOCK.lock().unwrap();
        let dir = tempfile::tempdir().expect("failed to create tempdir");
        let p = dir.path();
        git(p, &["init", "-b", "main"]);
        git(p, &["config", "user.email", "test@test.com"]);
        git(p, &["config", "user.name", "Test"]);
        git(p, &["commit", "--allow-empty", "-m", "initial"]);

        let prev = std::env::current_dir().unwrap();
        std::env::set_current_dir(p).unwrap();

        let with_hint = append_default_branch_hint(
            "git diff failed".to_string(),
            "fatal: ambiguous argument 'x..HEAD': unknown revision or path not in the working tree.",
        );
        let without_hint = append_default_branch_hint(
            "git diff failed".to_string(),
            "fatal: unrelated failure, nothing to do with revisions",
        );

        std::env::set_current_dir(&prev).unwrap();

        assert!(
            with_hint.contains("default branch is 'main'"),
            "expected hint for unresolvable-ref stderr:\n{with_hint}"
        );
        assert!(
            !without_hint.contains("default branch"),
            "expected no hint for unrelated stderr:\n{without_hint}"
        );
    }

    // 31. test_parse_origin_head_ref
    #[test]
    fn test_parse_origin_head_ref() {
        assert_eq!(
            parse_origin_head_ref("refs/remotes/origin/release/stable"),
            Some("release/stable".to_string())
        );
        assert_eq!(
            parse_origin_head_ref("refs/remotes/origin/main"),
            Some("main".to_string())
        );
    }

    // 32. test_scope_prefix_no_match_on_shared_dir_name_stem
    #[test]
    fn test_scope_prefix_no_match_on_shared_dir_name_stem() {
        let dir = setup_test_repo();
        let fanout_extra_dir = dir.path().join("src/fanout_extra");
        fs::create_dir_all(&fanout_extra_dir).unwrap();
        let a_rs = fanout_extra_dir.join("a.rs");
        fs::write(&a_rs, "fn a() {\n    1\n}\n").unwrap();
        git(dir.path(), &["add", "-A"]);
        git(dir.path(), &["commit", "-m", "add fanout_extra"]);

        fs::write(&a_rs, "fn a() {\n    99\n}\n").unwrap();

        let result = run_diff_in(
            dir.path(),
            &DiffSource::GitUncommitted,
            Some("src/fanout"),
            None,
            false,
            None,
        );
        assert!(
            result.is_err(),
            "expected 'src/fanout' to NOT match 'src/fanout_extra/a.rs' (non-boundary prefix)"
        );
        assert!(result.unwrap_err().contains("not found"));
    }

    // 33. test_dot_slash_prefixed_scope_matches_directory
    #[test]
    fn test_dot_slash_prefixed_scope_matches_directory() {
        let dir = setup_test_repo();
        let fanout_dir = dir.path().join("src/fanout");
        fs::create_dir_all(&fanout_dir).unwrap();
        let a_rs = fanout_dir.join("a.rs");
        fs::write(&a_rs, "fn a() {\n    1\n}\n").unwrap();
        git(dir.path(), &["add", "-A"]);
        git(dir.path(), &["commit", "-m", "add fanout"]);

        fs::write(&a_rs, "fn a() {\n    99\n}\n").unwrap();

        let result = run_diff_in(
            dir.path(),
            &DiffSource::GitUncommitted,
            Some("./src/fanout"),
            None,
            false,
            None,
        )
        .unwrap();
        assert!(
            result.contains("fanout/a.rs"),
            "expected './'-prefixed scope to match directory:\n{result}"
        );
        assert!(result.contains("Scope filter"));
    }

    // 34. test_repo_root_scope_falls_through_to_full_overview
    #[test]
    fn test_repo_root_scope_falls_through_to_full_overview() {
        let dir = setup_test_repo();
        let main_rs = dir.path().join("src/main.rs");
        let content = fs::read_to_string(&main_rs).unwrap();
        fs::write(
            &main_rs,
            content.replace("println!(\"hello\")", "println!(\"hi\")"),
        )
        .unwrap();

        let root = git(dir.path(), &["rev-parse", "--show-toplevel"])
            .trim()
            .to_string();

        let result = run_diff_in(
            dir.path(),
            &DiffSource::GitUncommitted,
            Some(&root),
            None,
            false,
            None,
        )
        .unwrap();
        assert!(
            !result.contains("Scope filter"),
            "expected repo-root scope to fall through to unscoped overview:\n{result}"
        );
        assert!(result.contains("main.rs"));
    }

    // 35. test_absolute_file_scope_normalizes_to_repo_relative
    #[test]
    fn test_absolute_file_scope_normalizes_to_repo_relative() {
        let dir = setup_test_repo();
        let main_rs = dir.path().join("src/main.rs");
        let content = fs::read_to_string(&main_rs).unwrap();
        fs::write(
            &main_rs,
            content.replace("println!(\"hello\")", "println!(\"hi\")"),
        )
        .unwrap();

        let root = git(dir.path(), &["rev-parse", "--show-toplevel"])
            .trim()
            .to_string();
        let abs_scope = format!("{root}/src/main.rs");

        let relative_result = run_diff_in(
            dir.path(),
            &DiffSource::GitUncommitted,
            Some("src/main.rs"),
            None,
            false,
            None,
        )
        .unwrap();
        let absolute_result = run_diff_in(
            dir.path(),
            &DiffSource::GitUncommitted,
            Some(&abs_scope),
            None,
            false,
            None,
        )
        .unwrap();
        // Symbol order isn't deterministic across two `diff()` calls in one
        // process (`matching.rs`'s `match_symbols` HashMap iteration), so
        // sort both sides before comparing.
        let mut abs_lines: Vec<&str> = absolute_result.lines().collect();
        let mut rel_lines: Vec<&str> = relative_result.lines().collect();
        abs_lines.sort_unstable();
        rel_lines.sort_unstable();
        assert_eq!(
            abs_lines, rel_lines,
            "absolute file scope should resolve to the same file detail as its repo-relative spelling:\nabs:\n{absolute_result}\nrel:\n{relative_result}"
        );
    }

    // 36. test_scoped_overview_filters_warnings_to_retained_files
    #[test]
    fn test_scoped_overview_filters_warnings_to_retained_files() {
        let dir = setup_test_repo();
        let fanout_dir = dir.path().join("src/fanout");
        let other_dir = dir.path().join("src/other");
        fs::create_dir_all(&fanout_dir).unwrap();
        fs::create_dir_all(&other_dir).unwrap();

        let a_rs = fanout_dir.join("a.rs");
        let b_rs = other_dir.join("b.rs");
        let c_rs = other_dir.join("c.rs");
        let d_rs = other_dir.join("d.rs");
        fs::write(&a_rs, "fn shared(x: i32) {\n    x\n}\n").unwrap();
        fs::write(&b_rs, "fn shared(x: i32) {\n    x\n}\n").unwrap();
        fs::write(&c_rs, "fn lonely(x: i32) {\n    x\n}\n").unwrap();
        fs::write(&d_rs, "fn lonely(x: i32) {\n    x\n}\n").unwrap();
        git(dir.path(), &["add", "-A"]);
        git(dir.path(), &["commit", "-m", "add fanout+other"]);

        fs::write(&a_rs, "fn shared(x: i32, y: i32) {\n    x\n}\n").unwrap();
        fs::write(&b_rs, "fn shared(x: i32, y: i32) {\n    x\n}\n").unwrap();
        fs::write(&c_rs, "fn lonely(x: i32, y: i32) {\n    x\n}\n").unwrap();
        fs::write(&d_rs, "fn lonely(x: i32, y: i32) {\n    x\n}\n").unwrap();

        let result = run_diff_in(
            dir.path(),
            &DiffSource::GitUncommitted,
            Some("src/fanout"),
            None,
            false,
            None,
        )
        .unwrap();
        assert!(
            result.contains("`shared`"),
            "expected warning for `shared` (retained file in scope) in:\n{result}"
        );
        assert!(
            !result.contains("`lonely`"),
            "expected no warning for `lonely` (entirely out of scope) in:\n{result}"
        );
    }

    // 37. test_is_file_function_scope_ignores_windows_drive_colon
    #[test]
    fn test_is_file_function_scope_ignores_windows_drive_colon() {
        assert!(
            !is_file_function_scope("C:/repo/src"),
            "drive-letter colon at index 1 must not be treated as file:function separator"
        );
        assert!(
            !is_file_function_scope("C:\\repo\\src"),
            "drive-letter colon (backslash form) must not be treated as file:function separator"
        );
        assert!(
            is_file_function_scope("src/main.rs:hello"),
            "a real file:function scope must still be detected"
        );
        assert!(
            is_file_function_scope("C:/other/x.rs:hello"),
            "a colon after the drive-letter prefix must still be detected as file:function"
        );
    }

    // 38. test_normalize_scope_trims_trailing_slash
    #[test]
    fn test_normalize_scope_trims_trailing_slash() {
        assert_eq!(normalize_scope("src/fanout/"), "src/fanout");
    }

    // 39. test_normalize_scope_strips_leading_dot_slash
    #[test]
    fn test_normalize_scope_strips_leading_dot_slash() {
        assert_eq!(normalize_scope("./src/fanout"), "src/fanout");
    }

    // 40. test_normalize_scope_absolute_to_repo_relative_and_root_is_empty
    #[test]
    fn test_normalize_scope_absolute_to_repo_relative_and_root_is_empty() {
        let _lock = CWD_LOCK.lock().unwrap();
        let dir = setup_test_repo();
        let prev = std::env::current_dir().unwrap();
        std::env::set_current_dir(dir.path()).unwrap();
        let root = repo_root().unwrap();
        let abs_subdir = root.join("src/fanout").to_string_lossy().into_owned();
        let subdir_result = normalize_scope(&abs_subdir);
        let root_result = normalize_scope(&root.to_string_lossy());
        std::env::set_current_dir(&prev).unwrap();
        assert_eq!(subdir_result, "src/fanout");
        assert_eq!(root_result, "");
    }

    // 41. test_scope_not_found_suggestions_are_ordered_by_score
    #[test]
    fn test_scope_not_found_suggestions_are_ordered_by_score() {
        let dir = setup_test_repo();
        fs::create_dir_all(dir.path().join("src/domain")).unwrap();
        let manager_rs = dir.path().join("src/manager.rs");
        let domain_man_rs = dir.path().join("src/domain/man.rs");
        let unrelated = dir.path().join("totally_unrelated_zzqq.xyz");
        fs::write(&manager_rs, "fn m() {\n    1\n}\n").unwrap();
        fs::write(&domain_man_rs, "fn m() {\n    1\n}\n").unwrap();
        fs::write(&unrelated, "noise").unwrap();
        git(dir.path(), &["add", "-A"]);
        git(dir.path(), &["commit", "-m", "add candidates"]);

        let main_rs = dir.path().join("src/main.rs");
        let content = fs::read_to_string(&main_rs).unwrap();
        fs::write(
            &main_rs,
            content.replace("println!(\"hello\")", "println!(\"hi\")"),
        )
        .unwrap();
        fs::write(&manager_rs, "fn m() {\n    2\n}\n").unwrap();
        fs::write(&domain_man_rs, "fn m() {\n    2\n}\n").unwrap();
        fs::write(&unrelated, "noise changed").unwrap();

        let result = run_diff_in(
            dir.path(),
            &DiffSource::GitUncommitted,
            Some("src/man.rs"),
            None,
            false,
            None,
        );
        assert!(result.is_err());
        let err = result.unwrap_err();
        let man_pos = err
            .find("src/main.rs")
            .expect("expected src/main.rs suggested");
        let mgr_pos = err
            .find("src/manager.rs")
            .expect("expected src/manager.rs suggested");
        let domain_pos = err
            .find("src/domain/man.rs")
            .expect("expected src/domain/man.rs suggested");
        assert!(
            domain_pos < mgr_pos && mgr_pos < man_pos,
            "expected ranked order src/domain/man.rs < src/manager.rs < src/main.rs in:\n{err}"
        );
    }

    // 42. test_log_mode_absolute_file_scope_normalizes_to_repo_relative
    #[test]
    fn test_log_mode_absolute_file_scope_normalizes_to_repo_relative() {
        let dir = setup_test_repo();
        let main_rs = dir.path().join("src/main.rs");
        let content = fs::read_to_string(&main_rs).unwrap();
        fs::write(
            &main_rs,
            content.replace("println!(\"hello\")", "println!(\"log test\")"),
        )
        .unwrap();
        git(dir.path(), &["add", "-A"]);
        git(dir.path(), &["commit", "-m", "second commit"]);

        let root = git(dir.path(), &["rev-parse", "--show-toplevel"])
            .trim()
            .to_string();
        let abs_scope = format!("{root}/src/main.rs");

        let relative_result = run_diff_in(
            dir.path(),
            &DiffSource::Log("HEAD~1..HEAD".to_string()),
            Some("src/main.rs"),
            None,
            false,
            None,
        )
        .unwrap();
        let absolute_result = run_diff_in(
            dir.path(),
            &DiffSource::Log("HEAD~1..HEAD".to_string()),
            Some(&abs_scope),
            None,
            false,
            None,
        )
        .unwrap();
        assert_eq!(
            mask_commit_age(&absolute_result),
            mask_commit_age(&relative_result),
            "absolute file scope should resolve to the same commit history as its repo-relative spelling:\nabs:\n{absolute_result}\nrel:\n{relative_result}"
        );
    }
}
