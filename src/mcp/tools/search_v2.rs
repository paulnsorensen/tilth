//! The canonical `tilth_search` engine: deterministic query routing (path ->
//! regex -> signature-prefix normalization -> filename-shaped miss ->
//! symbol/ambiguous -> literal -> miss) with bounded grok/deps enrichment on
//! unique hits, plus a `follow` branch that executes the continuation hints a
//! prior result handed back (see
//! `.hallouminate/wiki/adr/tilth-search-v2-roadmap-006.md`).

use std::path::Path;
use std::time::Instant;

use serde_json::{json, Value};

use crate::cache::OutlineCache;
use crate::index::bloom::BloomFilterCache;
use crate::session::Session;
use crate::telemetry::{SearchTelemetryRecord, TelemetrySink};
use crate::types::Match;

use super::require_cwd;

/// Characters that mark a query as a regex pattern. `.` and `/` are
/// deliberately excluded so ordinary paths (`src/mcp/mod.rs`) never trip
/// this before the path route gets first look.
const REGEX_METACHARS: &[char] = &[
    '\\', '+', '*', '?', '(', ')', '[', ']', '|', '^', '$', '{', '}',
];

mod continuations;
use continuations::{Follow, Target};

struct SearchFailure {
    message: String,
    class: &'static str,
}

impl SearchFailure {
    fn new(message: impl Into<String>, class: &'static str) -> Self {
        Self {
            message: message.into(),
            class,
        }
    }
}

struct SearchRun {
    response: String,
    route: String,
    routes_tried: Vec<String>,
    partial: bool,
    timeout: bool,
    dependency_coverage: f64,
    shard_state: String,
    budget_limited: bool,
}

pub(in crate::mcp) fn tool_search_v2(
    args: &Value,
    cache: &OutlineCache,
    session: &Session,
    bloom: &BloomFilterCache,
    telemetry: &TelemetrySink,
    client: &str,
    worktree: &str,
) -> Result<String, String> {
    let start = Instant::now();
    let first_call = session.search_count() == 0;
    let run = run_search_v2(args, cache, session, bloom, client);
    let mut record = SearchTelemetryRecord {
        verb: "tilth_search".into(),
        version: crate::telemetry::SCHEMA_VERSION,
        route: "error".into(),
        routes_tried: Vec::new(),
        first_call,
        latency_ms: 0,
        result_tokens: 0,
        partial: false,
        timeout: false,
        budget_limited: false,
        dependency_coverage: 0.0,
        shard_state: "none".into(),
        client: client.into(),
        worktree: worktree.into(),
        outcome: "ok".into(),
        error_class: None,
    };
    let result = match run {
        Ok(success) => {
            record.route = success.route;
            record.routes_tried = success.routes_tried;
            record.result_tokens = crate::types::estimate_tokens(success.response.len() as u64);
            record.partial = success.partial;
            record.timeout = success.timeout;
            record.budget_limited = success.budget_limited;
            record.dependency_coverage = success.dependency_coverage;
            record.shard_state = success.shard_state;
            Ok(success.response)
        }
        Err(failure) => {
            record.route = failure.class.into();
            record.outcome = "error".into();
            record.error_class = Some(failure.class.into());
            Err(failure.message)
        }
    };
    record.latency_ms = u64::try_from(start.elapsed().as_millis()).unwrap_or(u64::MAX);
    let _ = telemetry.record(&record);
    result
}

fn run_search_v2(
    args: &Value,
    cache: &OutlineCache,
    session: &Session,
    bloom: &BloomFilterCache,
    client: &str,
) -> Result<SearchRun, SearchFailure> {
    let object = args
        .as_object()
        .ok_or_else(|| SearchFailure::new("search arguments must be an object", "bad_object"))?;
    // Keep dispatch error precedence while recording rejected budgets here.
    let budget = match args.get("budget") {
        None => crate::budget::DEFAULT_BUDGET,
        Some(value) => value
            .as_u64()
            .filter(|n| *n > 0)
            .ok_or_else(|| SearchFailure::new("budget must be a positive integer", "bad_budget"))?,
    };
    let cwd = require_cwd(args).map_err(|e| SearchFailure::new(e, "bad_cwd"))?;
    if object
        .keys()
        .any(|k| !matches!(k.as_str(), "queries" | "cwd" | "budget"))
    {
        return Err(SearchFailure::new(
            "search accepts only queries, cwd, and budget",
            "unknown_keys",
        ));
    }
    let entries = args
        .get("queries")
        .and_then(Value::as_array)
        .ok_or_else(|| SearchFailure::new("missing queries", "missing_queries"))?;
    if entries.is_empty() {
        return Err(SearchFailure::new(
            format!("queries must contain 1-10 entries; got {}", entries.len()),
            "empty_queries",
        ));
    }
    if entries.len() > 10 {
        return Err(SearchFailure::new(
            format!("queries must contain 1-10 entries; got {}", entries.len()),
            "oversized_queries",
        ));
    }
    let mut follows: Vec<Option<Follow>> = Vec::with_capacity(entries.len());
    let mut structural = crate::search::StructuralPatterns::default();
    for entry in entries {
        let object = entry
            .as_object()
            .ok_or_else(|| SearchFailure::new("each entry must be an object", "bad_query_entry"))?;
        if ["query", "follow", "pattern"]
            .iter()
            .filter(|key| object.contains_key(**key))
            .count()
            != 1
        {
            return Err(SearchFailure::new(
                "each entry requires exactly one of query, follow, or pattern",
                "bad_query_entry",
            ));
        }
        if let Some(hint) = object.get("follow") {
            if object.len() != 1 {
                return Err(SearchFailure::new(
                    "follow entries accept only follow",
                    "bad_follow",
                ));
            }
            follows.push(Some(
                Follow::parse(hint, cwd, cache).map_err(|e| SearchFailure::new(e, "bad_follow"))?,
            ));
        } else if object.contains_key("pattern") {
            parse_structural_entry(object, cwd, &mut structural)?;
            follows.push(None);
        } else {
            if object.keys().any(|k| k != "query" && k != "glob") {
                return Err(SearchFailure::new(
                    "query entries accept only query and glob",
                    "bad_query_entry",
                ));
            }
            if !entry["query"].is_string() {
                return Err(SearchFailure::new(
                    "query must be a string",
                    "bad_query_entry",
                ));
            }
            validate_entry_glob(object, cwd, "bad_query_entry")?;
            follows.push(None);
        }
    }
    let mut results: Vec<(Value, Option<FileCounts>)> = Vec::with_capacity(entries.len());
    let mut hints = Vec::new();
    let mut routes_tried = Vec::with_capacity(entries.len());
    let mut normalizations = Vec::new();
    for (entry, follow) in entries.iter().zip(&follows) {
        let mut counts = None;
        let (mut result, route, mut entry_hints, diagnostic) = if let Some(follow) = follow {
            session.record_follow();
            let result = follow
                .execute(cwd, bloom, client, cache)
                .map_err(|e| SearchFailure::new(e, "follow_error"))?;
            (result, follow.kind.clone(), Vec::new(), None)
        } else if let Some(pattern) = entry.get("pattern").and_then(Value::as_str) {
            let language = entry["language"].as_str().unwrap();
            session.record_structural_search();
            let scan = structural
                .search(
                    language,
                    pattern,
                    cwd,
                    entry.get("glob").and_then(Value::as_str),
                    cache,
                )
                .map_err(|error| SearchFailure::new(error.to_string(), "structural_error"))?;
            let (result, file_counts) = structural_result(scan, pattern, language);
            counts = Some(file_counts);
            (result, "structural".into(), Vec::new(), None)
        } else {
            route_query(
                entry["query"].as_str().unwrap(),
                entry.get("glob").and_then(Value::as_str),
                cwd,
                cache,
                session,
            )
            .map_err(|e| SearchFailure::new(e.to_string(), "route_error"))?
        };
        if result.get("target").is_some() && follow.is_none() {
            let target: Target = serde_json::from_value(result["target"].clone())
                .map_err(|e| SearchFailure::new(e.to_string(), "dependency_error"))?;
            result["dependency_impact"] = continuations::dependencies(&target, cwd, client, cache)
                .map_err(|e| SearchFailure::new(e, "dependency_error"))?;
            if result["dependency_impact"]["coverage"] != "complete" {
                mark_partial(&mut result);
            }
        }
        routes_tried.push(route);
        hints.append(&mut entry_hints);
        results.push((result, counts));
        if let Some(diagnostic) = diagnostic {
            normalizations.push(diagnostic);
        }
    }
    let dependency_states: Vec<(bool, bool, bool)> = results
        .iter()
        .filter_map(|(r, _)| r.get("dependency_impact"))
        .map(|d| {
            (
                d["coverage"] == "complete",
                d["timed_out"] == true,
                d["index_state"] == "unavailable",
            )
        })
        .collect();
    let partial = results.iter().any(|(r, _)| r["completeness"] == "partial");
    let timeout = dependency_states.iter().any(|&(_, timed_out, _)| timed_out);
    let dependency_coverage = if dependency_states.is_empty() {
        1.0
    } else {
        let complete = dependency_states.iter().filter(|&&(c, _, _)| c).count();
        f64::from(u32::try_from(complete).unwrap())
            / f64::from(u32::try_from(dependency_states.len()).unwrap())
    };
    let shard_state = if dependency_states.is_empty() {
        "none"
    } else if dependency_states.iter().any(|&(_, _, missing)| missing) {
        "unavailable"
    } else {
        "open"
    };
    let route = if routes_tried.len() > 1 {
        "batch".to_string()
    } else {
        routes_tried[0].clone()
    };
    let diagnostics = if normalizations.is_empty() {
        json!({})
    } else {
        json!({"normalizations": normalizations})
    };
    let (output, budget_limited) = reduce_response(results, &hints, &diagnostics, budget)
        .map_err(|e| SearchFailure::new(e, "budget_error"))?;
    Ok(SearchRun {
        response: output,
        route,
        routes_tried,
        partial,
        timeout,
        dependency_coverage,
        shard_state: shard_state.into(),
        budget_limited,
    })
}

/// Check that an entry's optional `glob` is a string and builds a walker.
fn validate_entry_glob(
    object: &serde_json::Map<String, Value>,
    cwd: &Path,
    class: &'static str,
) -> Result<(), SearchFailure> {
    let glob = match object.get("glob") {
        None => None,
        Some(Value::String(glob)) => Some(glob.as_str()),
        Some(_) => return Err(SearchFailure::new("glob must be a string", class)),
    };
    crate::search::walker(cwd, glob)
        .map(|_| ())
        .map_err(|error| SearchFailure::new(error.to_string(), class))
}

/// Validate one structural entry and compile its pattern into `structural`.
fn parse_structural_entry(
    object: &serde_json::Map<String, Value>,
    cwd: &Path,
    structural: &mut crate::search::StructuralPatterns,
) -> Result<(), SearchFailure> {
    if object
        .keys()
        .any(|key| !matches!(key.as_str(), "pattern" | "language" | "glob"))
    {
        return Err(SearchFailure::new(
            "structural entries accept only pattern, language, and glob",
            "bad_structural",
        ));
    }
    let pattern = object
        .get("pattern")
        .and_then(Value::as_str)
        .ok_or_else(|| SearchFailure::new("pattern must be a string", "bad_structural"))?;
    let language = object
        .get("language")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            SearchFailure::new(
                format!(
                    "language must be {}",
                    crate::lang::spec::structural_language_list()
                ),
                "bad_structural",
            )
        })?;
    validate_entry_glob(object, cwd, "bad_structural")?;
    structural
        .prepare(language, pattern)
        .map_err(|error| SearchFailure::new(error, "bad_structural"))
}

/// Build the result record for one structural scan, returning it with the
/// per-file match counts the budget trim needs.
fn structural_result(
    scan: crate::search::StructuralScan,
    pattern: &str,
    language: &str,
) -> (Value, FileCounts) {
    let mut result = base_result(
        pattern,
        "structural",
        if scan.retained == 0 { "no_match" } else { "ok" },
    );
    result["language"] = json!(language);
    result["view"] = json!("matches");
    let total: usize = scan.file_counts.values().sum();
    result["total_matches"] = json!(total);
    result["files_matched"] = json!(scan.file_counts.len());
    result["items"] = json!(scan.groups);
    if scan.limited {
        result["note"] = json!(format!(
            "Showing the first {} of {total} matches. Narrow with glob.",
            scan.retained
        ));
        result["match_limited"] = json!(true);
    }
    if scan.skipped_files > 0 {
        result["skipped_files"] = json!(scan.skipped_files);
    }
    if scan.limited || scan.skipped_files > 0 {
        mark_partial(&mut result);
    }
    (result, scan.file_counts)
}

/// Mark a result incomplete. Only an `ok` status degrades to `partial`; an
/// `ambiguous` or `no_match` verdict keeps its own meaning and carries the
/// incompleteness in `completeness`.
fn mark_partial(result: &mut Value) {
    if result["status"] == "ok" {
        result["status"] = json!("partial");
    }
    result["completeness"] = json!("partial");
}

/// Optional payloads a budget trim may drop, as `(owner, field)`. A `None`
/// owner addresses the result record itself, `Some(key)` a nested object.
const REMOVABLE: [(Option<&str>, &str); 6] = [
    (None, "core"),
    (None, "preview"),
    (None, "items"),
    (None, "candidates"),
    (Some("dependency_impact"), "imports"),
    (Some("dependency_impact"), "dependents"),
];

/// Serialize the response, dropping optional payloads until the token estimate
/// fits `budget`. Each result carries the per-file match counts that let a
/// structural result narrow instead of vanish. Plain payloads go first,
/// largest-first and sized once up front (dropping one never changes another's
/// size). Structural `items` are narrowed last by `narrow_structural` against
/// the length the other drops left, and re-serialized per narrowed result.
/// Length is tracked by exact deltas, so the whole response is serialized once
/// more at the end. Entries and hints are never removed, so a budget too small
/// for the required metadata is an error rather than a lossy answer. Returns
/// the output and whether any result was budget-limited.
fn reduce_response(
    results: Vec<(Value, Option<FileCounts>)>,
    hints: &[Value],
    diagnostics: &Value,
    budget: u64,
) -> Result<(String, bool), String> {
    let (results, counts): (Vec<Value>, Vec<Option<FileCounts>>) = results.into_iter().unzip();
    let mut response = json!({"results": results, "hints": hints, "diagnostics": diagnostics});
    let output = serde_json::to_string(&response).map_err(|e| e.to_string())?;
    let mut len = output.len();
    if crate::types::estimate_tokens(len as u64) <= budget {
        return Ok((output, false));
    }
    let Some(results) = response["results"].as_array() else {
        return Err("search response must carry a results array".into());
    };
    let mut candidates: Vec<(usize, Option<&str>, &str, usize)> = Vec::new();
    for (index, result) in results.iter().enumerate() {
        for (owner, field) in REMOVABLE {
            let parent = match owner {
                None => result.as_object(),
                Some(key) => result.get(key).and_then(Value::as_object),
            };
            let Some(parent) = parent else { continue };
            let Some(value) = parent.get(field) else {
                continue;
            };
            // Dropping the field removes `"field":<value>` plus its separating
            // comma, which is there whenever the owner keeps another member.
            let size = field.len() + 3 + usize::from(parent.len() > 1) + value.to_string().len();
            candidates.push((index, owner, field, size));
        }
    }
    // Stable sort: equally sized payloads keep result order, then `REMOVABLE` order.
    candidates.sort_by_key(|candidate| std::cmp::Reverse(candidate.3));
    let (structural, plain): (Vec<_>, Vec<_>) =
        candidates
            .into_iter()
            .partition(|&(index, owner, field, _)| {
                owner.is_none()
                    && field == "items"
                    && counts[index].as_ref().is_some_and(|c| !c.is_empty())
            });

    for (index, owner, field, size) in plain {
        if crate::types::estimate_tokens(len as u64) <= budget {
            break;
        }
        let result = &mut response["results"][index];
        let removed = match owner {
            None => result.as_object_mut().and_then(|o| o.remove(field)),
            Some(key) => result[key].as_object_mut().and_then(|o| o.remove(field)),
        };
        if removed.is_none() {
            continue;
        }
        len -= size;
        // The trim markers below add bytes back; measure the record around them
        // so `len` stays exact and the final serialization needs no re-check.
        let before = result.to_string().len();
        if let Some(key) = owner {
            result[key]["coverage"] = json!("partial");
        }
        mark_partial(result);
        result["budget_limited"] = json!(true);
        len = len + result.to_string().len() - before;
    }
    let mut narrowed_originals: Vec<(usize, Value)> = Vec::new();
    for (index, ..) in structural {
        if crate::types::estimate_tokens(len as u64) <= budget {
            break;
        }
        let Some(counts) = counts[index].as_ref() else {
            continue;
        };
        let result = &mut response["results"][index];
        let rest = len - result.to_string().len();
        let narrowed = narrow_structural(result, counts, &|bytes| {
            crate::types::estimate_tokens((rest + bytes) as u64) <= budget
        });
        len = rest + narrowed.to_string().len();
        narrowed_originals.push((index, std::mem::replace(result, narrowed)));
    }
    // Early narrows were sized against the later results' full `items`; once
    // those shrink, re-narrow each against the final length and keep any
    // richer view that now fits.
    for (index, original) in &narrowed_originals {
        let Some(counts) = counts[*index].as_ref() else {
            continue;
        };
        let result = &mut response["results"][*index];
        let current = result.to_string().len();
        let rest = len - current;
        let renarrowed = narrow_structural(original, counts, &|bytes| {
            crate::types::estimate_tokens((rest + bytes) as u64) <= budget
        });
        let renarrowed_len = renarrowed.to_string().len();
        if renarrowed_len > current {
            len = rest + renarrowed_len;
            *result = renarrowed;
        }
    }
    if crate::types::estimate_tokens(len as u64) > budget {
        return Err(format!(
            "budget {budget} cannot fit required search metadata"
        ));
    }
    let output = serde_json::to_string(&response).map_err(|e| e.to_string())?;
    let budget_limited = response["results"]
        .as_array()
        .is_some_and(|results| results.iter().any(|r| r["budget_limited"] == true));
    Ok((output, budget_limited))
}

/// A longer file list is not actionable for an agent, so it skips to directory counts.
const MAX_LISTED_FILES: usize = 100;

/// Matches per file for one structural result, counted before the retention cap.
type FileCounts = std::collections::BTreeMap<String, usize>;

/// Directory-count groups keyed by `(directory, direct)`. A `direct` group holds
/// only that directory's own files and matches the glob `<dir>/*`; any other
/// group holds its whole subtree and matches `<dir>/**`. Picks the smallest
/// depth (at least 1) that yields two groups, else the deepest. Root-level
/// files group as `.`. Values are `(matches, files)`.
fn directory_groups(
    counts: &FileCounts,
) -> std::collections::BTreeMap<(String, bool), (usize, usize)> {
    let max_depth = counts
        .keys()
        .map(|path| path.matches('/').count())
        .max()
        .unwrap_or(0)
        .max(1);
    let group = |depth: usize| {
        let mut groups = std::collections::BTreeMap::<(String, bool), (usize, usize)>::new();
        for (path, count) in counts {
            let key = match path.rsplit_once('/') {
                None => (".".to_string(), false),
                Some((parent, _)) => {
                    let parts: Vec<&str> = parent.split('/').collect();
                    if parts.len() < depth {
                        (parent.to_string(), true)
                    } else {
                        (parts[..depth].join("/"), false)
                    }
                }
            };
            let entry = groups.entry(key).or_default();
            entry.0 += count;
            entry.1 += 1;
        }
        groups
    };
    let mut groups = group(1);
    for depth in 2..=max_depth {
        if groups.len() >= 2 {
            break;
        }
        groups = group(depth);
    }
    groups
}

const GLOB_METACHARS: [char; 7] = ['[', ']', '{', '}', '*', '?', '\\'];

/// Escape glob metacharacters so a literal path can be a `glob` value.
fn escape_glob(path: &str) -> String {
    let mut escaped = String::with_capacity(path.len());
    for c in path.chars() {
        if GLOB_METACHARS.contains(&c) {
            escaped.push('\\');
        }
        escaped.push(c);
    }
    escaped
}

/// Replace a structural result's `items` that no longer fit the budget with the
/// richest view that does: a prefix of the single matched file, per-file
/// counts, per-directory counts, or nothing. `fits` takes the candidate
/// result's serialized length. When no view fits, returns the plain
/// `items`-removed result, which is never larger than any view.
fn narrow_structural(result: &Value, counts: &FileCounts, fits: &dyn Fn(usize) -> bool) -> Value {
    let mut base = result.clone();
    if let Some(object) = base.as_object_mut() {
        object.remove("items");
    }
    mark_partial(&mut base);
    base["budget_limited"] = json!(true);
    let fitting = |candidate: &Value| fits(candidate.to_string().len());
    prefix_tier(result, &base, counts, fits)
        .or_else(|| files_tier(&base, counts).filter(&fitting))
        .or_else(|| directories_tier(&base, counts).filter(&fitting))
        .or_else(|| none_tier(&base, counts).filter(&fitting))
        .unwrap_or(base)
}

/// Longest prefix of a single file's matches that fits. Each match is
/// serialized once; a prefix's length is the empty candidate's length plus the
/// match lengths, their commas, and the extra digits of the repeated count.
fn prefix_tier(
    result: &Value,
    base: &Value,
    counts: &FileCounts,
    fits: &dyn Fn(usize) -> bool,
) -> Option<Value> {
    if counts.len() != 1 {
        return None;
    }
    let total: usize = counts.values().sum();
    let path = counts.keys().next()?;
    let matches = result["items"][0]["matches"].as_array()?;
    let build = |keep: usize| {
        let mut candidate = base.clone();
        candidate["items"] = json!([{"path": path, "matches": matches[..keep]}]);
        candidate["shown"] = json!(keep);
        candidate["note"] = json!(format!(
            "Showing the first {keep} of {total} matches in {path}. Raise budget to see more."
        ));
        candidate
    };
    let mut prefix_sums = Vec::with_capacity(matches.len() + 1);
    prefix_sums.push(0);
    for item in matches {
        prefix_sums.push(prefix_sums.last().unwrap() + item.to_string().len());
    }
    let empty = build(0).to_string().len();
    let len_for = |keep: usize| {
        let digits = keep.checked_ilog10().map_or(1, |log| log as usize + 1);
        empty + prefix_sums[keep] + keep.saturating_sub(1) + 2 * (digits - 1)
    };
    let (mut low, mut high) = (0, matches.len());
    while low < high {
        let middle = (low + high).div_ceil(2);
        if fits(len_for(middle)) {
            low = middle;
        } else {
            high = middle - 1;
        }
    }
    if low == 0 {
        return None;
    }
    let candidate = build(low);
    debug_assert_eq!(candidate.to_string().len(), len_for(low));
    Some(candidate)
}

fn files_tier(base: &Value, counts: &FileCounts) -> Option<Value> {
    if counts.len() > MAX_LISTED_FILES {
        return None;
    }
    let total: usize = counts.values().sum();
    let mut files = base.clone();
    files["view"] = json!("files");
    files["files"] = Value::Array(
        counts
            .iter()
            .map(|(path, count)| json!([path, count]))
            .collect(),
    );
    let mut note = format!(
        "Too many matches ({total} in {} files) for the budget. Rerun this entry with glob set to a listed path.",
        counts.len()
    );
    if counts.keys().any(|path| !path.contains('/')) {
        note.push_str(" Prefix a top-level file with / to match only that file.");
    }
    files["note"] = json!(note);
    Some(files)
}

fn directories_tier(base: &Value, counts: &FileCounts) -> Option<Value> {
    let total: usize = counts.values().sum();
    let file_total = counts.len();
    let groups = directory_groups(counts);
    let mut top: Vec<(&String, &usize)> = counts.iter().collect();
    top.sort_by(|a, b| b.1.cmp(a.1).then(a.0.cmp(b.0)));
    let example = groups
        .keys()
        .find(|(dir, _)| dir != ".")
        .map(|(dir, direct)| format!("{}/{}", escape_glob(dir), if *direct { "*" } else { "**" }));
    let escapes = groups.keys().any(|(dir, _)| dir.contains(GLOB_METACHARS));
    let mut note = match &example {
        Some(example) => format!(
            "Too many matches ({total} in {file_total} files across {} {}) for the budget. Rerun this entry with glob set to a directory, e.g. \"{example}\".",
            groups.len(),
            if groups.len() == 1 { "directory" } else { "directories" },
        ),
        None => format!(
            "Too many matches ({total} in {file_total} top-level files) for the budget. Rerun this entry with glob set to / plus a file name, e.g. \"/{}\".",
            escape_glob(top[0].0)
        ),
    };
    if escapes {
        note.push_str(r" Escape [ ] { } * ? \ in a path with a backslash.");
    }
    let mut directories = base.clone();
    directories["view"] = json!("directories");
    directories["directories"] = Value::Array(
        groups
            .iter()
            .map(|((dir, direct), (count, files))| {
                let label = if *direct {
                    format!("{dir}/*")
                } else {
                    dir.clone()
                };
                json!([label, count, files])
            })
            .collect(),
    );
    directories["top_files"] = Value::Array(
        top.into_iter()
            .take(10)
            .map(|(path, count)| json!([path, count]))
            .collect(),
    );
    directories["note"] = json!(note);
    Some(directories)
}

fn none_tier(base: &Value, counts: &FileCounts) -> Option<Value> {
    let total: usize = counts.values().sum();
    let mut none = base.clone();
    none["view"] = json!("none");
    none["note"] = json!(format!(
        "Too many matches ({total} in {} files) for the budget. Raise budget or narrow with glob.",
        counts.len()
    ));
    Some(none)
}

/// Route one query through the deterministic precedence: path -> regex ->
/// signature-prefix normalization -> filename-shaped miss -> symbol/ambiguous
/// -> literal -> miss. Returns the result record, the resolved route name
/// (for telemetry), any hints emitted for it, and an optional normalization
/// diagnostic when the query was rewritten before routing.
fn route_query(
    query: &str,
    glob: Option<&str>,
    cwd: &Path,
    cache: &OutlineCache,
    session: &Session,
) -> Result<(Value, String, Vec<Value>, Option<Value>), crate::error::TilthError> {
    session.record_search(query);

    // 1. path — existing file or dir, resolved relative to cwd (or as-is if absolute).
    let candidate = cwd.join(query);
    if candidate.exists() {
        if candidate.is_file() {
            if matches!(
                crate::lang::detect_file_type(&candidate),
                crate::types::FileType::Code(_)
            ) {
                let target_spec = format!("{query}:1");
                let (mut result, hints) = unique_hit(
                    &target_spec,
                    "path",
                    &candidate,
                    1,
                    None,
                    None,
                    cwd,
                    glob,
                    cache,
                )?;
                result["query"] = json!(query);
                return Ok((result, "path".to_string(), hints, None));
            }
            let (result, route) = content_route(query, glob, cwd, cache, Some("path"))?;
            return Ok((result, route, Vec::new(), None));
        }
        let result = base_result(query, "path", "ok");
        return Ok((result, "path".to_string(), Vec::new(), None));
    }

    // 2. regex — contains a regex metacharacter (`.` and `/` don't count).
    // A glob naming one exact existing file is single-file-bounded by
    // `search::exact_glob_target` inside the walker — no separate short-circuit needed here.
    if query.chars().any(|c| REGEX_METACHARS.contains(&c)) {
        let search_result = crate::search::search_regex_raw(query, cwd, glob)?;
        let mut result = raw_result(query, "regex", &search_result);
        result["preview"] = json!(crate::search::format_raw_result(&search_result, cache)?);
        return Ok((result, "regex".to_string(), Vec::new(), None));
    }

    // 3. signature-prefix normalization — strip a leading declaration keyword
    // (`fn foo` -> `foo`) before identifier/path routing, so a pasted
    // signature-shaped phrase still hits the underlying symbol.
    if let Some((kw, ident)) = strip_signature_prefix(query) {
        if is_identifier(ident) {
            let (mut result, route, hints) = route_identifier(ident, glob, cwd, cache)?;
            result["query"] = json!(query);
            let diag = json!({
                "query": query,
                "normalized_to": ident,
                "stripped_keyword": kw,
            });
            return Ok((result, route, hints, Some(diag)));
        }
    }

    // 4. filename-shaped miss — a bare basename with an extension that doesn't
    // exist verbatim: suggest fuzzy-matched real paths instead of falling
    // through to identifier/literal routing.
    if Path::new(query).extension().is_some() && !query.contains('/') {
        if let crate::read::fuzzy_path::FuzzyResolution::Suggestions(suggestions) =
            crate::read::fuzzy_path::resolve_fuzzy_path(
                cwd,
                query,
                crate::read::fuzzy_path::GateProfile::Search,
            )
        {
            if !suggestions.is_empty() {
                let mut result = base_result(query, "path", "ambiguous");
                result["candidates"] = json!(suggestions
                    .iter()
                    .map(|p| json!({"path": p}))
                    .collect::<Vec<_>>());
                return Ok((result, "path".to_string(), Vec::new(), None));
            }
        }
    }

    // 5. symbol / ambiguous — bare identifier: prefer definitions, then reuse
    // usage matches or search literal content when no symbols were found.
    if is_identifier(query) {
        let (result, route, hints) = route_identifier(query, glob, cwd, cache)?;
        return Ok((result, route, hints, None));
    }

    // 6. literal — content search (non-identifier phrases only).
    let (result, route) = content_route(query, glob, cwd, cache, None)?;
    Ok((result, route, Vec::new(), None))
}

/// Route a bare identifier through the definitions-first symbol cascade:
/// unique code definition -> ambiguous multi-definition -> usage/literal
/// fallback -> miss. Shared by the plain identifier route and the
/// signature-prefix-normalized route.
fn route_identifier(
    query: &str,
    glob: Option<&str>,
    cwd: &Path,
    cache: &OutlineCache,
) -> Result<(Value, String, Vec<Value>), crate::error::TilthError> {
    let sym_result = crate::search::search_symbol_raw_cached(query, cwd, glob, cache)?;
    let discovery_partial = sym_result.files_unreadable > 0
        || sym_result.definitions
            > sym_result
                .matches
                .iter()
                .filter(|m| m.is_definition)
                .count();
    let mut code_defs: Vec<Match> = sym_result
        .matches
        .iter()
        .filter(|m| {
            m.is_definition
                && matches!(
                    crate::lang::detect_file_type(&m.path),
                    crate::types::FileType::Code(_)
                )
        })
        .cloned()
        .collect();
    code_defs.sort_by(|a, b| {
        a.path
            .cmp(&b.path)
            .then(a.line.cmp(&b.line))
            .then(a.def_range.cmp(&b.def_range))
            // Prefer the innermost declaration when a wrapper and its body share a span.
            .then(b.def_byte_range.cmp(&a.def_byte_range))
    });
    code_defs.dedup_by(|a, b| {
        let overlapping_occurrence = match (a.def_byte_range, b.def_byte_range) {
            (Some((a_start, a_end)), Some((b_start, b_end))) => a_start < b_end && b_start < a_end,
            _ => a.def_byte_range == b.def_byte_range,
        };
        let same_declaration = a.path == b.path && a.line == b.line && a.def_range == b.def_range;
        if same_declaration && overlapping_occurrence {
            // Wrapper and body entries describe one declaration; retain legacy path:line hints.
            a.def_byte_range = None;
            true
        } else {
            false
        }
    });
    if code_defs.len() == 1 {
        let target = &code_defs[0];
        let (mut result, hints) = unique_hit(
            query,
            "symbol",
            &target.path,
            target.line,
            target.def_range.map(|(_, end)| end),
            target.def_byte_range,
            cwd,
            glob,
            cache,
        )?;
        if discovery_partial {
            mark_partial(&mut result);
        }
        return Ok((result, "symbol".to_string(), hints));
    }
    if code_defs.len() > 1 {
        let content_result = crate::search::search_content_raw(query, cwd, glob)?;
        let mut result = base_result(query, "ambiguous", "ambiguous");
        if discovery_partial
            || content_result.files_unreadable > 0
            || content_result.total_found > content_result.matches.len()
        {
            mark_partial(&mut result);
        }
        result["candidates"] = json!(candidates(&code_defs, cwd));
        if content_result.total_found > 0 {
            result["preview"] = json!(crate::search::format_raw_result(&content_result, cache)?);
        }
        return Ok((result, "ambiguous".to_string(), Vec::new()));
    }
    if sym_result.total_found > 0 {
        let mut result = raw_result(query, "literal", &sym_result);
        result["preview"] = json!(crate::search::format_raw_result(&sym_result, cache)?);
        return Ok((result, "literal".to_string(), Vec::new()));
    }
    let (mut result, route) = content_route(query, glob, cwd, cache, None)?;
    if sym_result.files_unreadable > 0 {
        mark_partial(&mut result);
    }
    Ok((result, route, Vec::new()))
}

/// The literal-content tail shared by every route that ends in a content
/// search. `route_name` pins the recorded route (the non-code path route);
/// `None` derives `literal` on a hit and `miss` on none. A `preview` is
/// attached only when the search actually found something.
fn content_route(
    query: &str,
    glob: Option<&str>,
    cwd: &Path,
    cache: &OutlineCache,
    route_name: Option<&str>,
) -> Result<(Value, String), crate::error::TilthError> {
    let content_result = crate::search::search_content_raw(query, cwd, glob)?;
    let route = route_name.unwrap_or(if content_result.total_found > 0 {
        "literal"
    } else {
        "miss"
    });
    let mut result = raw_result(query, route, &content_result);
    if content_result.total_found > 0 {
        result["preview"] = json!(crate::search::format_raw_result(&content_result, cache)?);
    }
    Ok((result, route.to_string()))
}

/// Split `query` into `(keyword, identifier)` when it is exactly a
/// declaration keyword followed by a single identifier token (e.g.
/// `"fn detect_file_type"` -> `("fn", "detect_file_type")`).
fn strip_signature_prefix(query: &str) -> Option<(&str, &str)> {
    const KEYWORDS: &[&str] = &[
        "fn",
        "func",
        "function",
        "def",
        "class",
        "struct",
        "enum",
        "trait",
        "interface",
        "impl",
        "type",
    ];
    let mut parts = query.split_whitespace();
    let kw = parts.next()?;
    let ident = parts.next()?;
    if parts.next().is_some() {
        return None;
    }
    if KEYWORDS.contains(&kw) {
        Some((kw, ident))
    } else {
        None
    }
}

fn raw_result(query: &str, route: &str, raw: &crate::types::SearchResult) -> Value {
    let mut result = base_result(
        query,
        route,
        if raw.total_found == 0 {
            "no_match"
        } else {
            "ok"
        },
    );
    result["total_found"] = json!(raw.total_found);
    if raw.files_unreadable > 0 || raw.total_found > raw.matches.len() {
        mark_partial(&mut result);
    }
    result
}

fn base_result(query: &str, resolved_as: &str, status: &str) -> Value {
    json!({
        "query": query,
        "resolved_as": resolved_as,
        "status": status,
        "completeness": "complete",
    })
}

/// Build a candidates array from ranked matches (already ranked/deduped by
/// `symbol::search`).
fn candidates(matches: &[Match], cwd: &Path) -> Vec<Value> {
    matches
        .iter()
        .map(|m| {
            json!({
                "path": display_rel(&m.path, cwd),
                "line": m.line,
                "is_definition": m.is_definition,
                "def_name": m.def_name,
                "occurrence": m.def_byte_range,
            })
        })
        .collect()
}

/// Bodies and signatures from files on the secrets denylist are never emitted;
/// v1's formatters suppressed the same content through `SECRET_REDACTION_NOTICE`.
fn redact_secret_text(path: &Path, text: String) -> String {
    if crate::search::path_is_secret_file(path) {
        crate::search::SECRET_REDACTION_NOTICE
            .trim_start()
            .to_string()
    } else {
        text
    }
}

fn unique_hit(
    query: &str,
    resolved_as: &str,
    target_path: &Path,
    target_line: u32,
    semantic_end: Option<u32>,
    occurrence: Option<(usize, usize)>,
    cwd: &Path,
    glob: Option<&str>,
    cache: &OutlineCache,
) -> Result<(Value, Vec<Value>), crate::error::TilthError> {
    let (source_path, line, name, body, core_partial) = if resolved_as == "path" {
        let content = std::fs::read_to_string(target_path).map_err(|source| {
            crate::error::TilthError::IoError {
                path: target_path.to_path_buf(),
                source,
            }
        })?;
        let body = content.lines().take(60).collect::<Vec<_>>().join("\n");
        let core_partial = content.lines().count() > 60;
        (target_path.to_path_buf(), None, None, body, core_partial)
    } else {
        let (target, content, _) = match occurrence {
            Some(occurrence) => crate::search::grok::resolve_candidate_with_source_occurrence(
                target_path,
                target_line,
                semantic_end,
                query,
                occurrence,
                cache,
            )?,
            None => crate::search::grok::resolve_candidate_with_source(
                target_path,
                target_line,
                semantic_end,
                query,
                cache,
            )?,
        };
        let span_start = target.span_start_line;
        let end = target.end_line;
        let body = content
            .lines()
            .skip(span_start.saturating_sub(1) as usize)
            .take((end - span_start + 1).min(60) as usize)
            .collect::<Vec<_>>()
            .join("\n");
        (
            target.path,
            Some(target.start_line),
            Some(target.name),
            body,
            end - span_start + 1 > 60,
        )
    };
    let target = Target {
        path: display_rel(&source_path, cwd),
        line,
        name,
        scope: cwd.to_string_lossy().into(),
        occurrence,
        glob: glob.map(str::to_string),
    };
    if glob.is_some() && !target.allows(&source_path, cwd) {
        return Ok((base_result(query, resolved_as, "no_match"), Vec::new()));
    }
    let mut result = base_result(query, resolved_as, "ok");
    result["core"] = json!(redact_secret_text(&source_path, body));
    result["target"] = json!(target);
    if core_partial {
        mark_partial(&mut result);
    }
    let hints = target.hints();
    Ok((result, hints))
}

fn is_identifier(query: &str) -> bool {
    let mut chars = query.chars();
    match chars.next() {
        Some(c) if c == '_' || c.is_ascii_alphabetic() => {}
        _ => return false,
    }
    chars.all(|c| c == '_' || c.is_ascii_alphanumeric())
}

fn display_rel(path: &Path, cwd: &Path) -> String {
    let canonical_cwd = cwd.canonicalize().unwrap_or_else(|_| cwd.to_path_buf());
    path.strip_prefix(cwd)
        .or_else(|_| path.strip_prefix(&canonical_cwd))
        .unwrap_or(path)
        .display()
        .to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    /// Reduce a plain response whose results carry no structural counts.
    fn reduce(response: &Value, budget: u64) -> Result<(String, bool), String> {
        let results = response["results"]
            .as_array()
            .unwrap()
            .iter()
            .map(|result| (result.clone(), None))
            .collect();
        let hints = response["hints"].as_array().unwrap().clone();
        reduce_response(results, &hints, &response["diagnostics"], budget)
    }

    fn structural_record(items: Value) -> Value {
        json!({"query": "wrap($A)", "resolved_as": "structural", "status": "ok",
            "completeness": "complete", "view": "matches", "items": items})
    }

    #[test]
    fn narrowed_structural_is_never_larger_than_plain_item_removal() {
        let counts: FileCounts = (0..60)
            .map(|i| (format!("dir{}/f{i}.py", i % 3), 2))
            .collect();
        let result = structural_record(json!([]));
        let mut plain = result.clone();
        plain.as_object_mut().unwrap().remove("items");
        mark_partial(&mut plain);
        plain["budget_limited"] = json!(true);
        let plain_len = plain.to_string().len();
        for limit in [
            plain_len,
            plain_len + 10,
            plain_len + 60,
            plain_len + 200,
            plain_len + 800,
            100_000,
        ] {
            let narrowed = narrow_structural(&result, &counts, &|bytes| bytes <= limit);
            assert!(
                narrowed.to_string().len() <= limit,
                "limit {limit}: {narrowed}"
            );
        }
    }

    #[test]
    fn prefix_tier_keeps_the_longest_prefix_that_fits() {
        let matches: Vec<Value> = (1..=120)
            .map(|i| json!([i, i, i * 7, i * 7 + 5, {"A": [[i, i, 1, 2]]}]))
            .collect();
        let counts: FileCounts = [("a.py".to_string(), 120)].into();
        let result = structural_record(json!([{"path": "a.py", "matches": matches}]));
        let shown = |limit: usize| {
            let narrowed = narrow_structural(&result, &counts, &|bytes| bytes <= limit);
            assert!(narrowed.to_string().len() <= limit || narrowed.get("shown").is_none());
            narrowed["shown"].as_u64()
        };
        let mut seen = std::collections::BTreeSet::new();
        for limit in (500..6000).step_by(53) {
            let Some(keep) = shown(limit) else { continue };
            seen.insert(keep);
            let exact = narrow_structural(&result, &counts, &|bytes| bytes <= limit)
                .to_string()
                .len();
            assert_eq!(shown(exact), Some(keep));
            assert!(shown(exact - 1).is_none_or(|smaller| smaller < keep));
        }
        assert!(seen.len() > 10, "{seen:?}");
    }

    #[test]
    fn structural_narrowing_runs_after_plain_payloads_are_dropped() {
        let counts: FileCounts = (0..30).map(|i| (format!("d/f{i:02}.py"), 20)).collect();
        let groups: Vec<Value> = counts
            .keys()
            .map(|path| json!({"path": path, "matches": vec![json!([1, 1, 0, 11, {}]); 20]}))
            .collect();
        let plain = json!({"query": "x y", "resolved_as": "literal", "status": "ok",
            "completeness": "complete", "preview": "x".repeat(3000)});
        let results = vec![
            (plain, None),
            (structural_record(json!(groups)), Some(counts)),
        ];
        let (output, limited) = reduce_response(results, &[], &json!({}), 300).unwrap();
        let parsed: Value = serde_json::from_str(&output).unwrap();
        assert!(limited);
        assert!(parsed["results"][0].get("preview").is_none());
        assert_eq!(parsed["results"][1]["view"], "files");
    }

    #[test]
    fn unreadable_unique_discovery_reports_partial_before_dependency_work() {
        let tmp = tempfile::tempdir().unwrap();
        std::fs::write(tmp.path().join("a.rs"), "fn root() {}\n").unwrap();
        std::fs::write(tmp.path().join("unreadable.rs"), [0xff, 0xfe]).unwrap();
        let (cache, _session, _bloom) = components();
        let (result, route, hints) = route_identifier("root", None, tmp.path(), &cache).unwrap();
        assert_eq!(route, "symbol");
        assert_eq!(result["status"], "partial");
        assert_eq!(result["completeness"], "partial");
        assert_eq!(result["core"], "fn root() {}");
        assert_eq!(hints.len(), 5);
    }

    #[test]
    fn unique_symbol_hit_recovers_from_stale_candidate_line() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("a.rs");
        std::fs::write(&path, "fn root() {}\n\nfn decoy() {}\n").unwrap();

        let (result, hints) = unique_hit(
            "root",
            "symbol",
            &path,
            3,
            None,
            None,
            tmp.path(),
            None,
            &OutlineCache::new(),
        )
        .expect("fresh symbol resolution must replace the stale candidate line");

        assert_eq!(result["core"], "fn root() {}");
        assert_eq!(result["target"]["line"], 1);
        assert_eq!(result["target"]["name"], "root");
        assert_eq!(hints.len(), 5);
    }

    #[test]
    fn unreadable_ambiguous_discovery_reports_partial() {
        let tmp = tempfile::tempdir().unwrap();
        for name in ["a.rs", "b.rs"] {
            std::fs::write(tmp.path().join(name), "fn root() {}\n").unwrap();
        }
        std::fs::write(tmp.path().join("unreadable.rs"), [0xff, 0xfe]).unwrap();
        let result = call(&json!({"cwd": tmp.path(), "queries": [{"query": "root"}]})).unwrap();
        assert_eq!(result["results"][0]["resolved_as"], "ambiguous");
        assert_eq!(result["results"][0]["status"], "ambiguous");
        assert_eq!(result["results"][0]["completeness"], "partial");
        assert_eq!(
            result["results"][0]["candidates"].as_array().unwrap().len(),
            2
        );
    }

    #[test]
    fn capped_ambiguous_candidates_and_preview_report_partial() {
        for (definitions, references) in [(20, 0), (2, 30)] {
            let tmp = tempfile::tempdir().unwrap();
            for index in 0..definitions {
                std::fs::write(
                    tmp.path().join(format!("definition_{index}.rs")),
                    "fn root() {}\n",
                )
                .unwrap();
            }
            std::fs::write(
                tmp.path().join("references.md"),
                "Call root here.\n".repeat(references),
            )
            .unwrap();
            let (result, telemetry_dir) =
                call_with_telemetry(&json!({"cwd": tmp.path(), "queries": [{"query": "root"}]}))
                    .unwrap();
            assert_eq!(result["results"][0]["resolved_as"], "ambiguous");
            assert_eq!(result["results"][0]["status"], "ambiguous");
            assert_eq!(result["results"][0]["completeness"], "partial");
            assert_eq!(
                result["results"][0]["candidates"].as_array().unwrap().len(),
                definitions.min(10)
            );
            let record: Value = serde_json::from_str(
                std::fs::read_to_string(telemetry_dir.path().join("current.jsonl"))
                    .unwrap()
                    .trim(),
            )
            .unwrap();
            assert_eq!(record["partial"], true);
        }
    }

    #[test]
    fn bounded_sections_and_budget_preserve_partial_metadata() {
        let tmp = tempfile::tempdir().unwrap();
        let mut content = "fn root() {}\n".to_string();
        for index in 0..40 {
            use std::fmt::Write as _;
            writeln!(content, "fn sibling_{index}() {{}}").unwrap();
        }
        std::fs::write(tmp.path().join("a.rs"), &content).unwrap();
        let initial = call(&json!({"cwd": tmp.path(), "queries": [{"query": "root"}]})).unwrap();
        let hint = initial["hints"]
            .as_array()
            .unwrap()
            .iter()
            .find(|h| h["kind"] == "fetch_siblings")
            .unwrap();
        let result = call(&json!({"cwd": tmp.path(), "queries": [{"follow": hint}]})).unwrap();
        assert_eq!(result["results"][0]["status"], "partial");
        assert_eq!(result["results"][0]["completeness"], "partial");
        assert_eq!(result["results"][0]["items"].as_array().unwrap().len(), 30);
        assert_eq!(
            result["hints"].as_array().unwrap().as_slice(),
            [] as [serde_json::Value; 0]
        );
        let mut response = json!({"results": [
            {"query": "one", "resolved_as": "literal", "status": "ok", "completeness": "complete", "preview": "\\\"".repeat(100_000)},
            {"query": "two", "resolved_as": "literal", "status": "ok", "completeness": "complete", "preview": "second"}
        ], "hints": [], "diagnostics": {}});
        for budget in [crate::budget::DEFAULT_BUDGET, 150] {
            let (output, _) = reduce(&response, budget).unwrap();
            assert!(crate::types::estimate_tokens(output.len() as u64) <= budget);
            let parsed: Value = serde_json::from_str(&output).unwrap();
            assert_eq!(parsed["results"].as_array().unwrap().len(), 2);
            assert_eq!(parsed["results"][0]["status"], "partial");
            assert_eq!(parsed["results"][0]["budget_limited"], true);
        }
        response["results"][0]
            .as_object_mut()
            .unwrap()
            .remove("preview");
        response["results"][0]["dependency_impact"] = json!({
            "coverage": "complete", "imports": ["x".repeat(10000)], "dependents": [], "total_imports": 1
        });
        let (output, _) = reduce(&response, 200).unwrap();
        let parsed: Value = serde_json::from_str(&output).unwrap();
        assert_eq!(
            parsed["results"][0]["dependency_impact"]["coverage"],
            "partial"
        );
    }

    #[test]
    fn budget_trim_keeps_ambiguous_status() {
        let response = json!({"results": [{"query": "root", "resolved_as": "ambiguous",
            "status": "ambiguous", "completeness": "complete",
            "candidates": [{"path": "x".repeat(5000)}]}], "hints": [], "diagnostics": {}});
        let (output, _) = reduce(&response, 100).unwrap();
        let parsed: Value = serde_json::from_str(&output).unwrap();
        assert_eq!(parsed["results"][0]["status"], "ambiguous");
        assert_eq!(parsed["results"][0]["completeness"], "partial");
        assert_eq!(parsed["results"][0]["budget_limited"], true);
    }

    #[test]
    fn structural_requests_reuse_documents_and_compile_once_per_language() {
        let tmp = tempfile::tempdir().unwrap();
        let source = "structural_witness_unique(value)\n";
        let witness = crate::lang::treesitter::ParseWitness::new(source);
        let first = tmp.path().join("first.py");
        std::fs::write(&first, source).unwrap();
        std::fs::write(tmp.path().join("second.py"), source).unwrap();
        std::fs::write(
            tmp.path().join("source.ts"),
            "structural_witness_unique(value);\n",
        )
        .unwrap();
        let (cache, session, bloom) = components();
        let (telemetry, telemetry_dir) = telemetry();
        let args = json!({"cwd": tmp.path(), "budget": 10000, "queries": [
            {"pattern": "structural_witness_unique($A)", "language": "python"},
            {"pattern": "structural_witness_unique($A)", "language": "python"},
            {"pattern": "structural_witness_unique($A)", "language": "typescript"}
        ]});
        let before = crate::search::compilation_count();
        for request in 1..=2 {
            let response =
                tool_search_v2(&args, &cache, &session, &bloom, &telemetry, "test", "test")
                    .unwrap();
            let response: Value = serde_json::from_str(&response).unwrap();
            assert_eq!(response["results"][0]["items"].as_array().unwrap().len(), 2);
            assert_eq!(response["results"][0], response["results"][1]);
            assert_eq!(response["results"][2]["items"].as_array().unwrap().len(), 1);
            assert_eq!(
                witness.count(),
                2,
                "candidate files parse once across requests"
            );
            assert_eq!(crate::search::compilation_count() - before, request * 2);
        }
        assert_eq!(session.search_count(), 6);
        assert!(
            !session.summary().contains("structural_witness_unique($A)"),
            "structural patterns must not leak into the session summary"
        );
        let records: Vec<Value> =
            std::fs::read_to_string(telemetry_dir.path().join("current.jsonl"))
                .expect("telemetry file written")
                .lines()
                .map(|line| serde_json::from_str(line).expect("valid telemetry record"))
                .collect();
        assert_eq!(records.len(), 2);
        assert_eq!(records[0]["first_call"], true);
        assert_eq!(records[1]["first_call"], false);

        let retained = cache.get_or_parse(&first).unwrap();
        let again = cache.get_or_parse(&first).unwrap();
        assert!(std::sync::Arc::ptr_eq(&retained, &again));
    }

    #[test]
    fn secret_files_never_emit_source_in_core() {
        let tmp = tempfile::tempdir().unwrap();
        std::fs::write(
            tmp.path().join("credentials.py"),
            "def token():\n    return \"hunter2\"\n",
        )
        .unwrap();
        assert!(crate::lang::detection::is_secret_file("credentials.py"));
        let notice = crate::search::SECRET_REDACTION_NOTICE.trim_start();
        for query in ["credentials.py", "token"] {
            let response =
                call(&json!({"cwd": tmp.path(), "queries": [{"query": query}]})).unwrap();
            assert_eq!(
                response["results"][0]["core"], notice,
                "{query}: {response}"
            );
            assert!(!response.to_string().contains("hunter2"), "{response}");
        }
    }

    #[test]
    fn file_query_hints_respect_glob_and_long_core_is_partial() {
        let tmp = tempfile::tempdir().unwrap();
        std::fs::write(
            tmp.path().join("a.rs"),
            format!("fn root() {{\n{}}}\n", " // line\n".repeat(80)),
        )
        .unwrap();
        let excluded =
            call(&json!({"cwd": tmp.path(), "queries": [{"query": "a.rs", "glob": "*.ts"}]}))
                .unwrap();
        assert_eq!(excluded["results"][0]["status"], "no_match");
        assert_eq!(
            excluded["hints"].as_array().unwrap().as_slice(),
            [] as [serde_json::Value; 0]
        );
        let initial = call(&json!({"cwd": tmp.path(), "queries": [{"query": "a.rs"}]})).unwrap();
        assert_eq!(initial["hints"].as_array().unwrap().len(), 1);
        let followed =
            call(&json!({"cwd": tmp.path(), "queries": [{"follow": initial["hints"][0]}]}))
                .unwrap();
        assert_eq!(followed["results"][0]["resolved_as"], "fetch_dependencies");
    }

    fn repo_root() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
    }

    fn telemetry() -> (TelemetrySink, tempfile::TempDir) {
        let tmp = tempfile::tempdir().expect("tempdir");
        let sink = TelemetrySink::for_test(tmp.path());
        (sink, tmp)
    }

    fn components() -> (OutlineCache, Session, BloomFilterCache) {
        (OutlineCache::new(), Session::new(), BloomFilterCache::new())
    }

    fn call_with_telemetry(args: &Value) -> Result<(Value, tempfile::TempDir), String> {
        let (cache, session, bloom) = components();
        let (telemetry, tmp) = telemetry();
        let out = tool_search_v2(
            args,
            &cache,
            &session,
            &bloom,
            &telemetry,
            "test-client",
            "test-worktree",
        )?;
        Ok((
            serde_json::from_str(&out).expect("valid json response"),
            tmp,
        ))
    }

    fn call(args: &Value) -> Result<Value, String> {
        call_with_telemetry(args).map(|(response, _tmp)| response)
    }

    fn single_query(query: &str) -> Result<Value, String> {
        call(&json!({
            "cwd": repo_root().to_str().unwrap(),
            "queries": [{"query": query}],
        }))
    }

    #[test]
    fn route_path_resolves_existing_file() {
        let resp = single_query("src/mcp/mod.rs").expect("path query succeeds");
        assert_eq!(resp["results"][0]["resolved_as"], "path");
    }

    #[test]
    fn route_non_code_path_reports_bounded_content_results() {
        let tmp = tempfile::tempdir().unwrap();
        std::fs::write(tmp.path().join("notes.json"), "{}\n").unwrap();
        for index in 0..11 {
            std::fs::write(
                tmp.path().join(format!("reference-{index}.txt")),
                "load notes.json\n",
            )
            .unwrap();
        }

        let resp = call(&json!({
            "cwd": tmp.path(),
            "queries": [{"query": "notes.json"}],
        }))
        .expect("path query succeeds");
        let result = &resp["results"][0];
        assert_eq!(result["resolved_as"], "path");
        assert_eq!(result["status"], "partial");
        assert_eq!(result["completeness"], "partial");
        assert!(result["total_found"].as_u64().unwrap() > 10);
    }

    #[test]
    fn route_filename_shaped_miss_suggests_fuzzy_path_candidates() {
        let resp = single_query("harness.py").expect("filename-shaped query succeeds");
        let result = &resp["results"][0];
        assert_eq!(result["resolved_as"], "path");
        assert_eq!(result["status"], "ambiguous");
        let candidates = result["candidates"].as_array().expect("candidates array");
        assert!(
            candidates
                .iter()
                .any(|c| c["path"] == "tests/mcp_v2/harness.py"),
            "candidates must include tests/mcp_v2/harness.py: {candidates:?}"
        );
    }

    #[test]
    fn route_symbol_resolves_unique_definition() {
        let resp = single_query("detect_file_type").expect("symbol query succeeds");
        assert_eq!(resp["results"][0]["resolved_as"], "symbol");
    }

    #[test]
    fn usage_only_identifier_in_exact_file_falls_back_to_literal() {
        let (resp, tmp) = call_with_telemetry(&json!({
            "cwd": repo_root().to_str().unwrap(),
            "queries": [{
                "query": "SearchTelemetryRecord",
                "glob": "src/mcp/tools/search_v2.rs",
            }],
        }))
        .expect("usage query succeeds");
        let result = &resp["results"][0];

        assert_eq!(result["resolved_as"], "literal");
        assert_eq!(result["status"], "ok");
        let preview = result["preview"].as_str().expect("literal preview");
        assert!(
            preview.contains("src/mcp/tools/search_v2.rs:"),
            "literal fallback must stay within the exact file: {preview}"
        );
        assert!(
            preview.contains("use crate::telemetry::{SearchTelemetryRecord, TelemetrySink};"),
            "literal fallback must return the exact source usage: {preview}"
        );
        assert!(
            !preview.contains("src/telemetry.rs:"),
            "literal fallback must exclude the external definition: {preview}"
        );

        let telemetry_log = std::fs::read_to_string(tmp.path().join("current.jsonl"))
            .expect("literal route telemetry should be persisted");
        let record_line = telemetry_log
            .lines()
            .next()
            .expect("telemetry log should contain one record");
        let record: Value = serde_json::from_str(record_line).expect("valid telemetry record");
        assert_eq!(record["route"], "literal");
        assert_eq!(record["routes_tried"], json!(["literal"]));
    }

    #[test]
    fn embedded_identifier_in_larger_token_falls_back_to_literal() {
        let query = ["Telemetry", "Record"].concat();
        let resp = call(&json!({
            "cwd": repo_root().to_str().unwrap(),
            "queries": [{
                "query": query,
                "glob": "src/mcp/tools/search_v2.rs",
            }],
        }))
        .expect("embedded identifier query succeeds");
        let result = &resp["results"][0];

        assert_eq!(result["resolved_as"], "literal");
        assert_eq!(result["status"], "ok");
        let preview = result["preview"].as_str().expect("literal preview");
        assert!(
            preview.contains("src/mcp/tools/search_v2.rs:"),
            "literal fallback must return the exact file: {preview}"
        );
        assert!(
            preview.contains("use crate::telemetry::{SearchTelemetryRecord, TelemetrySink};"),
            "literal fallback must return embedded content: {preview}"
        );
        assert!(
            !preview.contains("src/telemetry.rs:"),
            "exact-file literal fallback must exclude external content: {preview}"
        );
    }

    #[test]
    fn route_literal_resolves_multi_word_content() {
        let resp =
            single_query("DO NOT re-read expanded search content").expect("literal query succeeds");
        assert_eq!(resp["results"][0]["resolved_as"], "literal");
    }

    #[test]
    fn route_regex_resolves_metachar_pattern() {
        let resp = single_query(r"fn\s+detect_file_type").expect("regex query succeeds");
        assert_eq!(resp["results"][0]["resolved_as"], "regex");
    }

    #[test]
    fn regex_with_exact_file_glob_is_bounded_to_that_file() {
        let start = Instant::now();
        let resp = call(&json!({
            "cwd": repo_root().to_str().unwrap(),
            "queries": [{
                "query": r"SearchTelemetryRecord.*",
                "glob": "src/mcp/tools/search_v2.rs",
            }],
        }))
        .expect("bounded regex query succeeds");
        assert!(
            start.elapsed() < std::time::Duration::from_secs(2),
            "exact-file-glob regex must be single-file-bounded and fast: {:?}",
            start.elapsed()
        );
        let result = &resp["results"][0];
        assert_eq!(result["resolved_as"], "regex");
        let preview = result["preview"].as_str().expect("regex preview");
        assert!(
            preview.contains("src/mcp/tools/search_v2.rs:"),
            "regex preview must include the exact-glob file: {preview}"
        );
        assert!(
            !preview.contains("src/telemetry.rs:"),
            "regex preview must exclude files outside the exact glob: {preview}"
        );
    }

    #[test]
    fn route_ambiguous_resolves_multi_definition_identifier() {
        let resp = single_query("run").expect("ambiguous query succeeds");
        assert_eq!(resp["results"][0]["resolved_as"], "ambiguous");
    }

    #[test]
    fn same_line_same_name_definitions_remain_ambiguous() {
        let tmp = tempfile::tempdir().unwrap();
        std::fs::write(
            tmp.path().join("nested.js"),
            "function run() { function run() {}\n  return 1;\n}\n",
        )
        .unwrap();

        let response = call(&json!({"cwd": tmp.path(), "queries": [{"query": "run"}]})).unwrap();
        let result = &response["results"][0];
        assert_eq!(result["resolved_as"], "ambiguous");
        assert_eq!(result["status"], "ambiguous");
        let candidates = result["candidates"].as_array().unwrap();
        assert_eq!(candidates.len(), 2);
        assert!(candidates
            .iter()
            .all(|candidate| candidate["occurrence"].is_array()));
        assert_ne!(candidates[0]["occurrence"], candidates[1]["occurrence"]);
    }

    /// Built at runtime (not a single source literal) so the query itself
    /// never appears as a contiguous match in this very test file.
    fn absent_query() -> String {
        ["tilth_absent_probe_", "91c6a4"].concat()
    }

    #[test]
    fn route_miss_resolves_absent_query() {
        let resp = single_query(&absent_query()).expect("miss query succeeds");
        assert_eq!(resp["results"][0]["resolved_as"], "miss");
        assert_eq!(resp["results"][0]["status"], "no_match");
    }

    #[test]
    fn batch_of_three_preserves_order_and_yields_one_record_each() {
        let resp = call(&json!({
            "cwd": repo_root().to_str().unwrap(),
            "queries": [
                {"query": "src/mcp/mod.rs"},
                {"query": "detect_file_type"},
                {"query": absent_query()},
            ],
        }))
        .expect("batch query succeeds");
        let results = resp["results"].as_array().expect("results array");
        assert_eq!(results.len(), 3);
        assert_eq!(results[0]["query"], "src/mcp/mod.rs");
        assert_eq!(results[0]["resolved_as"], "path");
        assert_eq!(results[1]["query"], "detect_file_type");
        assert_eq!(results[1]["resolved_as"], "symbol");
        assert_eq!(results[2]["query"], absent_query());
        assert_eq!(results[2]["resolved_as"], "miss");
        assert_eq!(results[2]["status"], "no_match");
    }

    #[test]
    fn empty_batch_is_refused() {
        let err = call(&json!({
            "cwd": repo_root().to_str().unwrap(),
            "queries": [],
        }))
        .unwrap_err();
        assert!(err.contains("1-10"), "empty batch must be refused: {err}");
    }

    #[test]
    fn oversized_batch_is_refused() {
        let queries: Vec<Value> = (0..11).map(|i| json!({"query": format!("q{i}")})).collect();
        let err = call(&json!({
            "cwd": repo_root().to_str().unwrap(),
            "queries": queries,
        }))
        .unwrap_err();
        assert!(
            err.contains("1-10"),
            "11-entry batch must be refused: {err}"
        );
    }

    #[test]
    fn unique_symbol_hit_carries_core_and_complete_dependency_impact_and_hints() {
        let resp = single_query("detect_file_type").expect("symbol query succeeds");
        let result = &resp["results"][0];
        assert!(result["core"].is_string(), "unique hit must carry core");
        assert_eq!(result["dependency_impact"]["coverage"], "complete");

        let hint_kinds: Vec<&str> = resp["hints"]
            .as_array()
            .expect("hints array")
            .iter()
            .map(|h| h["kind"].as_str().expect("kind is a string"))
            .collect();
        for expected in [
            "fetch_callers",
            "fetch_callees",
            "fetch_siblings",
            "fetch_tests",
            "fetch_dependencies",
        ] {
            assert!(
                hint_kinds.contains(&expected),
                "missing hint kind {expected}: {hint_kinds:?}"
            );
        }
    }

    #[test]
    fn ambiguous_hit_carries_multiple_candidates_no_core_and_disambiguate_hint() {
        let resp = single_query("run").expect("ambiguous query succeeds");
        let result = &resp["results"][0];
        assert!(
            result["core"].is_null(),
            "ambiguous hit must not carry core"
        );
        let candidates = result["candidates"].as_array().expect("candidates array");
        assert!(candidates.len() > 1, "ambiguous must have >1 candidates");

        let hint_kinds: Vec<&str> = resp["hints"]
            .as_array()
            .expect("hints array")
            .iter()
            .map(|h| h["kind"].as_str().expect("kind is a string"))
            .collect();
        assert_eq!(hint_kinds, [] as [&str; 0]);
    }

    #[test]
    fn markdown_heading_collision_still_resolves_unique_code_symbol() {
        let tmp = tempfile::tempdir().expect("tempdir");
        std::fs::write(
            tmp.path().join("notes.md"),
            "# tilth_probe_widget\n\nSome docs.\n",
        )
        .expect("write markdown");
        std::fs::write(tmp.path().join("widget.rs"), "fn tilth_probe_widget() {}\n")
            .expect("write rust");

        let resp = call(&json!({
            "cwd": tmp.path().to_str().unwrap(),
            "queries": [{"query": "tilth_probe_widget"}],
        }))
        .expect("symbol query succeeds");
        assert_eq!(resp["results"][0]["resolved_as"], "symbol");
    }

    #[test]
    fn two_code_definitions_and_content_hits_yield_ambiguous_with_preview() {
        let tmp = tempfile::tempdir().expect("tempdir");
        std::fs::write(tmp.path().join("a.rs"), "fn tilth_probe_dupe() {}\n").expect("write a.rs");
        std::fs::write(tmp.path().join("b.rs"), "fn tilth_probe_dupe() {}\n").expect("write b.rs");
        std::fs::write(tmp.path().join("c.rs"), "// calls tilth_probe_dupe here\n")
            .expect("write c.rs");

        let resp = call(&json!({
            "cwd": tmp.path().to_str().unwrap(),
            "queries": [{"query": "tilth_probe_dupe"}],
        }))
        .expect("ambiguous query succeeds");
        let result = &resp["results"][0];
        assert_eq!(result["resolved_as"], "ambiguous");
        let candidates = result["candidates"].as_array().expect("candidates array");
        assert_eq!(candidates.len(), 2, "candidates: {candidates:?}");
        let preview = result["preview"].as_str().expect("preview must be present");
        assert!(!preview.is_empty(), "preview must not be empty");
    }

    #[test]
    fn response_never_contains_routes_tried() {
        let (cache, session, bloom) = components();
        let (telemetry, _tmp) = telemetry();
        let args = json!({
            "cwd": repo_root().to_str().unwrap(),
            "queries": [{"query": "detect_file_type"}, {"query": "run"}],
        });
        let out = tool_search_v2(
            &args,
            &cache,
            &session,
            &bloom,
            &telemetry,
            "test-client",
            "test-worktree",
        )
        .expect("batch query succeeds");
        assert!(
            !out.contains("routes_tried"),
            "response must never carry routes_tried: {out}"
        );
    }

    #[test]
    fn signature_prefix_normalizes_to_symbol_and_emits_diagnostic() {
        let resp = single_query("fn detect_file_type").expect("signature-prefix query succeeds");
        let result = &resp["results"][0];
        assert_eq!(result["resolved_as"], "symbol");
        assert_eq!(result["query"], "fn detect_file_type");

        let normalizations = resp["diagnostics"]["normalizations"]
            .as_array()
            .expect("normalizations array");
        assert!(
            normalizations.iter().any(|n| {
                n["query"] == "fn detect_file_type"
                    && n["normalized_to"] == "detect_file_type"
                    && n["stripped_keyword"] == "fn"
            }),
            "missing normalization diagnostic: {normalizations:?}"
        );
    }

    #[test]
    fn caller_selected_kind_is_rejected() {
        let err = call(&json!({
            "cwd": repo_root().to_str().unwrap(),
            "queries": [{"query": "detect_file_type", "kind": "callers"}],
        }))
        .expect_err("caller-selected kind must be rejected");
        assert!(err.contains("query and glob"), "unexpected error: {err}");
    }
    /// Build the shared fixture: a git-init tempdir with `fixture.ts` (root,
    /// `production_caller`, sibling), `helper.ts` (leaf), `fixture.test.ts`
    /// (`test_root`), and `other.ts` (a same-named root whose caller must
    /// never surface), then run the initial `root` query. Returns the
    /// tempdir (kept alive for later calls) and the initial response with
    /// its 5 hints.
    fn continuation_fixture() -> (tempfile::TempDir, Value) {
        let tmp = tempfile::tempdir().expect("tempdir");
        assert!(std::process::Command::new("git")
            .args(["init", "-q"])
            .current_dir(tmp.path())
            .status()
            .unwrap()
            .success());
        std::fs::write(tmp.path().join("fixture.ts"),
            "import { leaf } from './helper';\nexport function root() { leaf(); }\nfunction production_caller() { root(); }\nfunction sibling() {}\n").unwrap();
        std::fs::write(tmp.path().join("helper.ts"), "export function leaf() {}\n").unwrap();
        std::fs::write(
            tmp.path().join("fixture.test.ts"),
            "import { root } from './fixture';\nfunction test_root() { root(); }\n",
        )
        .unwrap();
        std::fs::write(
            tmp.path().join("other.ts"),
            "function root() {}\nfunction wrong_caller() { root(); }\n",
        )
        .unwrap();
        let cwd = tmp.path().to_str().unwrap();
        let initial = call(&json!({"cwd": cwd, "queries": [{"query": "root",
            "glob": "{fixture.ts,fixture.test.ts,helper.ts}"}]}))
        .unwrap();
        (tmp, initial)
    }

    /// The identity each hint kind must surface when followed.
    fn expected_identity_for_kind(kind: &str) -> &'static str {
        match kind {
            "fetch_callers" => "production_caller",
            "fetch_callees" => "leaf",
            "fetch_siblings" => "sibling",
            "fetch_tests" => "test_root",
            "fetch_dependencies" => "helper.ts",
            _ => unreachable!(),
        }
    }

    /// Flatten a followed result's identities: `dependency_impact` arrays for
    /// `fetch_dependencies`, `items[].name`/`items[].path` otherwise.
    fn identities_from_result(hint: &Value, result: &Value) -> Vec<String> {
        if hint["kind"] == "fetch_dependencies" {
            let impact = &result["dependency_impact"];
            impact["imports"]
                .as_array()
                .unwrap()
                .iter()
                .chain(impact["dependents"].as_array().unwrap())
                .map(|v| v.as_str().unwrap().to_string())
                .collect()
        } else {
            result["items"]
                .as_array()
                .unwrap()
                .iter()
                .flat_map(|item| [item.get("name"), item.get("path")])
                .flatten()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        }
    }

    #[test]
    fn hint_echo_round_trip_returns_expected_identity_for_every_kind() {
        let (tmp, initial) = continuation_fixture();
        let cwd = tmp.path().to_str().unwrap();
        let hints = initial["hints"].as_array().unwrap();
        assert_eq!(hints.len(), 5, "{initial}");
        for hint in hints {
            let followed = call(&json!({"cwd": cwd, "queries": [{"follow": hint}]})).unwrap();
            let result = &followed["results"][0];
            let expected = expected_identity_for_kind(hint["kind"].as_str().unwrap());
            assert_eq!(result["status"], "ok", "{followed}");
            let identities = identities_from_result(hint, result);
            assert!(identities.iter().any(|s| s == expected), "{followed}");
            assert!(
                !identities.iter().any(|s| s == "wrong_caller"),
                "{followed}"
            );
            assert_eq!(
                followed["hints"].as_array().unwrap().as_slice(),
                [] as [serde_json::Value; 0]
            );
        }
        assert_eq!(hints[0]["target"]["line"], 2);
        assert_eq!(hints[0]["target"]["path"], "fixture.ts");
    }

    #[test]
    fn followed_result_has_canonical_payload_without_preview() {
        let (tmp, initial) = continuation_fixture();
        let cwd = tmp.path().to_str().unwrap();
        let hints = initial["hints"].as_array().unwrap();
        for hint in hints {
            let followed = call(&json!({"cwd": cwd, "queries": [{"follow": hint}]})).unwrap();
            let result = &followed["results"][0];
            // The duplicate preview payload is gone; identities live only in
            // the canonical `items`/`dependency_impact` arrays (#238).
            assert!(
                result.get("preview").is_none(),
                "follow result must not carry preview: {followed}"
            );
            if hint["kind"] == "fetch_dependencies" {
                let impact = &result["dependency_impact"];
                assert!(impact["imports"].is_array(), "{followed}");
                assert!(impact["dependents"].is_array(), "{followed}");
                let identities = identities_from_result(hint, result);
                assert!(
                    identities
                        .iter()
                        .any(|s| s == expected_identity_for_kind("fetch_dependencies")),
                    "{followed}"
                );
            } else {
                let items = result["items"].as_array().unwrap();
                assert_eq!(
                    result["total_found"].as_u64().unwrap() as usize,
                    items.len(),
                    "{followed}"
                );
            }
        }
    }

    #[test]
    fn mixed_query_and_follow_entries_preserve_order() {
        let (tmp, initial) = continuation_fixture();
        let cwd = tmp.path().to_str().unwrap();
        let hints = initial["hints"].as_array().unwrap();
        let mixed = call(&json!({"cwd": cwd, "queries": [
            {"query": "^absent$"}, {"follow": hints[0]}, {"query": "root"}
        ]}))
        .unwrap();
        assert_eq!(mixed["results"][0]["status"], "no_match");
        assert_eq!(mixed["results"][1]["resolved_as"], "fetch_callers");
        assert_eq!(mixed["results"][2]["status"], "ambiguous");
    }

    #[test]
    fn malformed_follow_target_is_rejected() {
        let (tmp, initial) = continuation_fixture();
        let cwd = tmp.path().to_str().unwrap();
        let hints = initial["hints"].as_array().unwrap();
        for (key, value) in [
            ("name", json!("wrong")),
            ("line", json!(0)),
            ("line", json!(3)),
            ("scope", json!("/")),
            ("glob", json!(7)),
            ("glob", json!("other.rs")),
            ("path", json!("../fixture.rs")),
        ] {
            let mut bad = hints[0].clone();
            bad["target"][key] = value;
            assert!(
                call(&json!({"cwd": cwd, "queries": [{"follow": bad}]})).is_err(),
                "{bad}"
            );
        }
    }

    #[test]
    fn continuation_rejects_mixed_entries_and_unknown_kinds() {
        let root = repo_root();
        let cwd = root.to_str().unwrap();
        let mixed = call(&json!({
            "cwd": cwd,
            "queries": [{"query": "detect_file_type", "follow": {}}],
        }))
        .expect_err("mixed query/follow must fail");
        assert!(mixed.contains("exactly one"), "{mixed}");
        let unknown = call(&json!({
            "cwd": cwd,
            "queries": [{"follow": {"kind": "fetch_unknown", "target": {}}}],
        }))
        .expect_err("unknown continuation must fail");
        assert!(unknown.contains("unknown continuation"), "{unknown}");
    }

    #[test]
    fn no_match_is_explicit_and_disambiguation_has_no_unexecutable_hint() {
        let miss = single_query(&absent_query()).expect("miss query");
        assert_eq!(miss["results"][0]["status"], "no_match");
        let ambiguous = single_query("run").expect("ambiguous query");
        assert_eq!(
            ambiguous["hints"].as_array().expect("hints").as_slice(),
            [] as [serde_json::Value; 0]
        );
    }

    #[test]
    fn budget_limited_search_remains_valid_json() {
        let response = call(&json!({
            "cwd": repo_root().to_str().unwrap(),
            "queries": [{"query": "detect_file_type"}],
            "budget": 1,
        }))
        .expect_err("required metadata cannot fit one token");
        assert!(response.contains("budget"), "{response}");
    }

    #[test]
    fn incomplete_index_and_budget_never_claim_complete() {
        let tmp = tempfile::tempdir().unwrap();
        std::fs::write(tmp.path().join("root.rs"), "fn root() {}\n").unwrap();
        let args = json!({"cwd": tmp.path(), "queries": [{"query": "root"}]});
        let response = call(&args).unwrap();
        assert_eq!(
            response["results"][0]["dependency_impact"]["coverage"],
            "partial"
        );
        assert_eq!(response["results"][0]["status"], "partial");
        let mut small_args = args.clone();
        small_args["budget"] = json!(450);
        let small = call(&small_args);
        if let Ok(small) = small {
            assert!(crate::types::estimate_tokens(small.to_string().len() as u64) <= 450);
            assert_eq!(small["results"][0]["status"], "partial");
        }
    }

    #[test]
    fn regex_absence_and_noncode_references_have_exact_status() {
        let tmp = tempfile::tempdir().unwrap();
        std::fs::write(tmp.path().join("uv.lock"), "not the reference\n").unwrap();
        let response = call(&json!({"cwd": tmp.path(), "queries": [
            {"query": "uv.lock"}, {"query": "^absent$"}
        ]}))
        .unwrap();
        assert_eq!(response["results"][0]["status"], "no_match");
        assert_eq!(response["results"][1]["status"], "no_match");
        std::fs::write(tmp.path().join("notes.md"), "Use uv.lock for versions.\n").unwrap();
        let response =
            call(&json!({"cwd": tmp.path(), "queries": [{"query": "uv.lock"}]})).unwrap();
        assert_eq!(response["results"][0]["status"], "ok");
        assert!(response["results"][0]["preview"]
            .as_str()
            .unwrap()
            .contains("Use uv.lock"));
    }

    #[test]
    fn malformed_entry_fields_are_rejected() {
        let tmp = tempfile::tempdir().unwrap();
        for entry in [
            json!({"query": "root", "glob": 7}),
            json!({"follow": {}, "glob": "*.rs"}),
            json!({"query": "root", "expand": 1}),
            json!({"query": "root", "context": "src"}),
        ] {
            assert!(call(&json!({"cwd": tmp.path(), "queries": [entry]})).is_err());
        }
    }

    #[test]
    fn validation_and_execution_errors_emit_one_content_free_record() {
        let fixture = tempfile::tempdir().unwrap();
        let root = fixture.path().display().to_string();
        std::fs::write(fixture.path().join("invalid_utf8.rs"), [0xff]).unwrap();
        let secret = "telemetry-secret-7f2e";
        let oversized: Vec<Value> = (0..11).map(|_| json!({"query": secret})).collect();
        let cases = vec![
            (
                "nonobject",
                json!(secret),
                "search arguments must be an object",
                "bad_object",
            ),
            (
                "missing cwd",
                json!({"queries": [{"query": secret}]}),
                "missing required parameter \"cwd\"",
                "bad_cwd",
            ),
            (
                "relative cwd",
                json!({"cwd": ".", "queries": [{"query": secret}]}),
                "is relative",
                "bad_cwd",
            ),
            (
                "unknown key",
                json!({"cwd": root, "queries": [{"query": secret}], "extra": true}),
                "only queries, cwd, and budget",
                "unknown_keys",
            ),
            (
                "bad budget",
                json!({"cwd": root, "queries": [{"query": secret}], "budget": 0}),
                "budget must be a positive integer",
                "bad_budget",
            ),
            (
                "missing queries",
                json!({"cwd": root}),
                "missing queries",
                "missing_queries",
            ),
            (
                "empty queries",
                json!({"cwd": root, "queries": []}),
                "queries must contain 1-10 entries; got 0",
                "empty_queries",
            ),
            (
                "oversized queries",
                json!({"cwd": root, "queries": oversized}),
                "queries must contain 1-10 entries; got 11",
                "oversized_queries",
            ),
            (
                "nonobject entry",
                json!({"cwd": root, "queries": [secret]}),
                "each entry must be an object",
                "bad_query_entry",
            ),
            (
                "nonstring query",
                json!({"cwd": root, "queries": [{"query": 7}]}),
                "query must be a string",
                "bad_query_entry",
            ),
            (
                "nonstring glob",
                json!({"cwd": root, "queries": [{"query": secret, "glob": 7}]}),
                "glob must be a string",
                "bad_query_entry",
            ),
            (
                "invalid follow",
                json!({"cwd": root, "queries": [{"follow": {"kind": "fetch_unknown", "target": {}}}]}),
                "unknown continuation",
                "bad_follow",
            ),
            (
                "route error",
                json!({"cwd": root, "queries": [{"query": "invalid_utf8.rs"}]}),
                "UTF-8",
                "route_error",
            ),
            (
                "budget exhaustion",
                json!({"cwd": root, "queries": [{"query": secret}], "budget": 1}),
                "budget 1 cannot fit required search metadata",
                "budget_error",
            ),
        ];

        for (name, args, expected_error, expected_class) in cases {
            let (cache, session, bloom) = components();
            let (telemetry, sink) = telemetry();
            let error = tool_search_v2(
                &args,
                &cache,
                &session,
                &bloom,
                &telemetry,
                "test-client",
                "test-worktree",
            )
            .expect_err(name);
            assert!(
                error.contains(expected_error),
                "{name}: expected {expected_error:?}, got {error:?}"
            );

            let log = std::fs::read_to_string(sink.path().join("current.jsonl"))
                .expect("telemetry file written");
            let lines: Vec<&str> = log.lines().collect();
            assert_eq!(lines.len(), 1, "{name}: exactly one telemetry record");
            let record: Value = serde_json::from_str(lines[0]).expect("valid telemetry record");
            assert_eq!(record["verb"], "tilth_search", "{name}");
            assert_eq!(
                record["version"],
                crate::telemetry::SCHEMA_VERSION,
                "{name}"
            );
            assert_eq!(record["outcome"], "error", "{name}");
            assert_eq!(record["error_class"], expected_class, "{name}");
            assert_eq!(record["route"], expected_class, "{name}");
            assert!(record.get("query").is_none(), "{name}: query field leaked");
            assert!(!lines[0].contains(secret), "{name}: query content leaked");
        }
    }

    /// Telemetry describes the search that ran, not the trimmed envelope: a
    /// budget-trimmed batch reports `budget_limited` while `dependency_coverage`
    /// keeps its pre-trim value, `route` collapses to `batch`, and `first_call`
    /// is a session fact rather than a constant.
    #[test]
    fn telemetry_snapshots_pretrim_inputs_and_session_first_call() {
        let root = tempfile::tempdir().unwrap();
        assert!(std::process::Command::new("git")
            .args(["init", "-q"])
            .current_dir(root.path())
            .status()
            .unwrap()
            .success());
        // Keep metadata independent of the checkout path, but force body trimming.
        let source = format!(
            "fn detect_file_type() {{\n{}\n}}\n",
            "    let _value = 1;\n".repeat(300)
        );
        std::fs::write(root.path().join("fixture.rs"), source).unwrap();
        let (cache, session, bloom) = components();
        let (telemetry, sink) = telemetry();
        let args = json!({
            "cwd": root.path(),
            "queries": [{"query": "detect_file_type"}, {"query": "detect_file_type"}],
            "budget": 900,
        });
        let mut output = String::new();
        for _ in 0..2 {
            output = tool_search_v2(
                &args,
                &cache,
                &session,
                &bloom,
                &telemetry,
                "test-client",
                "test-worktree",
            )
            .expect("budget fits the required metadata");
        }
        let emitted: Value = serde_json::from_str(&output).expect("valid json response");
        let trimmed = emitted["results"]
            .as_array()
            .unwrap()
            .iter()
            .any(|r| r["budget_limited"] == true);
        assert!(trimmed, "budget must have forced a trim: {output}");

        let records: Vec<Value> = std::fs::read_to_string(sink.path().join("current.jsonl"))
            .expect("telemetry file written")
            .lines()
            .map(|line| serde_json::from_str(line).expect("valid record"))
            .collect();
        assert_eq!(records.len(), 2);
        for record in &records {
            assert_eq!(record["outcome"], "ok");
            assert_eq!(record["error_class"], Value::Null);
            assert_eq!(record["version"], crate::telemetry::SCHEMA_VERSION);
        }
        assert_eq!(records[0]["first_call"], true);
        assert_eq!(records[1]["first_call"], false);
        assert_eq!(records[0]["route"], "batch");
        assert_eq!(records[0]["routes_tried"], json!(["symbol", "symbol"]));
        assert_eq!(records[0]["budget_limited"], true);
        // Pre-trim coverage: both hits resolved complete dependency impact even
        // though the trim downgraded the coverage in the emitted response.
        assert_eq!(records[0]["dependency_coverage"], 1.0);
    }
}
