//! `tilth_search` — code search dispatcher. Supports `queries: [...]` batch
//! form plus the legacy single-query form, with per-kind routing
//! (symbol / any / content / regex / callers) and `if_modified_since`
//! redaction of unchanged result sections.

use std::path::{Path, PathBuf};
use std::sync::Arc;

use serde_json::Value;

use crate::cache::OutlineCache;
use crate::index::bloom::BloomFilterCache;
use crate::session::Session;

pub(crate) fn tool_search(
    args: &Value,
    cache: &OutlineCache,
    session: &Session,
    bloom: &Arc<BloomFilterCache>,
    edit_mode: bool,
) -> Result<String, String> {
    let now = std::time::SystemTime::now();
    let since = args
        .get("if_modified_since")
        .and_then(|v| v.as_str())
        .and_then(crate::mcp::iso::parse_iso_utc);

    // v2 surface: `queries: [{query, glob?, kind?}]`. When present, run each
    // entry through the legacy single-query path and concatenate. Per-query
    // glob/kind override the top-level values.
    if let Some(queries_arr) = args.get("queries").and_then(|v| v.as_array()) {
        if queries_arr.is_empty() {
            return Err("queries array is empty".into());
        }
        if queries_arr.len() > 10 {
            return Err(format!(
                "queries array limited to 10 entries (got {})",
                queries_arr.len()
            ));
        }
        let mut parts: Vec<String> = Vec::with_capacity(queries_arr.len());
        for (i, q) in queries_arr.iter().enumerate() {
            let qstr = q
                .get("query")
                .and_then(|v| v.as_str())
                .ok_or_else(|| format!("queries[{i}]: missing 'query' string"))?;
            let mut sub = serde_json::Map::new();
            sub.insert("query".into(), Value::String(qstr.to_string()));
            if let Some(g) = q.get("glob").and_then(|v| v.as_str()) {
                sub.insert("glob".into(), Value::String(g.to_string()));
            } else if let Some(g) = args.get("glob").and_then(|v| v.as_str()) {
                sub.insert("glob".into(), Value::String(g.to_string()));
            }
            let kind = q
                .get("kind")
                .and_then(|v| v.as_str())
                .or_else(|| args.get("kind").and_then(|v| v.as_str()));
            if let Some(kind) = kind {
                sub.insert("kind".into(), Value::String(kind.to_string()));
            }
            for k in ["expand", "scope", "budget", "if_modified_since"] {
                if let Some(v) = args.get(k) {
                    sub.insert(k.into(), v.clone());
                }
            }
            let sub_val = Value::Object(sub);
            let body = tool_search_single(&sub_val, cache, session, bloom, edit_mode)?;
            parts.push(format!("## query: {qstr}\n\n{body}"));
        }
        let combined = parts.join("\n\n---\n\n");
        let (scope, _) = super::resolve_scope(args);
        let combined = since
            .map(|s| redact_unchanged_search_sections(&combined, &scope, s))
            .unwrap_or(combined);
        return Ok(crate::mcp::iso::with_header(now, &combined));
    }
    let body = tool_search_single(args, cache, session, bloom, edit_mode)?;
    let (scope, _) = super::resolve_scope(args);
    let body = since
        .map(|s| redact_unchanged_search_sections(&body, &scope, s))
        .unwrap_or(body);
    Ok(crate::mcp::iso::with_header(now, &body))
}

fn tool_search_single(
    args: &Value,
    cache: &OutlineCache,
    session: &Session,
    bloom: &Arc<BloomFilterCache>,
    edit_mode: bool,
) -> Result<String, String> {
    let query = args
        .get("query")
        .and_then(|v| v.as_str())
        .ok_or("missing required parameter: query (or queries array)")?;
    let (scope, scope_warning) = super::resolve_scope(args);
    let kind = args.get("kind").and_then(|v| v.as_str());
    let expand = args
        .get("expand")
        .and_then(serde_json::Value::as_u64)
        .unwrap_or(2) as usize;
    let glob = args.get("glob").and_then(|v| v.as_str());
    let budget = args.get("budget").and_then(serde_json::Value::as_u64);

    let output = match kind {
        None | Some("any") => {
            session.record_search(query);
            search_merged_default(
                query, &scope, cache, session, bloom, expand, glob, edit_mode,
            )
        }
        Some("symbol") => {
            use crate::search::symbol::SymbolMode;
            let mode = SymbolMode::Strict;
            let queries: Vec<&str> = query
                .split(',')
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .collect();
            match queries.len() {
                0 => return Err("missing required parameter: query".into()),
                1 => {
                    session.record_search(queries[0]);
                    crate::search::search_symbol_expanded_mode(
                        queries[0], &scope, cache, session, bloom, expand, glob, mode, edit_mode,
                        false,
                    )
                }
                2..=5 => {
                    for q in &queries {
                        session.record_search(q);
                    }
                    crate::search::search_multi_symbol_expanded_mode(
                        &queries, &scope, cache, session, bloom, expand, glob, mode, edit_mode,
                        false,
                    )
                }
                _ => {
                    return Err(format!(
                        "multi-symbol search limited to 5 queries (got {})",
                        queries.len()
                    ))
                }
            }
        }
        Some("content") => {
            session.record_search(query);
            crate::search::search_content_expanded_mode(
                query, &scope, cache, session, expand, glob, edit_mode, false,
            )
        }
        Some("regex") => {
            session.record_search(query);
            crate::search::search_regex_expanded_mode(
                query, &scope, cache, session, expand, glob, edit_mode, false,
            )
        }
        Some("callers") => {
            session.record_search(query);
            crate::search::callers::search_callers_expanded(
                query, &scope, bloom, expand, glob, false,
            )
        }
        Some(kind) => {
            return Err(format!(
                "unknown search kind: {kind}. Use: symbol, any, content, regex, callers"
            ))
        }
    }
    .map_err(|e| e.to_string())?;

    let mut result = scope_warning.unwrap_or_default();
    result.push_str(&super::apply_budget(output, budget));
    Ok(result)
}

#[allow(clippy::too_many_arguments)]
fn search_merged_default(
    query: &str,
    scope: &Path,
    cache: &OutlineCache,
    session: &Session,
    bloom: &Arc<BloomFilterCache>,
    expand: usize,
    glob: Option<&str>,
    edit_mode: bool,
) -> Result<String, crate::error::TilthError> {
    use crate::search::symbol::SymbolMode;

    let mut sections = Vec::new();
    sections.push(format!(
        "## symbol results\n\n{}",
        crate::search::search_symbol_expanded_mode(
            query,
            scope,
            cache,
            session,
            bloom,
            expand,
            glob,
            SymbolMode::Strict,
            edit_mode,
            false,
        )?
    ));
    sections.push(format!(
        "## content results\n\n{}",
        crate::search::search_content_expanded_mode(
            query, scope, cache, session, expand, glob, edit_mode, false,
        )?
    ));
    if crate::classify::is_identifier(query) {
        sections.push(format!(
            "## caller results\n\n{}",
            crate::search::callers::search_callers_expanded(
                query, scope, bloom, expand, glob, false,
            )?
        ));
    }
    Ok(sections.join("\n\n---\n\n"))
}

fn redact_unchanged_search_sections(
    output: &str,
    scope: &Path,
    since: std::time::SystemTime,
) -> String {
    let mut rendered = Vec::new();
    let mut current = Vec::new();
    let mut current_path: Option<PathBuf> = None;

    for line in output.lines() {
        if is_search_section_heading(line) {
            flush_search_section(&mut rendered, &mut current, current_path.take(), since);
            current_path = search_result_path(line, scope);
        }
        current.push(line.to_string());
    }
    flush_search_section(&mut rendered, &mut current, current_path, since);
    rendered.join("\n")
}

fn flush_search_section(
    rendered: &mut Vec<String>,
    current: &mut Vec<String>,
    current_path: Option<PathBuf>,
    since: std::time::SystemTime,
) {
    if current.is_empty() {
        return;
    }
    if let Some(path) = current_path {
        if !crate::mcp::iso::file_changed_since(&path, since) {
            rendered.push(crate::mcp::iso::unchanged_stub(&path, since));
            current.clear();
            return;
        }
    }
    rendered.push(current.join("\n"));
    current.clear();
}

fn is_search_section_heading(line: &str) -> bool {
    line.starts_with("## ") || line.starts_with("### ")
}

fn search_result_path(line: &str, scope: &Path) -> Option<PathBuf> {
    let rest = line
        .strip_prefix("### ")
        .or_else(|| line.strip_prefix("## "))?;
    let loc = rest.split_whitespace().next()?;
    let (path_part, _) = loc.rsplit_once(':')?;
    if path_part.is_empty() {
        return None;
    }
    let path = scope.join(path_part);
    path.exists().then_some(path)
}

#[cfg(test)]
mod tests {
    /// MCP must never raise the search match cap — token budgets are tuned
    /// for the default cap (10). This source-level guard reads the dispatcher
    /// and asserts every callsite that takes a trailing `full: bool` passes
    /// `false`. A future maintainer flipping any of these to `true` would
    /// flow more matches through to the MCP transport and silently blow the
    /// budget; the only safe-by-construction defense is the literal `false`.
    ///
    /// The check is intentionally lexical: it scans for known callsites and
    /// fails the build if their final argument before the closing paren is
    /// not exactly `false`. Brittle to renames — acceptable cost for the
    /// regression-protection.
    #[test]
    fn mcp_search_callsites_pass_full_false() {
        let full_src = include_str!("search.rs");
        // Only scan production code — the test module below contains the
        // needle strings themselves, which would otherwise produce false hits.
        let cutoff = full_src
            .find("#[cfg(test)]")
            .expect("test module marker not found");
        let src = &full_src[..cutoff];

        // Expanded library entrypoints + the callers expanded entrypoint.
        // Each was changed by full-search-cap to take a trailing `full: bool`.
        let callsites = [
            "search_symbol_expanded_mode(",
            "search_multi_symbol_expanded_mode(",
            "search_content_expanded_mode(",
            "search_regex_expanded_mode(",
            "search_callers_expanded(",
        ];

        for needle in &callsites {
            let mut start = 0;
            let mut found_any = false;
            while let Some(pos) = src[start..].find(needle) {
                found_any = true;
                let open = start + pos + needle.len() - 1; // index of '('
                let close = match_paren(src, open)
                    .unwrap_or_else(|| panic!("unmatched paren for {needle} at {open}"));
                let body = &src[open + 1..close];
                let last_arg = last_arg_token(body);
                assert_eq!(
                    last_arg, "false",
                    "MCP callsite `{needle}...)` must end with `false` (got `{last_arg}`)\nbody: {body}"
                );
                start = close + 1;
            }
            assert!(
                found_any,
                "expected at least one MCP callsite for `{needle}` — has the name changed?"
            );
        }
    }

    /// Return the index of the `)` that closes the `(` at `open`.
    fn match_paren(src: &str, open: usize) -> Option<usize> {
        let bytes = src.as_bytes();
        assert_eq!(bytes[open], b'(');
        let mut depth: i32 = 0;
        for (i, b) in bytes.iter().enumerate().skip(open) {
            match *b {
                b'(' => depth += 1,
                b')' => {
                    depth -= 1;
                    if depth == 0 {
                        return Some(i);
                    }
                }
                _ => {}
            }
        }
        None
    }

    /// Last comma-separated token in `body`, trimmed and with a trailing
    /// comma stripped. Returns the literal text of the last argument.
    fn last_arg_token(body: &str) -> &str {
        let trimmed = body.trim().trim_end_matches(',').trim_end();
        let after_last_comma = trimmed.rsplit(',').next().unwrap_or(trimmed).trim();
        // Strip line-prefixes / whitespace introduced by multi-line formatting.
        after_last_comma
            .trim_start_matches(|c: char| c.is_whitespace())
            .trim_end_matches(|c: char| c.is_whitespace())
    }
}
