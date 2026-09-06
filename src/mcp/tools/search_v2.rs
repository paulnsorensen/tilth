//! F11 cold-partial verified-only search-v2 engine: deterministic query
//! routing (path -> regex -> symbol/ambiguous -> literal -> miss) with bounded
//! grok/deps enrichment on unique hits (see
//! `.hallouminate/wiki/adr/tilth-search-v2-trial.md`).

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
    let cwd = require_cwd(args)?;
    let object = args
        .as_object()
        .ok_or("search arguments must be an object")?;
    if object
        .keys()
        .any(|k| !matches!(k.as_str(), "queries" | "cwd" | "budget"))
    {
        return Err("search accepts only queries, cwd, and budget".into());
    }
    let budget = match args.get("budget") {
        None => crate::budget::DEFAULT_BUDGET,
        Some(value) => value
            .as_u64()
            .filter(|n| *n > 0)
            .ok_or("budget must be a positive integer")?,
    };
    let entries = args
        .get("queries")
        .and_then(Value::as_array)
        .ok_or("missing queries")?;
    if entries.is_empty() || entries.len() > 10 {
        return Err(format!(
            "queries must contain 1-10 entries; got {}",
            entries.len()
        ));
    }
    // Validate the full batch before any search changes session state; each
    // follow hint is parsed once here and reused by the execution loop.
    let mut follows: Vec<Option<Follow>> = Vec::with_capacity(entries.len());
    for entry in entries {
        let object = entry.as_object().ok_or("each entry must be an object")?;
        if object.contains_key("query") == object.contains_key("follow") {
            return Err("each entry requires exactly one of query or follow".into());
        }
        if let Some(hint) = object.get("follow") {
            if object.len() != 1 {
                return Err("follow entries accept only follow".into());
            }
            follows.push(Some(Follow::parse(hint, cwd)?));
        } else {
            if object.keys().any(|k| k != "query" && k != "glob") {
                return Err("query entries accept only query and glob".into());
            }
            if !entry["query"].is_string() {
                return Err("query must be a string".into());
            }
            if entry.get("glob").is_some_and(|g| !g.is_string()) {
                return Err("glob must be a string".into());
            }
            crate::search::walker(cwd, entry.get("glob").and_then(Value::as_str))
                .map_err(|e| e.to_string())?;
            follows.push(None);
        }
    }
    let mut results = Vec::with_capacity(entries.len());
    let mut hints = Vec::new();
    let mut routes_tried = Vec::with_capacity(entries.len());
    let mut normalizations = Vec::new();
    for (entry, follow) in entries.iter().zip(&follows) {
        let (mut result, route, mut entry_hints, diagnostic) = if let Some(follow) = follow {
            let result = follow.execute(cwd, bloom, client)?;
            (result, follow.kind.clone(), Vec::new(), None)
        } else {
            route_query(
                entry["query"].as_str().unwrap(),
                entry.get("glob").and_then(Value::as_str),
                cwd,
                cache,
                session,
            )
            .map_err(|e| e.to_string())?
        };
        if result.get("target").is_some() && follow.is_none() {
            let target: Target =
                serde_json::from_value(result["target"].clone()).map_err(|e| e.to_string())?;
            result["dependency_impact"] = continuations::dependencies(&target, cwd, client)?;
            if result["dependency_impact"]["coverage"] != "complete" {
                mark_partial(&mut result);
            }
        }
        routes_tried.push(route);
        hints.append(&mut entry_hints);
        results.push(result);
        if let Some(diagnostic) = diagnostic {
            normalizations.push(diagnostic);
        }
    }
    let mut response = json!({"results": results, "hints": hints,
        "diagnostics": if normalizations.is_empty() { json!({}) } else { json!({"normalizations": normalizations}) }});
    let output = reduce_response(&mut response, budget)?;
    let results = response["results"].as_array().unwrap();
    let dependency_results: Vec<_> = results
        .iter()
        .filter_map(|r| r.get("dependency_impact"))
        .collect();
    let complete_count = dependency_results
        .iter()
        .filter(|d| d["coverage"] == "complete")
        .count();
    let _ = telemetry.record(&SearchTelemetryRecord {
        verb: "tilth_search".into(),
        version: 2,
        route: routes_tried[0].clone(),
        routes_tried,
        first_call: true,
        latency_ms: u64::try_from(start.elapsed().as_millis()).unwrap_or(u64::MAX),
        result_tokens: crate::types::estimate_tokens(output.len() as u64),
        partial: results.iter().any(|r| r["completeness"] == "partial"),
        timeout: dependency_results.iter().any(|d| d["timed_out"] == true),
        dependency_coverage: if dependency_results.is_empty() {
            1.0
        } else {
            f64::from(u32::try_from(complete_count).unwrap())
                / f64::from(u32::try_from(dependency_results.len()).unwrap())
        },
        shard_state: if dependency_results.is_empty() {
            "none"
        } else if dependency_results
            .iter()
            .any(|d| d["index_state"] == "unavailable")
        {
            "unavailable"
        } else {
            "open"
        }
        .into(),
        client: client.into(),
        worktree: worktree.into(),
    });
    Ok(output)
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

fn reduce_response(response: &mut Value, budget: u64) -> Result<String, String> {
    loop {
        let output = serde_json::to_string(response).map_err(|e| e.to_string())?;
        if crate::types::estimate_tokens(output.len() as u64) <= budget {
            return Ok(output);
        }
        // Remove the largest optional payload. Never remove an entry or alter a hint.
        let mut largest = None;
        for (index, result) in response["results"].as_array().unwrap().iter().enumerate() {
            for key in [
                "/core",
                "/preview",
                "/items",
                "/candidates",
                "/dependency_impact/imports",
                "/dependency_impact/dependents",
            ] {
                if let Some(value) = result.pointer(key) {
                    let size = value.to_string().len();
                    if largest.is_none_or(|(_, _, previous)| size > previous) {
                        largest = Some((index, key, size));
                    }
                }
            }
        }
        let Some((index, key, _)) = largest else {
            return Err(format!(
                "budget {budget} cannot fit required search metadata"
            ));
        };
        let result = &mut response["results"][index];
        let (parent, field) = key.rsplit_once('/').unwrap();
        result
            .pointer_mut(parent)
            .unwrap()
            .as_object_mut()
            .unwrap()
            .remove(field);
        if parent == "/dependency_impact" {
            result["dependency_impact"]["coverage"] = json!("partial");
        }
        mark_partial(result);
        result["budget_limited"] = json!(true);
    }
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
    session.record_search(query, true);

    // 1. path — existing file or dir, resolved relative to cwd (or as-is if absolute).
    let candidate = cwd.join(query);
    if candidate.exists() {
        if candidate.is_file() {
            if matches!(
                crate::lang::detect_file_type(&candidate),
                crate::types::FileType::Code(_)
            ) {
                let target_spec = format!("{query}:1");
                let (mut result, hints) =
                    unique_hit(&target_spec, "path", &candidate, 1, cwd, glob)?;
                result["query"] = json!(query);
                return Ok((result, "path".to_string(), hints, None));
            }
            let content_result = crate::search::search_content_raw(query, cwd, glob)?;
            let mut result = raw_result(query, "path", &content_result);
            if content_result.total_found > 0 {
                result["preview"] =
                    json!(crate::search::format_raw_result(&content_result, cache)?);
            }
            return Ok((result, "path".to_string(), Vec::new(), None));
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
    let content_result = crate::search::search_content_raw(query, cwd, glob)?;
    let route = if content_result.total_found > 0 {
        "literal"
    } else {
        "miss"
    };
    let mut result = raw_result(query, route, &content_result);
    if content_result.total_found > 0 {
        result["preview"] = json!(crate::search::format_raw_result(&content_result, cache)?);
    }
    Ok((result, route.to_string(), Vec::new(), None))
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
    let sym_result = crate::search::search_symbol_raw(query, cwd, glob)?;
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
    code_defs.sort_by(|a, b| a.path.cmp(&b.path).then(a.line.cmp(&b.line)));
    code_defs.dedup_by(|a, b| a.path == b.path && a.line == b.line);
    if code_defs.len() == 1 {
        let target = &code_defs[0];
        let (mut result, hints) =
            unique_hit(query, "symbol", &target.path, target.line, cwd, glob)?;
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
    let content_result = crate::search::search_content_raw(query, cwd, glob)?;
    let route = if content_result.total_found > 0 {
        "literal"
    } else {
        "miss"
    };
    let mut result = raw_result(query, route, &content_result);
    if sym_result.files_unreadable > 0 {
        mark_partial(&mut result);
    }
    if content_result.total_found > 0 {
        result["preview"] = json!(crate::search::format_raw_result(&content_result, cache)?);
    }
    Ok((result, route.to_string(), Vec::new()))
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
    cwd: &Path,
    glob: Option<&str>,
) -> Result<(Value, Vec<Value>), crate::error::TilthError> {
    let (line, name, body, core_partial) = if resolved_as == "path" {
        let content = std::fs::read_to_string(target_path).map_err(|source| {
            crate::error::TilthError::IoError {
                path: target_path.to_path_buf(),
                source,
            }
        })?;
        let body = content.lines().take(60).collect::<Vec<_>>().join("\n");
        let core_partial = content.lines().count() > 60;
        (None, None, body, core_partial)
    } else {
        let spec = format!("{}:{target_line}", target_path.display());
        let (target, content, _) = crate::search::grok::resolve_with_source(&spec, cwd)?;
        let (start, end) = (target.start_line, target.end_line);
        let body = content
            .lines()
            .skip(start.saturating_sub(1) as usize)
            .take((end - start + 1).min(60) as usize)
            .collect::<Vec<_>>()
            .join("\n");
        (Some(start), Some(target.name), body, end - start + 1 > 60)
    };
    let target = Target {
        path: display_rel(target_path, cwd),
        line,
        name,
        scope: cwd.to_string_lossy().into(),
        glob: glob.map(str::to_string),
    };
    if glob.is_some() && !target.allows(target_path, cwd) {
        return Ok((base_result(query, resolved_as, "no_match"), Vec::new()));
    }
    let mut result = base_result(query, resolved_as, "ok");
    result["core"] = json!(redact_secret_text(target_path, body));
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
        assert!(result["hints"].as_array().unwrap().is_empty());
        let mut response = json!({"results": [
            {"query": "one", "resolved_as": "literal", "status": "ok", "completeness": "complete", "preview": "\\\"".repeat(100_000)},
            {"query": "two", "resolved_as": "literal", "status": "ok", "completeness": "complete", "preview": "second"}
        ], "hints": [], "diagnostics": {}});
        for budget in [crate::budget::DEFAULT_BUDGET, 150] {
            let output = reduce_response(&mut response.clone(), budget).unwrap();
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
        assert!(reduce_response(&mut response, 200).is_ok());
        assert_eq!(
            response["results"][0]["dependency_impact"]["coverage"],
            "partial"
        );
    }

    #[test]
    fn budget_trim_keeps_ambiguous_status() {
        let mut response = json!({"results": [{"query": "root", "resolved_as": "ambiguous",
            "status": "ambiguous", "completeness": "complete",
            "candidates": [{"path": "x".repeat(5000)}]}], "hints": [], "diagnostics": {}});
        let output = reduce_response(&mut response, 100).unwrap();
        let parsed: Value = serde_json::from_str(&output).unwrap();
        assert_eq!(parsed["results"][0]["status"], "ambiguous");
        assert_eq!(parsed["results"][0]["completeness"], "partial");
        assert_eq!(parsed["results"][0]["budget_limited"], true);
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
        assert!(excluded["hints"].as_array().unwrap().is_empty());
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
    fn route_path_routes_non_code_file_to_text_search() {
        let resp = single_query("Cargo.lock").expect("path query succeeds");
        let result = &resp["results"][0];
        assert_eq!(result["resolved_as"], "path");
        assert_eq!(result["status"], "ok");
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
        assert!(hint_kinds.is_empty());
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
    #[test]
    fn continuation_hints_round_trip_for_every_kind() {
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
        let hints = initial["hints"].as_array().unwrap();
        assert_eq!(hints.len(), 5, "{initial}");
        for hint in hints {
            let followed = call(&json!({"cwd": cwd, "queries": [{"follow": hint}]})).unwrap();
            let result = &followed["results"][0];
            let expected = match hint["kind"].as_str().unwrap() {
                "fetch_callers" => "production_caller",
                "fetch_callees" => "leaf",
                "fetch_siblings" => "sibling",
                "fetch_tests" => "test_root",
                "fetch_dependencies" => "helper.ts",
                _ => unreachable!(),
            };
            assert_eq!(result["status"], "ok", "{followed}");
            assert!(
                result["preview"].as_str().unwrap().contains(expected),
                "{followed}"
            );
            assert!(!result["preview"].as_str().unwrap().contains("wrong_caller"));
            assert!(followed["hints"].as_array().unwrap().is_empty());
        }
        assert_eq!(hints[0]["target"]["line"], 2);
        assert_eq!(hints[0]["target"]["path"], "fixture.ts");
        let mixed = call(&json!({"cwd": cwd, "queries": [
            {"query": "^absent$"}, {"follow": hints[0]}, {"query": "root"}
        ]}))
        .unwrap();
        assert_eq!(mixed["results"][0]["status"], "no_match");
        assert_eq!(mixed["results"][1]["resolved_as"], "fetch_callers");
        assert_eq!(mixed["results"][2]["status"], "ambiguous");
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
        assert!(ambiguous["hints"].as_array().expect("hints").is_empty());
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
}
