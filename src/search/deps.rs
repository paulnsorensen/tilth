//! File-level dependency analysis: what a file imports and what imports it.
//! Used by `tilth_deps` for blast-radius checks before breaking changes.

use std::collections::{HashMap, HashSet};
use std::fmt::Write as _;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use crate::error::TilthError;
use crate::lang::detect_file_type;
use crate::lang::outline::{extract_import_source, get_outline_entries};
use crate::lang::spec::spec;
use crate::read::imports::{
    is_external, is_import_line, resolve_python_scoped, resolve_scoped_paths, target_ambiguity,
    PyRoots, Uncertainty,
};
use crate::search::callees::{extract_callee_names, resolve_callees};
use crate::search::callers::find_callers_batch;
use crate::types::Lang;
use crate::types::{FileType, OutlineKind};

/// Maximum number of exported symbols to search for in the reverse direction.
const MAX_EXPORTED_SYMBOLS: usize = 25;

/// Maximum number of dependents to show before truncation.
const MAX_DEPENDENTS: usize = 15;

/// Wall-clock bound for the reverse import scan. A scan that stops here
/// reports partial coverage; it never claims a complete dependent set.
const IMPORT_SCAN_BUDGET: Duration = Duration::from_secs(3);

/// Maximum number of unreadable or uncertain consumer paths named in the output.
const MAX_COVERAGE_PATHS: usize = 5;

/// Result of a full dependency analysis for a single file.
pub struct DepsResult {
    pub target: PathBuf,
    pub uses_local: Vec<LocalDep>,
    pub uses_external: Vec<String>,
    pub used_by: Vec<Dependent>,
    /// Total dependents found before truncation.
    pub total_dependents: usize,
    pub exported_count: usize,
    /// Actual number of symbols searched (may be < `exported_count` if capped).
    pub searched_count: usize,
    /// What the reverse import scan could not prove.
    pub reverse_coverage: ReverseCoverage,
}

/// Coverage of the reverse import scan. A default value means the scan saw
/// every in-scope consumer and resolved each one without uncertainty.
#[derive(Debug, Default)]
pub struct ReverseCoverage {
    /// Python consumers that could not be read.
    pub unreadable: Vec<PathBuf>,
    /// Python consumers with an unresolved import that can hide an edge to
    /// the target (ambiguous module, blocked or unavailable re-export owner).
    pub uncertain: Vec<PathBuf>,
    /// Directory entries the walker could not visit.
    pub walk_errors: usize,
    /// The scan stopped at its deadline.
    pub timed_out: bool,
}

impl ReverseCoverage {
    #[must_use]
    pub fn is_complete(&self) -> bool {
        self.unreadable.is_empty()
            && self.uncertain.is_empty()
            && self.walk_errors == 0
            && !self.timed_out
    }
}

/// A local file dependency with the symbols used from it.
pub struct LocalDep {
    pub path: PathBuf,
    pub symbols: Vec<String>,
}

/// A file that depends on the target, with symbol-level call detail.
pub struct Dependent {
    pub path: PathBuf,
    /// (`calling_function`, `called_symbol`, `line`) triples.
    pub symbols: Vec<(String, String, u32)>,
    /// Direct import edges to the target: `(module, line)` pairs. A dependent
    /// that only imports the target (never calls it) has an empty `symbols`.
    pub imports: Vec<(String, u32)>,
    pub is_test: bool,
}

/// Analyse the dependency graph for `path` within `scope`.
///
/// Phase 1: Extract exported symbols from the outline.
/// Phase 2: Forward dependencies — what this file uses.
/// Phase 3: Reverse dependencies — what uses this file.
pub fn analyze_deps(
    path: &Path,
    scope: &Path,
    bloom: &crate::index::bloom::BloomFilterCache,
) -> Result<DepsResult, TilthError> {
    // Canonicalize for reliable path comparison (callers return absolute paths).
    let path = &path.canonicalize().map_err(|e| TilthError::IoError {
        path: path.to_path_buf(),
        source: e,
    })?;

    let content = fs::read_to_string(path).map_err(|e| TilthError::IoError {
        path: path.clone(),
        source: e,
    })?;

    let FileType::Code(lang) = detect_file_type(path) else {
        // Non-code file: return empty deps gracefully.
        return Ok(DepsResult {
            target: path.clone(),
            uses_local: Vec::new(),
            uses_external: Vec::new(),
            used_by: Vec::new(),
            total_dependents: 0,
            exported_count: 0,
            searched_count: 0,
            reverse_coverage: ReverseCoverage::default(),
        });
    };

    // Package roots for scoped import resolution — discovered once from the
    // explicit scope and shared by the ambiguity check and both passes below.
    let import_roots = PyRoots::discover(scope);

    // Refuse to guess when the target's own module identity is ambiguous:
    // duplicate package roots make its dependent set unreliable, so report the
    // module and its bounded candidates instead of a confident wrong answer.
    if let Some(ambiguous) = target_ambiguity(path, &import_roots) {
        return Err(TilthError::AmbiguousModule {
            module: ambiguous.module,
            candidates: ambiguous
                .candidates
                .iter()
                .map(|p| p.strip_prefix(scope).unwrap_or(p).display().to_string())
                .collect(),
        });
    }

    // ── Phase 1: Extract exported symbols ────────────────────────────────────

    let entries = get_outline_entries(&content, lang);

    let mut all_names: Vec<String> = Vec::new();
    for entry in &entries {
        // Skip imports and re-export wrappers — they don't define symbols here.
        if matches!(entry.kind, OutlineKind::Import | OutlineKind::Export) {
            continue;
        }
        collect_symbol_names(entry, &mut all_names);
    }

    // Deduplicate
    all_names.sort();
    all_names.dedup();

    // Filter placeholder / noise names
    all_names.retain(|n| !is_placeholder_name(n));

    let exported_count = all_names.len();

    // Cap at MAX_EXPORTED_SYMBOLS, preferring longer (more specific) names
    let searched_count = if all_names.len() > MAX_EXPORTED_SYMBOLS {
        all_names.sort_by(|a, b| b.len().cmp(&a.len()).then_with(|| a.cmp(b)));
        all_names.truncate(MAX_EXPORTED_SYMBOLS);
        MAX_EXPORTED_SYMBOLS
    } else {
        all_names.len()
    };

    // ── Phase 2: Forward dependencies ────────────────────────────────────────

    // Local deps via callee resolution
    let callee_names = extract_callee_names(&content, lang, None);
    let resolved = resolve_callees(&callee_names, path, &content, bloom);

    // Group resolved callees by file
    let mut local_by_file: HashMap<PathBuf, Vec<String>> = HashMap::new();
    for callee in resolved {
        if callee.file != *path {
            local_by_file
                .entry(callee.file)
                .or_default()
                .push(callee.name);
        }
    }

    // Merge in import-resolved files (may not have resolved callees if symbols
    // weren't matched, but the import relationship itself is meaningful).
    // Scoped resolution adds absolute in-scope Python imports. Python refuses
    // to guess: any uncertain forward import (ambiguous module or blocked
    // re-export ownership) fails the whole call rather than silently
    // dropping the edge.
    let mut resolved_local_modules: HashSet<String> = HashSet::new();
    let import_files = if spec(lang).scoped_imports {
        let resolution = resolve_python_scoped(path, &content, &import_roots);
        if let Some(first) = resolution.uncertain.into_iter().next() {
            return Err(match first {
                Uncertainty::AmbiguousModule { module, candidates } => {
                    TilthError::AmbiguousModule {
                        module,
                        candidates: candidates
                            .iter()
                            .map(|p| p.strip_prefix(scope).unwrap_or(p).display().to_string())
                            .collect(),
                    }
                }
                Uncertainty::BlockedOwnership { module, name } => {
                    TilthError::BlockedOwnership { module, name }
                }
                Uncertainty::UnavailableOwner { module, name, path } => {
                    TilthError::UnavailableOwner { module, name, path }
                }
            });
        }
        resolution
            .edges
            .into_iter()
            .map(|edge| {
                resolved_local_modules.insert(edge.module);
                edge.path
            })
            .collect()
    } else {
        resolve_scoped_paths(path, &content, &import_roots)
    };
    for import_path in import_files {
        local_by_file.entry(import_path).or_default();
    }

    // Sort symbols within each dep, then build the list sorted by path
    let mut uses_local: Vec<LocalDep> = local_by_file
        .into_iter()
        .map(|(dep_path, mut syms)| {
            syms.sort();
            syms.dedup();
            LocalDep {
                path: dep_path,
                symbols: syms,
            }
        })
        .collect();
    uses_local.sort_by(|a, b| a.path.cmp(&b.path));

    // External deps via line-level import parsing
    let mut external_set: HashSet<String> = HashSet::new();
    for line in content.lines() {
        if !is_import_line(line, lang) {
            continue;
        }
        let source = extract_import_source(line, Some(lang));
        if source.is_empty() {
            continue;
        }
        if is_external(&source, lang)
            && !is_stdlib(&source, lang)
            && is_valid_module_path(&source)
            && !resolved_local_modules.contains(&source)
        {
            external_set.insert(source.clone());
        }
    }
    let mut uses_external: Vec<String> = external_set.into_iter().collect();
    uses_external.sort();

    // ── Phase 3: Reverse dependencies ────────────────────────────────────────

    // Call-site edges: files that invoke an exported symbol.
    let mut calls_by_file: HashMap<PathBuf, Vec<(String, String, u32)>> = HashMap::new();
    if searched_count > 0 {
        let symbols_set: HashSet<String> = all_names.iter().cloned().collect();
        let (raw_matches, _) = find_callers_batch(
            &symbols_set,
            scope,
            bloom,
            None,
            crate::search::callers::BATCH_EARLY_QUIT,
        )?;
        for (matched_symbol, caller_match) in raw_matches {
            // Exclude calls from within the target file itself (self-references)
            if caller_match.path == *path {
                continue;
            }
            calls_by_file.entry(caller_match.path).or_default().push((
                caller_match.calling_function,
                matched_symbol,
                caller_match.line,
            ));
        }
    }

    // Import edges: files that import the target directly, even without a call.
    // Only languages with scoped-import resolution resolve import edges here,
    // so other targets skip the scan.
    let (imports_by_file, reverse_coverage) = if spec(lang).scoped_imports {
        let deadline = Instant::now() + IMPORT_SCAN_BUDGET;
        collect_import_dependents(path, lang, scope, &import_roots, deadline)?
    } else {
        (HashMap::new(), ReverseCoverage::default())
    };

    // Union both edge kinds by file so an importer that never calls the target
    // still surfaces, and a file that both imports and calls appears once.
    let mut dep_paths: HashSet<PathBuf> = calls_by_file.keys().cloned().collect();
    dep_paths.extend(imports_by_file.keys().cloned());
    let target_dir = path.parent();
    let mut used_by: Vec<Dependent> = dep_paths
        .into_iter()
        .map(|dep_path| {
            let mut pairs = calls_by_file.get(&dep_path).cloned().unwrap_or_default();
            pairs.sort();
            pairs.dedup();
            let mut imports = imports_by_file.get(&dep_path).cloned().unwrap_or_default();
            imports.sort();
            imports.dedup();
            let is_test = is_test_file(&dep_path);
            Dependent {
                path: dep_path,
                symbols: pairs,
                imports,
                is_test,
            }
        })
        .collect();

    // Sort: same directory first, non-tests before tests, then alphabetical
    used_by.sort_by(|a, b| {
        let a_same_dir = target_dir.is_some_and(|d| a.path.parent() == Some(d));
        let b_same_dir = target_dir.is_some_and(|d| b.path.parent() == Some(d));
        b_same_dir
            .cmp(&a_same_dir)
            .then_with(|| a.is_test.cmp(&b.is_test))
            .then_with(|| a.path.cmp(&b.path))
    });

    let total_dependents = used_by.len();
    used_by.truncate(MAX_DEPENDENTS);

    Ok(DepsResult {
        target: path.clone(),
        uses_local,
        uses_external,
        used_by,
        total_dependents,
        exported_count,
        searched_count,
        reverse_coverage,
    })
}

/// Format a `DepsResult` as a compact, readable string.
///
/// Budget truncation priority (when `budget` tokens is too tight):
/// 1. Truncate "Used by" entries (keep header count)
/// 2. Truncate "Uses (external)" to count only
/// 3. Truncate "Uses (local)" symbol lists to file paths only
/// 4. Never truncate the header line
pub fn format_deps(result: &DepsResult, scope: &Path, budget: Option<usize>) -> String {
    let dep_count = result.total_dependents;
    let (prod_deps, test_deps): (Vec<_>, Vec<_>) = result.used_by.iter().partition(|d| !d.is_test);

    // ── Build sections (full fidelity first) ─────────────────────────────────

    // Header
    let rel_target = result
        .target
        .strip_prefix(scope)
        .unwrap_or(&result.target)
        .display()
        .to_string();
    // The partial marker lives in the header because the header is the one
    // part that budget truncation never removes.
    let header = format!(
        "# Deps: {} — {} local, {} external, {} dependent{}{}",
        rel_target,
        result.uses_local.len(),
        result.uses_external.len(),
        dep_count,
        if dep_count == 1 { "" } else { "s" },
        partial_marker(&result.reverse_coverage),
    );

    let uses_local_section = format_uses_local(&result.uses_local, scope, true);
    let uses_external_section = format_uses_external(&result.uses_external);
    let used_by_section = format_used_by(&prod_deps, scope, "## Used by");
    let used_by_tests_section = format_used_by(&test_deps, scope, "## Used by (tests)");

    let mut barrel_note = if result.exported_count > MAX_EXPORTED_SYMBOLS {
        format!(
            "\n\n> ({} of {} exports shown — barrel file detected)",
            result.searched_count, result.exported_count
        )
    } else {
        String::new()
    };
    barrel_note.push_str(&format_coverage_detail(&result.reverse_coverage, scope));

    // Full output
    let mut parts: Vec<String> = Vec::new();
    parts.push(header.clone());
    if !uses_local_section.is_empty() {
        parts.push(uses_local_section.clone());
    }
    if !uses_external_section.is_empty() {
        parts.push(uses_external_section.clone());
    }
    if !used_by_section.is_empty() {
        parts.push(used_by_section.clone());
    }
    if !used_by_tests_section.is_empty() {
        parts.push(used_by_tests_section.clone());
    }
    let truncated = result.total_dependents.saturating_sub(result.used_by.len());
    if truncated > 0 {
        parts.push(format!("... and {truncated} more dependents"));
    }
    if !barrel_note.is_empty() {
        parts.push(barrel_note.clone());
    }

    let full = parts.join("\n\n");
    let full_tokens = crate::types::estimate_tokens(full.len() as u64) as usize;

    let output = match budget {
        None => full,
        Some(b) if full_tokens <= b => full,
        Some(b) => {
            // Apply truncation in priority order
            apply_budget_truncation(
                &header,
                &uses_local_section,
                &uses_external_section,
                &prod_deps,
                &test_deps,
                &barrel_note,
                scope,
                b,
            )
        }
    };

    let token_est = crate::types::estimate_tokens(output.len() as u64);
    format!("{output}\n\n[~{token_est} tokens]")
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/// Collect symbol names from an outline entry and its children.
fn collect_symbol_names(entry: &crate::types::OutlineEntry, out: &mut Vec<String>) {
    out.push(entry.name.clone());
    for child in &entry.children {
        // Include public methods of classes/structs/impls
        if !matches!(child.kind, OutlineKind::Import | OutlineKind::Export) {
            out.push(child.name.clone());
        }
    }
}

/// ` (partial: …)` for the header when the reverse scan is incomplete.
fn partial_marker(coverage: &ReverseCoverage) -> String {
    if coverage.is_complete() {
        return String::new();
    }
    let mut reasons: Vec<String> = Vec::new();
    if !coverage.unreadable.is_empty() {
        reasons.push(format!("{} unreadable", coverage.unreadable.len()));
    }
    if !coverage.uncertain.is_empty() {
        reasons.push(format!("{} uncertain", coverage.uncertain.len()));
    }
    if coverage.walk_errors > 0 {
        reasons.push(format!("{} walk errors", coverage.walk_errors));
    }
    if coverage.timed_out {
        reasons.push("scan timed out".to_string());
    }
    format!(" (partial: {})", reasons.join(", "))
}

/// Bounded list of the consumers behind a partial reverse scan.
fn format_coverage_detail(coverage: &ReverseCoverage, scope: &Path) -> String {
    let mut out = String::new();
    for (label, paths) in [
        ("unreadable consumers", &coverage.unreadable),
        ("uncertain consumers", &coverage.uncertain),
    ] {
        if paths.is_empty() {
            continue;
        }
        let shown: Vec<String> = paths
            .iter()
            .take(MAX_COVERAGE_PATHS)
            .map(|p| p.strip_prefix(scope).unwrap_or(p).display().to_string())
            .collect();
        let more = paths.len().saturating_sub(shown.len());
        let _ = write!(out, "\n\n> {label}: {}", shown.join(", "));
        if more > 0 {
            let _ = write!(out, " (+{more} more)");
        }
    }
    out
}

/// Whether one consumer's unresolved import can hide an edge to `target`.
/// An ambiguous module hides the target only when the target is one of its
/// candidates; a blocked or unavailable re-export owner can be any file.
fn may_hide_target(uncertainty: &Uncertainty, target_canon: &Path) -> bool {
    match uncertainty {
        Uncertainty::AmbiguousModule { candidates, .. } => candidates
            .iter()
            .any(|c| c.canonicalize().is_ok_and(|c| c == target_canon)),
        Uncertainty::BlockedOwnership { .. } | Uncertainty::UnavailableOwner { .. } => true,
    }
}

/// Files under `scope` that import `target` directly (Python absolute or
/// relative imports), keyed by the dependent's scope-form path with the
/// `(module, line)` evidence for each edge, plus the coverage of the scan.
///
/// The scan uses the shared parallel walker policy and stops at `deadline`.
/// A consumer whose canonical path leaves the canonical scope is skipped, so
/// a narrower scope cannot discover outside consumers. Walker errors,
/// unreadable consumers, and consumers with an unresolved import that can
/// hide the target are recorded in the coverage; they are never dropped
/// silently.
#[allow(clippy::type_complexity)]
fn collect_import_dependents(
    target: &Path,
    lang: Lang,
    scope: &Path,
    roots: &PyRoots,
    deadline: Instant,
) -> Result<(HashMap<PathBuf, Vec<(String, u32)>>, ReverseCoverage), TilthError> {
    let target_canon = target
        .canonicalize()
        .unwrap_or_else(|_| target.to_path_buf());
    let scope_canon = scope.canonicalize().unwrap_or_else(|_| scope.to_path_buf());
    let edges: Mutex<HashMap<PathBuf, Vec<(String, u32)>>> = Mutex::new(HashMap::new());
    let unreadable: Mutex<Vec<PathBuf>> = Mutex::new(Vec::new());
    let uncertain: Mutex<Vec<PathBuf>> = Mutex::new(Vec::new());
    let walk_errors = AtomicUsize::new(0);
    let timed_out = AtomicBool::new(false);

    let walker = super::walker(scope, None)?;
    walker.run(|| {
        let (edges, unreadable, uncertain) = (&edges, &unreadable, &uncertain);
        let (walk_errors, timed_out) = (&walk_errors, &timed_out);
        let (target_canon, scope_canon) = (&target_canon, &scope_canon);
        Box::new(move |entry| {
            if Instant::now() >= deadline {
                timed_out.store(true, Ordering::Relaxed);
                return ignore::WalkState::Quit;
            }
            let Ok(entry) = entry else {
                walk_errors.fetch_add(1, Ordering::Relaxed);
                return ignore::WalkState::Continue;
            };
            if !entry.file_type().is_some_and(|ft| ft.is_file()) {
                return ignore::WalkState::Continue;
            }
            let candidate = entry.path();
            if detect_file_type(candidate) != FileType::Code(lang) {
                return ignore::WalkState::Continue;
            }
            let Ok(candidate_canon) = candidate.canonicalize() else {
                push_locked(unreadable, candidate.to_path_buf());
                return ignore::WalkState::Continue;
            };
            if candidate_canon == *target_canon || !candidate_canon.starts_with(scope_canon) {
                return ignore::WalkState::Continue;
            }
            let Ok(content) = fs::read_to_string(candidate) else {
                push_locked(unreadable, candidate.to_path_buf());
                return ignore::WalkState::Continue;
            };
            let resolution = resolve_python_scoped(candidate, &content, roots);
            let mut proven: Vec<(String, u32)> = Vec::new();
            for edge in resolution.edges {
                if edge.path.canonicalize().is_ok_and(|p| p == *target_canon) {
                    proven.push((edge.module, edge.line));
                }
            }
            if proven.is_empty() {
                // A proven dependent is already reported; only an unproven
                // consumer can be a hidden one.
                if resolution
                    .uncertain
                    .iter()
                    .any(|u| may_hide_target(u, target_canon))
                {
                    push_locked(uncertain, candidate.to_path_buf());
                }
            } else {
                edges
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner)
                    .entry(candidate.to_path_buf())
                    .or_default()
                    .extend(proven);
            }
            ignore::WalkState::Continue
        })
    });

    let into_sorted = |paths: Mutex<Vec<PathBuf>>| {
        let mut paths = paths
            .into_inner()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        paths.sort();
        paths
    };
    let coverage = ReverseCoverage {
        unreadable: into_sorted(unreadable),
        uncertain: into_sorted(uncertain),
        walk_errors: walk_errors.load(Ordering::Relaxed),
        timed_out: timed_out.load(Ordering::Relaxed),
    };
    let edges = edges
        .into_inner()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    Ok((edges, coverage))
}

fn push_locked(paths: &Mutex<Vec<PathBuf>>, path: PathBuf) {
    paths
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
        .push(path);
}

/// Returns true if the name is a noise/placeholder that should be excluded
/// from the reverse-dependency search. Also used by `fuzzy_symbol` to filter
/// the grok suggestion candidate pool — one home for the invariant.
pub(crate) fn is_placeholder_name(name: &str) -> bool {
    if name == "<anonymous>" {
        return true;
    }
    if name.starts_with('<') {
        return true;
    }
    if name.starts_with("impl ") {
        return true;
    }
    // Single-character names are too generic (e.g. `T`, `E`, `f`)
    if name.chars().count() == 1 {
        return true;
    }
    false
}

/// Returns true if the import source is a standard library module.
/// Agents can't navigate into stdlib — showing these is noise.
fn is_stdlib(source: &str, lang: crate::types::Lang) -> bool {
    crate::lang::spec::spec(lang).stdlib.matches(source)
}

/// Returns true if the string looks like a valid module/package path.
/// Filters out garbage from string literals that pass `is_import_line`.
fn is_valid_module_path(source: &str) -> bool {
    // Must not contain spaces (real module paths don't)
    if source.contains(' ') {
        return false;
    }
    // Must start with an alphanumeric, @, or dot
    source
        .chars()
        .next()
        .is_some_and(|c| c.is_alphanumeric() || c == '@' || c == '.')
}

use crate::types::is_test_file;

/// Format the "Uses (local)" section.
fn format_uses_local(deps: &[LocalDep], scope: &Path, with_symbols: bool) -> String {
    if deps.is_empty() {
        return String::new();
    }
    let mut out = String::from("## Uses (local)");
    for dep in deps {
        let rel = dep
            .path
            .strip_prefix(scope)
            .unwrap_or(&dep.path)
            .display()
            .to_string();
        if with_symbols && !dep.symbols.is_empty() {
            let _ = write!(out, "\n{:<30} {}", rel, dep.symbols.join(", "));
        } else {
            let _ = write!(out, "\n{rel}");
        }
    }
    out
}

/// Format the "Uses (external)" section.
fn format_uses_external(externals: &[String]) -> String {
    if externals.is_empty() {
        return String::new();
    }
    let mut out = String::from("## Uses (external)");
    for ext in externals {
        let _ = write!(out, "\n{ext}");
    }
    out
}

/// Format a "Used by" section from a slice of dependents.
fn format_used_by(deps: &[&Dependent], scope: &Path, heading: &str) -> String {
    if deps.is_empty() {
        return String::new();
    }
    let mut out = String::from(heading);
    for dep in deps {
        let rel = dep
            .path
            .strip_prefix(scope)
            .unwrap_or(&dep.path)
            .display()
            .to_string();
        // Group by (caller, line) for readability — keep the earliest line per caller
        let mut by_caller: HashMap<&str, (u32, Vec<&str>)> = HashMap::new();
        for (caller, symbol, line) in &dep.symbols {
            let entry = by_caller
                .entry(caller.as_str())
                .or_insert((*line, Vec::new()));
            entry.0 = entry.0.min(*line);
            entry.1.push(symbol.as_str());
        }
        let mut callers: Vec<(&str, u32, Vec<&str>)> = by_caller
            .into_iter()
            .map(|(caller, (line, syms))| (caller, line, syms))
            .collect();
        callers.sort_unstable_by(|a, b| a.1.cmp(&b.1).then_with(|| a.0.cmp(b.0)));
        for (caller, line, syms) in callers {
            let loc = format!("{rel}:{line}");
            let joined = syms.join(", ");
            let _ = write!(out, "\n{loc:<30} {caller:<20} \u{2192} {joined}");
        }
        // Import-only edges (no call site) still name the target dependency.
        for (module, line) in &dep.imports {
            let loc = format!("{rel}:{line}");
            let label = "(import)";
            let _ = write!(out, "\n{loc:<30} {label:<20} \u{2192} {module}");
        }
    }
    out
}

/// Apply progressive budget truncation and reassemble the output.
#[allow(clippy::too_many_arguments)]
fn apply_budget_truncation(
    header: &str,
    uses_local_full: &str,
    uses_external_full: &str,
    prod_deps: &[&Dependent],
    test_deps: &[&Dependent],
    barrel_note: &str,
    scope: &Path,
    budget: usize,
) -> String {
    // Try progressively degraded versions
    #[allow(clippy::type_complexity)]
    let candidates: &[fn(
        &str,
        &str,
        &str,
        &[&Dependent],
        &[&Dependent],
        &str,
        &Path,
    ) -> String] = &[
        // Level 0: no tests
        |hdr, ul, ue, pd, _td, bn, sc| {
            assemble(&[hdr, ul, ue, &format_used_by(pd, sc, "## Used by"), bn])
        },
        // Level 1: no used-by entries at all
        |hdr, ul, ue, pd, _td, bn, _sc| {
            let count = pd.len();
            let note = if count > 0 {
                format!("\n\n(... {count} more dependents)")
            } else {
                String::new()
            };
            assemble(&[hdr, ul, ue, &note, bn])
        },
        // Level 2: external as count only
        |hdr, ul, _ue, _pd, _td, bn, _sc| assemble(&[hdr, ul, bn]),
        // Level 3: local as paths only (no symbols)
        |hdr, ul, _ue, _pd, _td, _bn, _sc| {
            // Strip symbol lists: each line is "path_padded  symbols" — take only up to first space run
            let local_lines: Vec<&str> = ul
                .lines()
                .skip(1) // skip heading
                .map(|l| l.split_whitespace().next().unwrap_or(l))
                .collect();
            let paths_only = if local_lines.is_empty() {
                String::new()
            } else {
                format!("## Uses (local)\n{}", local_lines.join("\n"))
            };
            assemble(&[hdr, &paths_only])
        },
        // Level 4: header only
        |hdr, _ul, _ue, _pd, _td, _bn, _sc| hdr.to_string(),
    ];

    for (level, candidate_fn) in candidates.iter().enumerate() {
        let candidate = candidate_fn(
            header,
            uses_local_full,
            uses_external_full,
            prod_deps,
            test_deps,
            barrel_note,
            scope,
        );
        if level == 0 && fits_budget(&candidate, budget) {
            return candidate;
        }
        if level > 0 {
            if let Some(candidate) = with_truncation_notice(&candidate, budget) {
                return candidate;
            }
        }
    }

    // Absolute fallback: header only.
    with_truncation_notice(header, budget).unwrap_or_else(|| header.to_string())
}

fn with_truncation_notice(candidate: &str, budget: usize) -> Option<String> {
    let candidate = format!(
        "{candidate}\n\n... truncated — raise `budget` (currently {budget}) to see full dependency detail"
    );
    fits_budget(&candidate, budget).then_some(candidate)
}

fn fits_budget(output: &str, budget: usize) -> bool {
    crate::types::estimate_tokens(output.len() as u64) as usize <= budget
}

/// Join non-empty parts with double newlines.
fn assemble(parts: &[&str]) -> String {
    parts
        .iter()
        .filter(|s| !s.trim().is_empty())
        .copied()
        .collect::<Vec<_>>()
        .join("\n\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write_file(root: &Path, rel: &str, body: &str) {
        let path = root.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, body).unwrap();
    }

    #[test]
    fn analyze_deps_reports_direct_and_reexport_importers() {
        // The #197 src-layout fixture: rankings.py is imported directly by
        // direct.py and, through the package surface's explicit re-export, by
        // surface.py. Both must appear as dependents.
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path();
        write_file(root, "packages/producer/src/producer/__init__.py", "");
        write_file(
            root,
            "packages/producer/src/producer/ingest/rankings.py",
            "class RankingEntry:\n    pass\n",
        );
        write_file(
            root,
            "packages/producer/src/producer/ingest/__init__.py",
            "from .rankings import RankingEntry\n",
        );
        write_file(root, "consumer/src/consumer/__init__.py", "");
        write_file(
            root,
            "consumer/src/consumer/direct.py",
            "from producer.ingest.rankings import RankingEntry\n",
        );
        write_file(
            root,
            "consumer/src/consumer/surface.py",
            "from producer.ingest import RankingEntry\n",
        );

        let bloom = crate::index::bloom::BloomFilterCache::new();
        let target = root.join("packages/producer/src/producer/ingest/rankings.py");
        let result = analyze_deps(&target, root, &bloom).expect("analyze_deps");
        let dependents: Vec<String> = result
            .used_by
            .iter()
            .map(|d| d.path.to_string_lossy().replace('\\', "/"))
            .collect();
        assert!(
            dependents
                .iter()
                .any(|p| p.ends_with("consumer/src/consumer/surface.py")),
            "re-export importer missing: {dependents:?}"
        );
        assert!(
            dependents
                .iter()
                .any(|p| p.ends_with("consumer/src/consumer/direct.py")),
            "direct importer missing: {dependents:?}"
        );
    }

    #[test]
    fn budget_truncation_keeps_notice_inside_budget() {
        let header = "# deps for src/lib.rs";
        let uses_local = format!(
            "## Uses (local)\n{}",
            (0..100)
                .map(|i| format!("src/module_{i}.rs  symbol_{i}"))
                .collect::<Vec<_>>()
                .join("\n")
        );
        let budget = 32;

        let out = apply_budget_truncation(
            header,
            &uses_local,
            "",
            &[],
            &[],
            "",
            Path::new("."),
            budget,
        );

        assert_eq!(
            out,
            "# deps for src/lib.rs\n\n... truncated — raise `budget` (currently 32) to see full dependency detail"
        );
        assert!(
            fits_budget(&out, budget),
            "truncation notice must count against budget: {out}"
        );
    }

    #[test]
    fn go_stdlib_fmt_is_stdlib() {
        assert!(is_stdlib("fmt", crate::types::Lang::Go));
    }

    #[test]
    fn go_stdlib_fmtlib_is_not_stdlib() {
        // "fmtlib" is not a Go stdlib package—previously matched via starts_with("fmt")
        assert!(!is_stdlib("fmtlib", crate::types::Lang::Go));
    }

    #[test]
    fn go_stdlib_fmtutil_is_not_stdlib() {
        assert!(!is_stdlib("fmtutil", crate::types::Lang::Go));
    }

    #[test]
    fn go_stdlib_multi_segment_paths_are_stdlib() {
        // Regression: multi-segment stdlib imports (single-line form
        // `import "net/http"`) must classify as stdlib via their root segment.
        // The exact-match allowlist briefly regressed these to "external".
        for path in [
            "net/http",
            "encoding/json",
            "path/filepath",
            "crypto/sha256",
            "text/template",
            "container/list",
            "database/sql",
        ] {
            assert!(
                is_stdlib(path, crate::types::Lang::Go),
                "{path} should be classified as Go stdlib"
            );
        }
    }

    #[test]
    fn go_local_multi_segment_path_is_not_stdlib() {
        // A local/third-party multi-segment package whose root isn't stdlib.
        assert!(!is_stdlib("mypackage/sub", crate::types::Lang::Go));
    }

    #[test]
    fn go_local_package_without_dot_is_not_stdlib() {
        // A local package like "mypackage" has no dot but is NOT stdlib—
        // the old !source.contains('.') rule wrongly classified it as stdlib.
        assert!(!is_stdlib("mypackage", crate::types::Lang::Go));
    }

    #[test]
    fn go_external_dotted_path_is_not_stdlib() {
        assert!(!is_stdlib(
            "github.com/gin-gonic/gin",
            crate::types::Lang::Go
        ));
    }

    #[test]
    fn go_stdlib_cmp_and_maps_are_stdlib() {
        // Go 1.21+ added `cmp` and `maps` to the standard library.
        assert!(is_stdlib("cmp", crate::types::Lang::Go));
        assert!(is_stdlib("maps", crate::types::Lang::Go));
    }

    /// The #197 fixture plus a second owner of `RankingEntry`, so the package
    /// surface cannot attribute the name to one file.
    fn blocked_ownership_fixture(root: &Path) {
        write_file(root, "packages/producer/src/producer/__init__.py", "");
        write_file(
            root,
            "packages/producer/src/producer/ingest/rankings.py",
            "class RankingEntry:\n    pass\n",
        );
        write_file(
            root,
            "packages/producer/src/producer/ingest/other.py",
            "class RankingEntry:\n    pass\n",
        );
        write_file(
            root,
            "packages/producer/src/producer/ingest/__init__.py",
            "from .rankings import RankingEntry\nfrom .other import RankingEntry\n",
        );
        write_file(root, "consumer/src/consumer/__init__.py", "");
        write_file(
            root,
            "consumer/src/consumer/direct.py",
            "from producer.ingest.rankings import RankingEntry\n",
        );
        write_file(
            root,
            "consumer/src/consumer/surface.py",
            "from producer.ingest import RankingEntry\n",
        );
    }

    #[test]
    fn uncertain_consumer_makes_the_dependent_set_partial() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path();
        blocked_ownership_fixture(root);
        let bloom = crate::index::bloom::BloomFilterCache::new();
        let target = root.join("packages/producer/src/producer/ingest/rankings.py");
        let result = analyze_deps(&target, root, &bloom).expect("analyze_deps");

        // The proven importer is reported; the blocked one is named, not dropped.
        assert!(result
            .used_by
            .iter()
            .any(|d| d.path.ends_with("consumer/src/consumer/direct.py")));
        let uncertain: Vec<_> = result.reverse_coverage.uncertain.iter().collect();
        assert_eq!(uncertain.len(), 1, "{uncertain:?}");
        assert!(uncertain[0].ends_with("consumer/src/consumer/surface.py"));
        assert!(!result.reverse_coverage.is_complete());

        let full = format_deps(&result, root, None);
        assert!(full.contains("(partial: 1 uncertain)"), "{full}");
        assert!(
            full.contains("uncertain consumers: consumer/src/consumer/surface.py"),
            "{full}"
        );
        // The marker survives the tightest budget because it is in the header.
        let tight = format_deps(&result, root, Some(1));
        assert!(tight.contains("(partial: 1 uncertain)"), "{tight}");
    }

    #[test]
    fn unreadable_consumer_makes_the_dependent_set_partial() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path();
        blocked_ownership_fixture(root);
        std::fs::remove_file(root.join("consumer/src/consumer/surface.py")).unwrap();
        // Invalid UTF-8: the consumer exists but cannot be read as source.
        std::fs::write(
            root.join("consumer/src/consumer/broken.py"),
            [0xff, 0xfe, 0x00],
        )
        .unwrap();
        let bloom = crate::index::bloom::BloomFilterCache::new();
        let target = root.join("packages/producer/src/producer/ingest/rankings.py");
        let result = analyze_deps(&target, root, &bloom).expect("analyze_deps");
        let unreadable = &result.reverse_coverage.unreadable;
        assert_eq!(unreadable.len(), 1, "{unreadable:?}");
        assert!(unreadable[0].ends_with("consumer/src/consumer/broken.py"));
        assert!(format_deps(&result, root, None).contains("(partial: 1 unreadable)"));
    }

    #[test]
    fn complete_reverse_scan_has_no_partial_marker() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path();
        blocked_ownership_fixture(root);
        std::fs::remove_file(root.join("consumer/src/consumer/surface.py")).unwrap();
        let bloom = crate::index::bloom::BloomFilterCache::new();
        let target = root.join("packages/producer/src/producer/ingest/rankings.py");
        let result = analyze_deps(&target, root, &bloom).expect("analyze_deps");
        assert!(result.reverse_coverage.is_complete());
        assert!(!format_deps(&result, root, None).contains("partial"));
    }

    #[test]
    fn narrower_scope_excludes_outside_consumers() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path();
        blocked_ownership_fixture(root);
        let bloom = crate::index::bloom::BloomFilterCache::new();
        let target = root.join("packages/producer/src/producer/ingest/rankings.py");
        let scope = root.join("packages/producer");
        let result = analyze_deps(&target, &scope, &bloom).expect("analyze_deps");
        let outside = |p: &PathBuf| p.to_string_lossy().contains("consumer/src");
        assert!(!result.used_by.iter().any(|d| outside(&d.path)));
        assert!(!result.reverse_coverage.uncertain.iter().any(outside));
    }

    #[cfg(unix)]
    #[test]
    fn symlinked_directory_does_not_import_outside_consumers() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path();
        blocked_ownership_fixture(root);
        let scope = root.join("packages/producer");
        std::os::unix::fs::symlink(root.join("consumer"), scope.join("linked")).unwrap();
        let bloom = crate::index::bloom::BloomFilterCache::new();
        let target = root.join("packages/producer/src/producer/ingest/rankings.py");
        let result = analyze_deps(&target, &scope, &bloom).expect("analyze_deps");
        assert!(
            result
                .used_by
                .iter()
                .all(|d| !d.path.starts_with(scope.join("linked"))),
            "a consumer behind a symlink that leaves the scope must be skipped"
        );
        assert!(result.reverse_coverage.uncertain.is_empty());
    }

    #[test]
    fn expired_deadline_reports_a_timed_out_reverse_scan() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path();
        blocked_ownership_fixture(root);
        let target = root.join("packages/producer/src/producer/ingest/rankings.py");
        let roots = PyRoots::discover(root);
        let (edges, coverage) =
            collect_import_dependents(&target, Lang::Python, root, &roots, Instant::now())
                .expect("scan");
        assert!(edges.is_empty());
        assert!(coverage.timed_out);
        assert_eq!(partial_marker(&coverage), " (partial: scan timed out)");
    }
}
