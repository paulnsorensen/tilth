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
    let queries = args
        .get("queries")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            "missing required parameter \"queries\": pass an array of 1-10 {query, glob?} objects."
                .to_string()
        })?;
    if queries.is_empty() {
        return Err("\"queries\" must contain 1-10 entries; got 0.".to_string());
    }
    if queries.len() > 10 {
        return Err(format!(
            "\"queries\" must contain 1-10 entries; got {}.",
            queries.len()
        ));
    }

    let mut results = Vec::with_capacity(queries.len());
    let mut hints = Vec::new();
    let mut routes_tried = Vec::with_capacity(queries.len());
    let mut primary_route = String::new();

    for entry in queries {
        let query = entry
            .get("query")
            .and_then(Value::as_str)
            .ok_or_else(|| "each queries entry requires a \"query\" string.".to_string())?;
        let glob = entry.get("glob").and_then(Value::as_str);

        let (result, route, mut entry_hints) =
            route_query(query, glob, cwd, cache, session, bloom).map_err(|e| e.to_string())?;
        routes_tried.push(route.clone());
        if primary_route.is_empty() {
            primary_route = route;
        }
        hints.append(&mut entry_hints);
        results.push(result);
    }

    let response = json!({
        "results": results,
        "hints": hints,
        "diagnostics": {},
    });
    let response_str = serde_json::to_string(&response).map_err(|e| e.to_string())?;

    let _ = telemetry.record(&SearchTelemetryRecord {
        verb: "search_v2".to_string(),
        version: 1,
        route: primary_route,
        routes_tried,
        first_call: true,
        latency_ms: u64::try_from(start.elapsed().as_millis()).unwrap_or(u64::MAX),
        result_tokens: crate::types::estimate_tokens(response_str.len() as u64),
        partial: false,
        timeout: false,
        dependency_coverage: 1.0,
        shard_state: "none".to_string(),
        client: client.to_string(),
        worktree: worktree.to_string(),
    });

    Ok(response_str)
}

/// Route one query through the deterministic precedence: path -> regex ->
/// symbol/ambiguous -> literal -> miss. Returns the result record, the
/// resolved route name (for telemetry), and any hints emitted for it.
fn route_query(
    query: &str,
    glob: Option<&str>,
    cwd: &Path,
    cache: &OutlineCache,
    session: &Session,
    bloom: &BloomFilterCache,
) -> Result<(Value, String, Vec<Value>), crate::error::TilthError> {
    session.record_search(query, true);

    // 1. path — existing file or dir, resolved relative to cwd (or as-is if absolute).
    let candidate = cwd.join(query);
    if candidate.exists() {
        if candidate.is_file() {
            let target_spec = format!("{query}:1");
            let (result, hints) =
                unique_hit(&target_spec, "path", &candidate, cwd, bloom, session)?;
            let result = with_query(result, query);
            return Ok((result, "path".to_string(), hints));
        }
        let result = base_result(query, "path", "ok");
        return Ok((result, "path".to_string(), Vec::new()));
    }

    // 2. regex — contains a regex metacharacter (`.` and `/` don't count).
    if query.chars().any(|c| REGEX_METACHARS.contains(&c)) {
        let search_result = crate::search::search_regex_raw(query, cwd, glob)?;
        let mut result = base_result(query, "regex", "ok");
        result["preview"] = json!(crate::search::format_raw_result(&search_result, cache)?);
        return Ok((result, "regex".to_string(), Vec::new()));
    }

    // 3. symbol / ambiguous — bare identifier: prefer definitions, then reuse
    // usage matches or search literal content when no symbols were found.
    if is_identifier(query) {
        let sym_result = crate::search::search_symbol_raw(query, cwd, glob)?;
        if sym_result.definitions == 1 {
            let target = sym_result
                .matches
                .iter()
                .find(|m| m.is_definition)
                .expect("definitions == 1 implies one is_definition match");
            let (result, hints) = unique_hit(query, "symbol", &target.path, cwd, bloom, session)?;
            return Ok((result, "symbol".to_string(), hints));
        }
        if sym_result.definitions > 1 {
            let mut result = base_result(query, "ambiguous", "ambiguous");
            result["candidates"] = json!(candidates(&sym_result.matches, cwd));
            let hint = json!({"kind": "disambiguate", "target": query});
            return Ok((result, "ambiguous".to_string(), vec![hint]));
        }
        if sym_result.total_found > 0 {
            let mut result = base_result(query, "literal", "ok");
            result["preview"] = json!(crate::search::format_raw_result(&sym_result, cache)?);
            return Ok((result, "literal".to_string(), Vec::new()));
        }
        let content_result = crate::search::search_content_raw(query, cwd, glob)?;
        if content_result.total_found > 0 {
            let mut result = base_result(query, "literal", "ok");
            result["preview"] = json!(crate::search::format_raw_result(&content_result, cache)?);
            return Ok((result, "literal".to_string(), Vec::new()));
        }

        let result = base_result(query, "miss", "miss");
        return Ok((result, "miss".to_string(), Vec::new()));
    }

    // 4. literal — content search (non-identifier phrases only).
    let content_result = crate::search::search_content_raw(query, cwd, glob)?;
    if content_result.total_found > 0 {
        let mut result = base_result(query, "literal", "ok");
        result["preview"] = json!(crate::search::format_raw_result(&content_result, cache)?);
        return Ok((result, "literal".to_string(), Vec::new()));
    }

    // 5. miss — nothing matched.
    let result = base_result(query, "miss", "miss");
    Ok((result, "miss".to_string(), Vec::new()))
}

/// Overwrite the `"query"` field of an enrichment result built against a
/// derived target spec (e.g. `"path:1"`) with the caller's original query.
fn with_query(mut result: Value, query: &str) -> Value {
    result["query"] = json!(query);
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

/// Enrich a unique symbol/file hit with bounded grok core context and
/// verified-only dependency impact, plus the standard continuation hints.
fn unique_hit(
    query: &str,
    resolved_as: &str,
    target_path: &Path,
    cwd: &Path,
    bloom: &BloomFilterCache,
    session: &Session,
) -> Result<(Value, Vec<Value>), crate::error::TilthError> {
    let grok_result = crate::search::grok::grok(
        query,
        cwd,
        bloom,
        session,
        crate::search::grok::GrokCaps::default(),
    )?;
    let core = crate::search::grok::format_grok(&grok_result, cwd);

    let deps_result = crate::search::deps::analyze_deps(target_path, cwd, bloom)?;
    let impact = crate::search::deps::format_deps(&deps_result, cwd, None);

    let mut result = base_result(query, resolved_as, "ok");
    result["core"] = json!(core);
    result["dependency_impact"] = json!({
        "coverage": "complete",
        "impact": impact,
    });

    let hints = vec![
        json!({"kind": "callers", "target": query}),
        json!({"kind": "callees", "target": query}),
        json!({"kind": "siblings", "target": query}),
        json!({"kind": "tests", "target": query}),
        json!({"kind": "dependency_continuation", "target": query}),
    ];

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
    path.strip_prefix(cwd).unwrap_or(path).display().to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

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

    fn call_with_telemetry(args: Value) -> Result<(Value, tempfile::TempDir), String> {
        let (cache, session, bloom) = components();
        let (telemetry, tmp) = telemetry();
        let out = tool_search_v2(
            &args,
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

    fn call(args: Value) -> Result<Value, String> {
        call_with_telemetry(args).map(|(response, _tmp)| response)
    }

    fn single_query(query: &str) -> Result<Value, String> {
        call(json!({
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
    fn route_symbol_resolves_unique_definition() {
        let resp = single_query("detect_file_type").expect("symbol query succeeds");
        assert_eq!(resp["results"][0]["resolved_as"], "symbol");
    }

    #[test]
    fn usage_only_identifier_in_exact_file_falls_back_to_literal() {
        let (resp, tmp) = call_with_telemetry(json!({
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
        let resp = call(json!({
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
    }

    #[test]
    fn batch_of_three_preserves_order_and_yields_one_record_each() {
        let resp = call(json!({
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
    }

    #[test]
    fn empty_batch_is_refused() {
        let err = call(json!({
            "cwd": repo_root().to_str().unwrap(),
            "queries": [],
        }))
        .unwrap_err();
        assert!(err.contains("1-10"), "empty batch must be refused: {err}");
    }

    #[test]
    fn oversized_batch_is_refused() {
        let queries: Vec<Value> = (0..11).map(|i| json!({"query": format!("q{i}")})).collect();
        let err = call(json!({
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
            "callers",
            "callees",
            "siblings",
            "tests",
            "dependency_continuation",
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
        assert!(hint_kinds.contains(&"disambiguate"));
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
}
