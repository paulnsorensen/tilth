use std::io::{self, BufRead, Write};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::cache::OutlineCache;
use crate::index::bloom::BloomFilterCache;
use crate::session::Session;
use crate::timeout::{self, spawn_with_timeout, SpawnFailure, ThreadTracker};

mod iso;
mod path_suffix;
mod tools;
mod write;

use tools::{
    tool_definitions, tool_deps, tool_diff, tool_files, tool_grok, tool_read, tool_search,
    tool_write,
};

/// Shared dependencies passed through the request → dispatch pipeline.
#[derive(Clone)]
struct Services {
    cache: Arc<OutlineCache>,
    session: Arc<Session>,
    bloom: Arc<BloomFilterCache>,
    tracker: Arc<ThreadTracker>,
    edit_mode: bool,
}

impl Services {
    fn new(edit_mode: bool) -> Self {
        Self {
            cache: Arc::new(OutlineCache::new()),
            session: Arc::new(Session::new()),
            bloom: Arc::new(BloomFilterCache::new()),
            tracker: Arc::new(ThreadTracker::new()),
            edit_mode,
        }
    }

    fn cache(&self) -> &OutlineCache {
        &self.cache
    }

    fn session(&self) -> &Session {
        &self.session
    }

    fn bloom(&self) -> &Arc<BloomFilterCache> {
        &self.bloom
    }

    fn tracker(&self) -> &Arc<ThreadTracker> {
        &self.tracker
    }

    fn edit_mode(&self) -> bool {
        self.edit_mode
    }
}

// Sent to the LLM via the MCP `instructions` field during initialization.
// The strings live in prompts/mcp-base.md and prompts/mcp-edit.md so they can
// be versioned and rendered as Markdown. AGENTS.md is regenerated from the
// same files via scripts/regen-agents-md.sh, keeping the human-facing copy in
// lockstep with what MCP hosts receive in the `instructions` field.
const SERVER_INSTRUCTIONS: &str = include_str!("../../prompts/mcp-base.md");
const EDIT_MODE_EXTRA: &str = include_str!("../../prompts/mcp-edit.md");

/// MCP server over stdio. When `edit_mode` is true, exposes `tilth_write` and
/// switches `tilth_read` to hashline output format.
///
/// `scope` overrides the default search root. When provided, tilth chdir's to it
/// at startup so all tools, git commands, and searches use the correct project root.
/// This fixes MCP hosts that launch tilth with cwd=/ (e.g., Codex).
pub fn run(edit_mode: bool, scope: Option<&Path>) -> io::Result<()> {
    let scope_is_explicit = scope.is_some();

    // Resolve the project root and chdir to it.
    // Priority: explicit --scope > MCP roots (handled later) > package_root(cwd) > cwd
    if let Some(s) = scope {
        if s.is_dir() {
            let _ = std::env::set_current_dir(s);
        }
    } else {
        let cwd = std::env::current_dir().unwrap_or_default();
        if let Some(root) = crate::lang::package_root(&cwd) {
            let _ = std::env::set_current_dir(root);
        }
    }

    let services = Services::new(edit_mode);
    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut stdout = stdout.lock();

    // Track pending roots/list request (for MCP roots protocol)
    let mut pending_roots_id: Option<Value> = None;

    for line in stdin.lock().lines() {
        let line = line?;
        if line.is_empty() {
            continue;
        }

        // Parse as generic JSON first — could be a request, notification, or response
        let msg: Value = match serde_json::from_str(&line) {
            Ok(v) => v,
            Err(e) => {
                write_error(&mut stdout, None, -32700, &format!("parse error: {e}"))?;
                continue;
            }
        };

        // Check if this is a response to our roots/list request
        if let Some(ref roots_id) = pending_roots_id {
            if msg.get("id") == Some(roots_id) {
                pending_roots_id = None;
                // Only apply roots on success and if --scope was NOT explicitly provided
                if !scope_is_explicit {
                    if let Some(root_path) = extract_root_from_response(&msg) {
                        let _ = std::env::set_current_dir(&root_path);
                    }
                }
                continue;
            }
        }

        // Must have "method" to be a request or notification
        let method = match msg.get("method").and_then(Value::as_str) {
            Some(m) => m.to_string(),
            None => continue, // Not a request — skip (could be an unexpected response)
        };

        let id = msg.get("id").cloned();
        if id.is_none() {
            // Notifications have no id — silently drop them per JSON-RPC spec
            continue;
        }

        // Parse params
        let params = msg.get("params").cloned().unwrap_or(Value::Null);

        let req = JsonRpcRequest {
            _jsonrpc: "2.0".to_string(),
            id,
            method: method.clone(),
            params,
        };

        let response = handle_request(&req, &services);
        serde_json::to_writer(&mut stdout, &response)?;
        stdout.write_all(b"\n")?;
        stdout.flush()?;

        // After initialize response: send roots/list if client supports it
        // and we don't already have an explicit --scope
        if method == "initialize" && !scope_is_explicit && pending_roots_id.is_none() {
            let client_caps = req.params.get("capabilities").unwrap_or(&Value::Null);
            if client_caps.get("roots").is_some() {
                let roots_id = Value::String("tilth_roots_1".to_string());
                let roots_req = serde_json::json!({
                    "jsonrpc": "2.0",
                    "id": roots_id,
                    "method": "roots/list"
                });
                serde_json::to_writer(&mut stdout, &roots_req)?;
                stdout.write_all(b"\n")?;
                stdout.flush()?;
                pending_roots_id = Some(roots_id);
            }
        }
    }

    Ok(())
}

/// Extract the first root directory path from a roots/list response.
/// Parses `file://` URIs and returns the path, or None if no valid roots.
fn extract_root_from_response(msg: &Value) -> Option<PathBuf> {
    let roots = msg.get("result")?.get("roots")?.as_array()?;
    for root in roots {
        let uri = root.get("uri")?.as_str()?;
        let raw_path = uri.strip_prefix("file://").unwrap_or(uri);
        // On invalid UTF-8 in a percent-encoded path, fall back to the
        // original input rather than substituting U+FFFD replacements.
        let decoded = percent_encoding::percent_decode_str(raw_path)
            .decode_utf8()
            .map_or_else(|_| raw_path.to_string(), std::borrow::Cow::into_owned);
        let path = PathBuf::from(decoded);
        if path.is_dir() {
            return Some(path);
        }
    }
    None
}

#[derive(Deserialize)]
struct JsonRpcRequest {
    #[serde(rename = "jsonrpc")]
    _jsonrpc: String,
    id: Option<Value>,
    method: String,
    #[serde(default)]
    params: Value,
}

#[derive(Serialize)]
struct JsonRpcResponse {
    jsonrpc: &'static str,
    id: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    result: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<JsonRpcError>,
}

#[derive(Serialize)]
struct JsonRpcError {
    code: i32,
    message: String,
}

fn handle_request(req: &JsonRpcRequest, services: &Services) -> JsonRpcResponse {
    let edit_mode = services.edit_mode();
    match req.method.as_str() {
        "initialize" => {
            let overview = if std::env::var("TILTH_NO_OVERVIEW").is_ok() {
                String::new()
            } else {
                let cwd = std::env::current_dir().unwrap_or_default();
                crate::overview::fingerprint(&cwd)
            };
            let instructions = if edit_mode {
                if overview.is_empty() {
                    format!("{SERVER_INSTRUCTIONS}{EDIT_MODE_EXTRA}")
                } else {
                    format!("{overview}\n\n{SERVER_INSTRUCTIONS}{EDIT_MODE_EXTRA}")
                }
            } else if overview.is_empty() {
                SERVER_INSTRUCTIONS.to_string()
            } else {
                format!("{overview}\n\n{SERVER_INSTRUCTIONS}")
            };
            JsonRpcResponse {
                jsonrpc: "2.0",
                id: req.id.clone(),
                result: Some(serde_json::json!({
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "tools": {}
                    },
                    "serverInfo": {
                        "name": "tilth",
                        "version": env!("CARGO_PKG_VERSION")
                    },
                    "instructions": instructions
                })),
                error: None,
            }
        }

        "tools/list" => JsonRpcResponse {
            jsonrpc: "2.0",
            id: req.id.clone(),
            result: Some(serde_json::json!({
                "tools": tool_definitions(edit_mode)
            })),
            error: None,
        },

        "tools/call" => handle_tool_call(req, services),

        "ping" => JsonRpcResponse {
            jsonrpc: "2.0",
            id: req.id.clone(),
            result: Some(serde_json::json!({})),
            error: None,
        },

        _ => JsonRpcResponse {
            jsonrpc: "2.0",
            id: req.id.clone(),
            result: None,
            error: Some(JsonRpcError {
                code: -32601,
                message: format!("method not found: {}", req.method),
            }),
        },
    }
}

/// Execute a tool by name with the given arguments. Returns formatted output or error string.
/// No classifier involved — the caller specifies the tool explicitly.
fn dispatch_tool(tool: &str, args: &Value, services: &Services) -> Result<String, String> {
    let edit_mode = services.edit_mode();
    match tool {
        "tilth_read" => tool_read(args, services.cache(), services.session(), edit_mode),
        "tilth_search" => tool_search(args, services.cache(), services.session(), services.bloom()),
        "tilth_files" => tool_files(args),
        "tilth_deps" => tool_deps(args, services.bloom()),
        "tilth_grok" => tool_grok(args, services.bloom()),
        "tilth_diff" => tool_diff(args),
        "tilth_write" if edit_mode => tool_write(args, services.session(), services.bloom()),
        _ => Err(format!("unknown tool: {tool}")),
    }
}

// ---------------------------------------------------------------------------
// MCP tool call handler
// ---------------------------------------------------------------------------

fn handle_tool_call(req: &JsonRpcRequest, services: &Services) -> JsonRpcResponse {
    let params = &req.params;
    let tool_name = params.get("name").and_then(|v| v.as_str()).unwrap_or("");
    let args = params.get("arguments").unwrap_or(&Value::Null);

    let result = if services.tracker().is_at_cap() {
        Err(
            "server busy: too many prior operations still running after timeout. \
             Wait or set TILTH_TIMEOUT=<seconds> higher."
                .into(),
        )
    } else {
        run_tool_with_timeout(services, tool_name, args, timeout::request_timeout())
    };

    build_tool_response(req.id.clone(), result)
}

fn run_tool_with_timeout(
    services: &Services,
    tool_name: &str,
    args: &Value,
    timeout: std::time::Duration,
) -> Result<String, String> {
    let services_worker = services.clone();
    let tool_name_owned = tool_name.to_string();
    let args_owned = args.clone();

    let outcome = spawn_with_timeout(services.tracker(), timeout, move || {
        dispatch_tool(&tool_name_owned, &args_owned, &services_worker)
    });

    match outcome {
        Ok(inner) => inner,
        Err(SpawnFailure::Timeout) => Err(format!(
            "tool timed out after {}s — the operation took too long. \
             Try: reduce scope, use section instead of full, or set \
             TILTH_TIMEOUT=<seconds> to increase the limit.",
            timeout.as_secs()
        )),
        Err(SpawnFailure::Panic) => Err("tool panicked during execution".into()),
    }
}

fn build_tool_response(id: Option<Value>, result: Result<String, String>) -> JsonRpcResponse {
    let (text, is_error) = match result {
        Ok(output) => (output, false),
        Err(e) => (e, true),
    };
    let mut payload = serde_json::json!({
        "content": [{ "type": "text", "text": text }]
    });
    if is_error {
        payload["isError"] = Value::Bool(true);
    }
    JsonRpcResponse {
        jsonrpc: "2.0",
        id,
        result: Some(payload),
        error: None,
    }
}

fn write_error(w: &mut impl Write, id: Option<Value>, code: i32, msg: &str) -> io::Result<()> {
    let resp = JsonRpcResponse {
        jsonrpc: "2.0",
        id,
        result: None,
        error: Some(JsonRpcError {
            code,
            message: msg.into(),
        }),
    };
    serde_json::to_writer(&mut *w, &resp)?;
    w.write_all(b"\n")?;
    w.flush()
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::fmt::Write as _;

    // -- extract_root_from_response -------------------------------------------

    #[test]
    fn extract_root_valid_file_uri() {
        // Claude Code sends: {"result":{"roots":[{"uri":"file:///Users/x/project"}]}}
        let tmp = tempfile::tempdir().unwrap();
        let uri = format!("file://{}", tmp.path().display());
        let msg = serde_json::json!({
            "result": { "roots": [{ "uri": uri }] }
        });
        let path = extract_root_from_response(&msg);
        assert_eq!(path, Some(tmp.path().to_path_buf()));
    }

    #[test]
    fn extract_root_percent_encoded_uri() {
        let tmp = tempfile::tempdir().unwrap();
        let space_dir = tmp.path().join("my project");
        std::fs::create_dir(&space_dir).unwrap();
        let encoded =
            format!("file://{}", tmp.path().display()).replace(' ', "%20") + "/my%20project";
        let msg = serde_json::json!({
            "result": { "roots": [{ "uri": encoded }] }
        });
        let path = extract_root_from_response(&msg);
        assert_eq!(path, Some(space_dir));
    }

    #[test]
    fn extract_root_empty_roots() {
        // Codex sends: {"result":{"roots":[]}}
        let msg = serde_json::json!({
            "result": { "roots": [] }
        });
        assert_eq!(extract_root_from_response(&msg), None);
    }

    #[test]
    fn extract_root_nonexistent_path() {
        let msg = serde_json::json!({
            "result": { "roots": [{ "uri": "file:///nonexistent/path/that/does/not/exist" }] }
        });
        assert_eq!(extract_root_from_response(&msg), None);
    }

    #[test]
    fn extract_root_no_result() {
        let msg = serde_json::json!({"error": {"code": -1, "message": "nope"}});
        assert_eq!(extract_root_from_response(&msg), None);
    }

    #[test]
    fn extract_root_multiple_roots_takes_first_valid() {
        let tmp = tempfile::tempdir().unwrap();
        let uri = format!("file://{}", tmp.path().display());
        let msg = serde_json::json!({
            "result": { "roots": [
                { "uri": "file:///nonexistent" },
                { "uri": uri },
            ]}
        });
        // First root is invalid, second is valid — should return second
        let path = extract_root_from_response(&msg);
        assert_eq!(path, Some(tmp.path().to_path_buf()));
    }

    // -- package_root fallback from subdirectory ------------------------------

    #[test]
    fn package_root_finds_project_from_subdirectory() {
        let project = tempfile::tempdir().unwrap();
        let project_path = project.path();
        std::fs::write(
            project_path.join("Cargo.toml"),
            "[package]\nname = \"test\"",
        )
        .unwrap();
        let subdir = project_path.join("src").join("deep").join("nested");
        std::fs::create_dir_all(&subdir).unwrap();

        // package_root from the nested subdir should find the project root
        let root = crate::lang::package_root(&subdir);
        assert!(root.is_some(), "package_root should find the project");
        // Compare canonicalized paths to handle macOS /var -> /private/var symlinks
        let root_canon = root.unwrap().canonicalize().unwrap();
        let expected_canon = project_path.canonicalize().unwrap();
        assert_eq!(root_canon, expected_canon);
    }

    // -- prompt extraction byte locks ------------------------------------------
    //
    // These tests pin the MCP `instructions` strings to their pre-refactor byte
    // shapes so the prompts/*.md extraction is provably a no-op. They flag
    // future drift loudly: any prompt edit must update both the markdown source
    // and the assertions below.

    #[test]
    fn server_instructions_byte_lock() {
        assert_eq!(
            SERVER_INSTRUCTIONS.len(),
            3594,
            "SERVER_INSTRUCTIONS byte count drifted from baseline"
        );
        assert!(SERVER_INSTRUCTIONS
            .starts_with("tilth — code intelligence MCP server. Replaces grep, cat, find, ls"));
        assert!(SERVER_INSTRUCTIONS
            .ends_with("DO NOT re-read files already shown in expanded search results."));
        assert!(
            !SERVER_INSTRUCTIONS.contains("\n\n\n"),
            "SERVER_INSTRUCTIONS must not introduce triple newlines (likely a trailing-newline drift in prompts/mcp-base.md)"
        );
        assert!(SERVER_INSTRUCTIONS.contains("For multi-symbol lookup, separate each with a comma"));
        assert!(SERVER_INSTRUCTIONS
            .contains("Re-expanding a previously shown definition returns [shown earlier]"));
        assert!(
            SERVER_INSTRUCTIONS.contains("tilth_grok: Everything structural about a symbol"),
            "tilth_grok description must remain in SERVER_INSTRUCTIONS"
        );
    }

    #[test]
    fn edit_mode_extra_byte_lock() {
        assert_eq!(
            EDIT_MODE_EXTRA.len(),
            2424,
            "EDIT_MODE_EXTRA byte count drifted from refactor baseline"
        );
        assert!(
            EDIT_MODE_EXTRA.starts_with("\n\ntilth_write: Batch write"),
            "EDIT_MODE_EXTRA must keep its leading blank-line separator so format!(\"{{S}}{{E}}\") emits one blank line between sections"
        );
        assert!(EDIT_MODE_EXTRA
            .ends_with("DO NOT use the host Edit or Write tool. Use tilth_write for all writes."));
        assert!(
            !EDIT_MODE_EXTRA.contains("\n\n\n"),
            "EDIT_MODE_EXTRA must not introduce triple newlines"
        );
        assert!(EDIT_MODE_EXTRA.contains("(BOTH line and hash required)"));
    }

    #[test]
    fn instructions_compose_with_single_blank_line_between_sections() {
        // Pre-refactor: format!("{S}{E}") relied on EDIT_MODE_EXTRA's leading
        // "\n\n" to produce one blank line between the base and edit sections.
        // This asserts the composition still has that shape.
        let combined = format!("{SERVER_INSTRUCTIONS}{EDIT_MODE_EXTRA}");
        assert!(combined.contains(
            "DO NOT re-read files already shown in expanded search results.\n\ntilth_write: Batch write"
        ));
    }

    // -- tilth_read tool: batch reads, suffix grammar, view modes ----------
    // Restored from pre-merge 3801a4c (dropped by the #35 upstream merge).
    // These guard every behavior the batch-only read revert restored.

    /// Helper: parse the first line of a tool_read response as JSON when the
    /// header is present. Returns `None` when the response body has no JSON
    /// header (full content with no since/view-meta).
    fn parse_first_line_json(out: &str) -> Option<serde_json::Value> {
        let first = out.lines().next()?;
        serde_json::from_str(first).ok()
    }

    #[test]
    fn tool_read_paths_wrong_type_reports_type_error() {
        // A scalar (or any non-array) value for `paths` should produce a
        // type-specific error, not the generic "missing" message.
        let args = serde_json::json!({ "paths": "a.rs" });
        let cache = OutlineCache::new();
        let session = Session::new();
        let err = tool_read(&args, &cache, &session, false)
            .expect_err("scalar `paths` must be rejected as wrong type");
        assert!(
            err.contains("paths must be an array"),
            "unexpected error: {err}"
        );
        assert!(
            !err.contains("missing required parameter"),
            "wrong-type error must not claim the param is missing: {err}"
        );
    }

    #[test]
    fn tool_read_unknown_mode_errors() {
        let args = serde_json::json!({
            "paths": ["a.rs"],
            "mode": "banana"
        });
        let cache = OutlineCache::new();
        let session = Session::new();
        let err =
            tool_read(&args, &cache, &session, false).expect_err("unknown mode must be rejected");
        assert!(err.contains("unknown read mode"), "unexpected error: {err}");
    }

    /// Batch reads must return the content of every submitted path — no file
    /// is dropped or reordered on the way through the tool handler.
    #[test]
    fn batch_read_returns_all_files() {
        let dir = tempfile::tempdir().unwrap();
        let file_count = 5usize;

        let paths: Vec<PathBuf> = (0..file_count)
            .map(|i| {
                let p = dir.path().join(format!("file{i}.txt"));
                std::fs::write(&p, format!("content-of-file-{i}")).unwrap();
                p
            })
            .collect();

        let paths_json: Vec<serde_json::Value> = paths
            .iter()
            .map(|p| serde_json::json!(p.to_str().unwrap()))
            .collect();

        let args = serde_json::json!({ "paths": paths_json });
        let cache = OutlineCache::new();
        let session = Session::new();

        let result = tool_read(&args, &cache, &session, false).expect("batch read must succeed");

        for i in 0..file_count {
            assert!(
                result.contains(&format!("content-of-file-{i}")),
                "output must contain content of file {i}"
            );
        }
    }

    #[test]
    fn batch_read_mode_full_applies_to_all_paths() {
        let dir = tempfile::tempdir().unwrap();
        let p1 = dir.path().join("a.rs");
        let p2 = dir.path().join("b.rs");
        let large = format!(
            "fn only_signature() {{}}\n{}",
            "// padding padding padding padding\n".repeat(1000)
        );
        std::fs::write(&p1, &large).unwrap();
        std::fs::write(&p2, "fn small() {}\n").unwrap();

        let args = serde_json::json!({
            "paths": [p1.to_str().unwrap(), p2.to_str().unwrap()],
            "mode": "full"
        });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&args, &cache, &session, false).expect("batch full read ok");
        assert!(
            out.contains("padding padding"),
            "large body must be included: {out}"
        );
        assert!(
            out.contains("fn small"),
            "small body must be included: {out}"
        );
    }

    /// Batch reads must surface every requested path: existing files inline,
    /// missing files in a trailing `── not found ──` section.
    #[test]
    fn batch_read_not_found_section_lists_missing_paths() {
        let dir = tempfile::tempdir().unwrap();
        let real = dir.path().join("real.py");
        std::fs::write(&real, "x = 1\ny = 2\n").unwrap();
        let missing = dir.path().join("test_name_function");

        let args = serde_json::json!({
            "paths": [real.to_str().unwrap(), missing.to_str().unwrap()],
        });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&args, &cache, &session, false)
            .expect("batch read must succeed with mixed valid/missing");

        assert!(
            out.contains("x = 1"),
            "valid file content must be included: {out}"
        );
        assert!(
            out.contains("── not found ──"),
            "not-found section must appear: {out}"
        );
        let nf_idx = out
            .find("── not found ──")
            .expect("not-found header present");
        let nf_section = &out[nf_idx..];
        assert!(
            nf_section.contains("test_name_function"),
            "missing path must be listed in not-found section: {out}"
        );
        assert!(
            !nf_section.contains("real.py"),
            "valid path must not be in not-found section: {out}"
        );
    }

    /// Spec: "Don't error the whole call." An all-missing batch must still
    /// return Ok with only the `── not found ──` section — no inline file blocks.
    #[test]
    fn batch_read_all_missing_returns_section_only() {
        let dir = tempfile::tempdir().unwrap();
        let m1 = dir.path().join("ghost_a");
        let m2 = dir.path().join("ghost_b");

        let args = serde_json::json!({
            "paths": [m1.to_str().unwrap(), m2.to_str().unwrap()],
        });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&args, &cache, &session, false)
            .expect("all-missing batch must succeed (Ok), not error the whole call");

        assert!(
            out.contains("── not found ──"),
            "not-found section must appear: {out}"
        );
        assert!(out.contains("ghost_a"), "first missing listed: {out}");
        assert!(out.contains("ghost_b"), "second missing listed: {out}");
    }

    /// Locks completeness (every missing path appears) and ordering (input
    /// order preserved), plus the structural invariant that valid file
    /// content comes before the not-found section.
    #[test]
    fn batch_read_missing_paths_listed_in_order_after_valid_content() {
        let dir = tempfile::tempdir().unwrap();
        let real = dir.path().join("real.py");
        std::fs::write(&real, "x = 1\n").unwrap();
        let m1 = dir.path().join("aaa_missing");
        let m2 = dir.path().join("zzz_missing");

        let args = serde_json::json!({
            "paths": [
                m1.to_str().unwrap(),
                real.to_str().unwrap(),
                m2.to_str().unwrap(),
            ],
        });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&args, &cache, &session, false).expect("mixed batch succeeds");

        let content_idx = out.find("x = 1").expect("real file content present");
        let nf_idx = out
            .find("── not found ──")
            .expect("not-found section present");
        assert!(
            content_idx < nf_idx,
            "valid content must appear before the not-found section: {out}"
        );

        let nf = &out[nf_idx..];
        let i1 = nf.find("aaa_missing").expect("first missing listed");
        let i2 = nf.find("zzz_missing").expect("second missing listed");
        assert!(
            i1 < i2,
            "missing paths must appear in input order, not sorted or reversed: {nf}"
        );
    }

    /// Boundary check: the not-found section is batch-specific. A single
    /// missing path keeps the prior Err behaviour, so callers that depend
    /// on the explicit error code path still see it.
    #[test]
    fn single_missing_path_does_not_use_not_found_section() {
        let dir = tempfile::tempdir().unwrap();
        let missing = dir.path().join("ghost_solo");
        let args = serde_json::json!({ "paths": [missing.to_str().unwrap()] });
        let cache = OutlineCache::new();
        let session = Session::new();
        let result = tool_read(&args, &cache, &session, false);
        assert!(
            result.is_err(),
            "single missing path must surface as Err, not as a not-found section"
        );
    }

    /// A `#symbol` suffix that doesn't resolve in an otherwise-readable file
    /// is the symbol-equivalent of a missing path: it must land in the
    /// `── not found ──` footer using the qualified `<path>#<symbol>` form,
    /// not as an inline error mixed into the content stream.
    #[test]
    fn batch_read_symbol_miss_listed_in_not_found_section() {
        let dir = tempfile::tempdir().unwrap();
        let p1 = dir.path().join("real.rs");
        let p2 = dir.path().join("other.rs");
        std::fs::write(&p1, "fn real_fn() {}\n").unwrap();
        std::fs::write(&p2, "fn other_fn() {}\n").unwrap();

        // Mix: file exists + symbol exists, file exists + symbol missing.
        let target_real = format!("{}#real_fn", p1.to_str().unwrap());
        let target_miss = format!("{}#ghost_symbol", p2.to_str().unwrap());

        let args = serde_json::json!({ "paths": [target_real, target_miss] });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&args, &cache, &session, false)
            .expect("batch with symbol miss must succeed (Ok)");

        let nf_idx = out
            .find("── not found ──")
            .expect("not-found section must be present");
        let nf_section = &out[nf_idx..];

        // Found symbol's body must appear before the footer, not in it.
        let body_idx = out
            .find("fn real_fn")
            .expect("resolved symbol body present");
        assert!(
            body_idx < nf_idx,
            "resolved symbol body must precede the not-found section: {out}"
        );

        // Miss must use the qualified `path#symbol` form in the footer.
        let qualified = format!("{}#ghost_symbol", p2.display());
        assert!(
            nf_section.contains(&qualified),
            "missing symbol must appear as `<path>#<symbol>` in footer: {nf_section}"
        );

        // The old inline error string must no longer appear anywhere.
        assert!(
            !out.contains("error: symbol 'ghost_symbol' not found in outline"),
            "symbol miss must not surface as an inline error in the content stream: {out}"
        );
    }

    /// Precondition-failure boundary: a `#symbol` suffix on a non-code file
    /// (no tree-sitter grammar) must NOT be routed to `── not found ──` —
    /// that would misrepresent "wrong file type for symbol grammar" as
    /// "you typed the wrong symbol name." Falls through to the existing
    /// inline error path instead.
    #[test]
    fn batch_read_symbol_on_non_code_file_falls_through_to_inline_error() {
        let dir = tempfile::tempdir().unwrap();
        let code = dir.path().join("real.rs");
        let txt = dir.path().join("notes.txt");
        std::fs::write(&code, "fn real_fn() {}\n").unwrap();
        std::fs::write(&txt, "just some prose, no grammar\n").unwrap();

        let target_real = format!("{}#real_fn", code.to_str().unwrap());
        let target_precondition = format!("{}#anything", txt.to_str().unwrap());

        let args = serde_json::json!({ "paths": [target_real, target_precondition] });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&args, &cache, &session, false)
            .expect("batch with non-code symbol target must succeed (Ok)");

        // The non-code path must NOT appear in the not-found footer — if it
        // does, we're misclassifying "wrong file type" as "missing symbol".
        if let Some(nf_idx) = out.find("── not found ──") {
            let nf_section = &out[nf_idx..];
            assert!(
                !nf_section.contains(&format!("{}#anything", txt.display())),
                "non-code file symbol target must not appear in not-found footer: {nf_section}"
            );
        }
    }

    // -- batch tool_edit --------------------------------------------------------

    /// `tilth_read` accepts the `path#n-m` suffix grammar.
    #[test]
    fn tool_read_line_range_suffix() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("a.txt");
        std::fs::write(&p, "l1\nl2\nl3\nl4\nl5\n").unwrap();
        let spec = format!("{}#2-4", p.to_str().unwrap());
        let args = serde_json::json!({ "paths": [spec] });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&args, &cache, &session, false).expect("suffix accepted");
        assert!(out.contains("l2"), "expected l2 in output: {out}");
        assert!(out.contains("l4"), "expected l4 in output: {out}");
        assert!(!out.contains("l5"), "must not include l5: {out}");
    }

    /// `tilth_read` heading suffix `path## Heading` resolves to that section.
    #[test]
    fn tool_read_heading_suffix() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("doc.md");
        std::fs::write(&p, "# Top\nintro\n## Foo\nfoo body\n## Bar\nbar body\n").unwrap();
        // Path-suffix grammar: `path#<heading text>` (with internal space)
        let spec = format!("{}#Foo", p.to_str().unwrap());
        // Without internal space, it's classified as symbol — for headings
        // use form with `##`. Use heading-style suffix instead:
        let spec_heading = format!("{}### Bar", p.to_str().unwrap());
        let _ = spec; // unused: the symbol form would fail on .md (no Code lang)
        let args = serde_json::json!({ "paths": [spec_heading] });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&args, &cache, &session, false).expect("heading suffix");
        assert!(out.contains("bar body"), "expected heading content: {out}");
    }

    /// `tool_read` with `if_modified_since` in the future returns an
    /// `(unchanged)` stub rather than reading the file. Spec criterion 11.
    #[test]
    fn tool_read_if_modified_since_future_returns_unchanged_stub() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("big.txt");
        std::fs::write(&p, "contents you should NOT see\n").unwrap();
        // Pick a timestamp well in the future; file mtime <= ts ⇒ unchanged.
        let args = serde_json::json!({
            "paths": [p.to_str().unwrap()],
            "if_modified_since": "2099-01-01T00:00:00Z"
        });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&args, &cache, &session, false).expect("stub ok");
        assert!(out.contains("unchanged"), "expected stub marker: {out}");
        assert!(
            !out.contains("contents you should NOT see"),
            "body must not leak on unchanged stub: {out}"
        );
        assert!(
            out.contains("\"if_modified_since\""),
            "JSON cache-token header missing: {out}"
        );
    }

    /// `tool_read` with `if_modified_since` in the past (epoch) returns the
    /// actual file content. Boundary partner for the unchanged-stub test.
    #[test]
    fn tool_read_if_modified_since_past_returns_content() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("now.txt");
        std::fs::write(&p, "hello world\n").unwrap();
        let args = serde_json::json!({
            "paths": [p.to_str().unwrap()],
            "if_modified_since": "1970-01-01T00:00:00Z"
        });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&args, &cache, &session, false).expect("content ok");
        assert!(out.contains("hello world"), "expected body: {out}");
    }

    /// `tilth_read` `path#n` (FromLine) suffix returns from line n to end.
    #[test]
    fn tool_read_from_line_suffix() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("f.txt");
        std::fs::write(&p, "l1\nl2\nl3\nl4\n").unwrap();
        let spec = format!("{}#3", p.to_str().unwrap());
        let args = serde_json::json!({ "paths": [spec] });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&args, &cache, &session, false).expect("from-line suffix ok");
        assert!(out.contains("l3"), "line 3 expected: {out}");
        assert!(out.contains("l4"), "line 4 expected: {out}");
        assert!(!out.contains("l1"), "line 1 must be excluded: {out}");
    }

    /// `mode: signature` emits hash-prefixed signature lines, not full bodies.
    #[test]
    fn tool_read_signature_mode_emits_hash_prefixed_signatures() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("lib.rs");
        std::fs::write(
            &p,
            "fn signature_target() {\n    let body_marker = 42;\n}\n",
        )
        .unwrap();
        let args = serde_json::json!({
            "paths": [p.to_str().unwrap()],
            "mode": "signature"
        });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&args, &cache, &session, false).expect("signature ok");
        assert!(
            out.contains("[signature]"),
            "signature header missing: {out}"
        );
        assert!(
            out.lines().any(
                |l| crate::format::parse_anchor(l.split('|').next().unwrap_or("")).is_some()
                    && l.contains("fn signature_target")
            ),
            "hash-prefixed signature line missing: {out}"
        );
        assert!(
            !out.contains("body_marker"),
            "signature mode must not include function body: {out}"
        );
    }

    /// Auto mode uses the same hash-prefixed signature output for large code.
    #[test]
    fn tool_read_auto_large_code_emits_hash_prefixed_signatures() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("large.rs");
        let mut src = String::from("fn large_signature_target() {\n    let body_marker = 42;\n}\n");
        src.push_str(&"// padding padding padding padding\n".repeat(1000));
        std::fs::write(&p, src).unwrap();
        let args = serde_json::json!({ "paths": [p.to_str().unwrap()] });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&args, &cache, &session, false).expect("auto signature ok");
        assert!(
            out.contains("[signature]"),
            "signature header missing: {out}"
        );
        assert!(
            out.lines().any(|l| l.contains("large_signature_target")
                && crate::format::parse_anchor(l.split('|').next().unwrap_or("")).is_some()),
            "hash-prefixed signature line missing: {out}"
        );
        assert!(
            !out.contains("body_marker"),
            "auto large-code signature must not include body: {out}"
        );
    }

    /// Auto mode on small code returns the full body (header `[full]`),
    /// covering row 1 / column 1 of the spec heuristic table.
    #[test]
    fn tool_read_auto_small_code_returns_full_body() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("small.rs");
        std::fs::write(&p, "fn small_target() {\n    let body_marker = 1;\n}\n").unwrap();
        let args = serde_json::json!({ "paths": [p.to_str().unwrap()] });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&args, &cache, &session, false).expect("auto small-code ok");
        assert!(out.contains("[full]"), "expected `[full]` header: {out}");
        assert!(
            out.contains("body_marker"),
            "small code must include the body, not just signatures: {out}"
        );
    }

    /// Auto mode on a small markdown file returns the full body (`[full]`),
    /// covering row 2 / column 1 of the spec heuristic table.
    #[test]
    fn tool_read_auto_small_markdown_returns_full_body() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("notes.md");
        std::fs::write(&p, "# Title\n\nBody paragraph that must appear verbatim.\n").unwrap();
        let args = serde_json::json!({ "paths": [p.to_str().unwrap()] });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&args, &cache, &session, false).expect("auto small-md ok");
        assert!(out.contains("[full]"), "expected `[full]` header: {out}");
        assert!(
            out.contains("Body paragraph that must appear verbatim"),
            "small markdown must include body: {out}"
        );
    }

    /// Auto mode on a large markdown file returns the heading-and-preview
    /// outline (`[outline]`), covering row 2 / column 2 of the heuristic.
    #[test]
    fn tool_read_auto_large_markdown_returns_outline() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("large.md");
        let mut src = String::from("# Top\n\n## Headline Marker\n\nBody preview line one.\n");
        src.push_str(&"filler line repeated for size.\n".repeat(2_000));
        std::fs::write(&p, src).unwrap();
        let args = serde_json::json!({ "paths": [p.to_str().unwrap()] });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&args, &cache, &session, false).expect("auto large-md ok");
        assert!(
            out.contains("[outline]"),
            "expected `[outline]` header: {out}"
        );
        assert!(
            out.contains("Headline Marker"),
            "large markdown outline must surface headings: {out}"
        );
        assert!(
            !out.contains("filler line repeated"),
            "large markdown outline must not dump filler body: {out}"
        );
    }

    /// Auto mode on a large structured (JSON) file returns the keys outline
    /// (`[keys]`), covering the structured row of the heuristic.
    #[test]
    fn tool_read_auto_large_structured_returns_keys() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("config.json");
        let mut src = String::from("{\n  \"top_level_marker\": {\n");
        for i in 0..2_000 {
            let _ = writeln!(
                src,
                "    \"padding_key_{i}\": \"value-value-value-value-value-{i}\","
            );
        }
        src.push_str("    \"trailing_key\": null\n  }\n}\n");
        std::fs::write(&p, src).unwrap();
        let args = serde_json::json!({ "paths": [p.to_str().unwrap()] });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&args, &cache, &session, false).expect("auto structured ok");
        assert!(out.contains("[keys]"), "expected `[keys]` header: {out}");
        assert!(
            out.contains("top_level_marker"),
            "structured outline must surface top-level keys: {out}"
        );
    }

    /// Auto mode on a plain text file falls back to the file_type-specific
    /// outline branch (`[outline]`) — no signature path applies because
    /// `should_auto_signature` only fires for code, covering the "other
    /// text" row of the heuristic.
    #[test]
    fn tool_read_auto_other_text_does_not_signature() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("notes.txt");
        let body: String = "plain prose line that is not code.\n".repeat(2_000);
        std::fs::write(&p, body).unwrap();
        let args = serde_json::json!({ "paths": [p.to_str().unwrap()] });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&args, &cache, &session, false).expect("auto other-text ok");
        assert!(
            !out.contains("[signature]"),
            "non-code file must never use signature mode: {out}"
        );
    }

    /// `mode=stripped` on a code file removes plain comments + debug logs
    /// while preserving doc comments and TODO/FIXME markers, and emits
    /// `view: "stripped"` in the meta header along with `lines_stripped`.
    #[test]
    fn tool_read_stripped_mode_drops_comments_and_keeps_doc_comments() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("strip_target.rs");
        // `dbg!` is on the Rust debug-log strip list; `println!` is not
        // (intentional — `println!` is often legitimate CLI output, not noise).
        std::fs::write(
            &p,
            "/// Doc comment that survives.\nfn target() {\n    // plain comment that goes\n    // TODO: keep this one\n    let kept = 1;\n    dbg!(\"debug log dropped\");\n}\n",
        )
        .unwrap();
        let args = serde_json::json!({
            "paths": [p.to_str().unwrap()],
            "mode": "stripped"
        });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&args, &cache, &session, false).expect("stripped ok");

        let meta = parse_first_line_json(&out).expect("JSON view-meta header expected");
        assert_eq!(meta.get("view").and_then(|v| v.as_str()), Some("stripped"));
        // Explicit `mode=stripped` is a deliberate shape request — like
        // `mode=signature`, it MUST NOT advertise `next_view`. The agent
        // already picked this view.
        assert!(
            meta.get("next_view").is_none(),
            "explicit mode=stripped must not emit next_view: {out}"
        );
        let lines_stripped = meta
            .get("lines_stripped")
            .and_then(serde_json::Value::as_u64)
            .expect("lines_stripped must be present");
        assert!(
            lines_stripped >= 2,
            "expected at least 2 lines stripped (plain comment + dbg!), got {lines_stripped}: {out}"
        );

        assert!(out.contains("[stripped]"), "header view tag: {out}");
        assert!(
            out.contains("Doc comment that survives"),
            "doc comments must be kept: {out}"
        );
        assert!(out.contains("TODO: keep this one"), "TODOs kept: {out}");
        assert!(out.contains("let kept = 1"), "real code kept: {out}");
        assert!(
            !out.contains("plain comment that goes"),
            "plain comment must be stripped: {out}"
        );
        assert!(
            !out.contains("debug log dropped"),
            "debug log must be stripped: {out}"
        );
    }

    /// Stripped output uses original 1-indexed line numbers in a left gutter
    /// so the agent can see which line numbers were dropped (gaps) without
    /// having to diff against the file.
    #[test]
    fn tool_read_stripped_preserves_original_line_numbers_in_gutter() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("gutter.rs");
        std::fs::write(
            &p,
            "fn alpha() {}\n// stripped line 2\nfn beta() {}\n// stripped line 4\nfn gamma() {}\n",
        )
        .unwrap();
        let args = serde_json::json!({
            "paths": [p.to_str().unwrap()],
            "mode": "stripped"
        });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&args, &cache, &session, false).expect("stripped ok");
        // Lines 1, 3, 5 survive; gutter shows the original numbers.
        assert!(
            out.contains("1  fn alpha()")
                && out.contains("3  fn beta()")
                && out.contains("5  fn gamma()"),
            "expected original line numbers in gutter: {out}"
        );
    }

    /// Hashlines must NOT appear in stripped output even when the server is
    /// in edit mode — the line set is non-contiguous with the file on disk
    /// and would mislead the agent into trying to anchor a write.
    #[test]
    fn tool_read_stripped_suppresses_hashlines_in_edit_mode() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("nohash.rs");
        std::fs::write(&p, "fn keep() {}\n// stripped\nfn also() {}\n").unwrap();
        let args = serde_json::json!({
            "paths": [p.to_str().unwrap()],
            "mode": "stripped"
        });
        let cache = OutlineCache::new();
        let session = Session::new();
        // edit_mode = true intentionally — stripped MUST still suppress hashlines.
        let out = tool_read(&args, &cache, &session, true).expect("stripped+edit ok");
        // Hashline format is `<line>:<3-hex>|<content>`. Check the body has
        // no such anchored line for our `fn keep()` content.
        assert!(
            !out.lines().any(
                |l| crate::format::parse_anchor(l.split('|').next().unwrap_or("")).is_some()
                    && l.contains("fn keep()")
            ),
            "no hashline anchors in stripped output: {out}"
        );
        assert!(
            out.contains("non-editable view"),
            "non-editable note expected in inline header: {out}"
        );
    }

    /// `mode=stripped` + path suffix → suffix wins, raw range returned with no
    /// strip pass. Suffix-takes-priority is the consistent rule across modes.
    #[test]
    fn tool_read_stripped_with_suffix_returns_raw_range() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("suffix_wins.rs");
        std::fs::write(
            &p,
            "fn a() {}\n// this comment must NOT be stripped\nfn b() {}\n",
        )
        .unwrap();
        let args = serde_json::json!({
            "paths": [format!("{}#1-3", p.to_str().unwrap())],
            "mode": "stripped"
        });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&args, &cache, &session, false).expect("stripped+suffix ok");
        assert!(
            out.contains("this comment must NOT be stripped"),
            "suffix wins; comments survive in raw slice: {out}"
        );
        assert!(
            !out.contains("[stripped]"),
            "suffix slice must use [section] header, not [stripped]: {out}"
        );
    }

    /// Unknown mode error must mention `stripped` so agents discover the new mode.
    #[test]
    fn tool_read_unknown_mode_error_lists_stripped() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("x.rs");
        std::fs::write(&p, "fn x() {}\n").unwrap();
        let args = serde_json::json!({
            "paths": [p.to_str().unwrap()],
            "mode": "minified_maybe"
        });
        let cache = OutlineCache::new();
        let session = Session::new();
        let err = tool_read(&args, &cache, &session, false).expect_err("unknown mode rejected");
        assert!(err.contains("stripped"), "error must list new mode: {err}");
    }

    /// Auto-signature on large code emits `view: "signature"` and the
    /// `next_view: "full"` escalation hint (implicit promotion).
    #[test]
    fn tool_read_auto_signature_emits_view_meta_with_next_view() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("big.rs");
        // Push the file over the auto-signature threshold (~24KB → >6000 tokens).
        let mut src = String::from("fn implicit_target() {}\n");
        src.push_str(&"// padding padding padding padding padding\n".repeat(2000));
        std::fs::write(&p, src).unwrap();
        let args = serde_json::json!({ "paths": [p.to_str().unwrap()] });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&args, &cache, &session, false).expect("auto sig ok");
        let meta = parse_first_line_json(&out).expect("view-meta JSON header expected");
        assert_eq!(meta.get("view").and_then(|v| v.as_str()), Some("signature"));
        assert_eq!(
            meta.get("next_view").and_then(|v| v.as_str()),
            Some("full"),
            "auto promotion advertises escalation: {out}"
        );
        assert!(
            meta.get("original_line_count")
                .and_then(serde_json::Value::as_u64)
                .is_some(),
            "original_line_count required for 'showing N of M' rendering: {out}"
        );
    }

    /// Explicit `mode=signature` emits `view: "signature"` but NOT
    /// `next_view` — the LLM picked this view on purpose.
    #[test]
    fn tool_read_explicit_signature_omits_next_view() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("small.rs");
        std::fs::write(&p, "fn small_target() {\n    let x = 1;\n}\n").unwrap();
        let args = serde_json::json!({
            "paths": [p.to_str().unwrap()],
            "mode": "signature"
        });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&args, &cache, &session, false).expect("explicit sig ok");
        let meta = parse_first_line_json(&out).expect("view-meta JSON header expected");
        assert_eq!(meta.get("view").and_then(|v| v.as_str()), Some("signature"));
        assert!(
            meta.get("next_view").is_none(),
            "explicit signature must not nag with next_view: {out}"
        );
    }

    /// `mode=auto` on a small code file returns full content and emits NO
    /// view-meta JSON header (the LLM has everything; no signal needed).
    #[test]
    fn tool_read_auto_small_code_omits_view_meta_header() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("tiny.rs");
        std::fs::write(&p, "fn tiny() {\n    let body = 1;\n}\n").unwrap();
        let args = serde_json::json!({ "paths": [p.to_str().unwrap()] });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&args, &cache, &session, false).expect("auto small ok");
        // First line must NOT be a JSON header — the file's `# path` markdown header should lead.
        let first = out.lines().next().expect("at least one line");
        assert!(
            !first.starts_with('{'),
            "small full reads must not emit a JSON header: {out}"
        );
    }

    /// Auto-outline on a large markdown emits `view: "outline"` + `next_view`.
    #[test]
    fn tool_read_auto_large_markdown_emits_outline_view_meta() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("big.md");
        let mut src = String::from("# Title\n\n## Section A\n\n");
        src.push_str(&"Lorem ipsum padding line.\n".repeat(2000));
        std::fs::write(&p, src).unwrap();
        let args = serde_json::json!({ "paths": [p.to_str().unwrap()] });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&args, &cache, &session, false).expect("auto large md ok");
        let meta = parse_first_line_json(&out).expect("view-meta JSON header expected");
        assert_eq!(meta.get("view").and_then(|v| v.as_str()), Some("outline"));
        assert_eq!(meta.get("next_view").and_then(|v| v.as_str()), Some("full"));
    }

    /// Budget truncation surfaces `truncated`, `truncated_at_line`, and
    /// `original_line_count` in the view-meta header so the host can render
    /// a "showing 1–N of M lines" hint without re-reading the file.
    #[test]
    fn tool_read_budget_truncation_emits_meta_fields() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("clip.rs");
        // Many small functions separated by blank lines — `apply()` prefers
        // `\n\n` boundaries when truncating, so we need internal blank lines
        // for it to find a non-zero cut point.
        let mut src = String::new();
        for i in 0..100 {
            src.push_str(&format!("fn f{i}() {{\n    let l = {i};\n}}\n\n"));
        }
        std::fs::write(&p, src).unwrap();
        let args = serde_json::json!({
            "paths": [p.to_str().unwrap()],
            "budget": 400
        });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&args, &cache, &session, false).expect("budget read ok");
        let meta = parse_first_line_json(&out).expect("view-meta JSON header expected");
        assert_eq!(
            meta.get("truncated").and_then(serde_json::Value::as_bool),
            Some(true),
            "budget cut must set truncated=true: {out}"
        );
        let at_line = meta
            .get("truncated_at_line")
            .and_then(serde_json::Value::as_u64)
            .expect("truncated_at_line missing");
        let total = meta
            .get("original_line_count")
            .and_then(serde_json::Value::as_u64)
            .expect("original_line_count missing");
        assert!(
            at_line >= 2 && at_line < total,
            "N inside (1, M): at_line={at_line}, M={total}: {out}"
        );
    }

    #[test]
    fn tool_read_budget_truncation_stays_under_requested_budget() {
        // Regression: `finalize_response` prepends a JSON view-meta header AFTER
        // budgeting the body. The body budget must subtract the header's tokens
        // so the rendered response (header + body) fits inside the user's ask.
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("big.rs");
        let mut src = String::new();
        for i in 0..400 {
            src.push_str(&format!("fn f{i}() {{\n    let l = {i};\n}}\n\n"));
        }
        std::fs::write(&p, src).unwrap();
        let budget = 500u64;
        let args = serde_json::json!({
            "paths": [p.to_str().unwrap()],
            "budget": budget
        });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&args, &cache, &session, false).expect("budget read ok");
        let meta = parse_first_line_json(&out).expect("view-meta JSON header expected");
        assert_eq!(
            meta.get("truncated").and_then(serde_json::Value::as_bool),
            Some(true),
            "test setup expected truncation: {out}"
        );
        let response_tokens = crate::types::estimate_tokens(out.len() as u64);
        assert!(
            response_tokens <= budget,
            "rendered response must fit in requested budget {budget} (got {response_tokens} tokens, {} bytes)",
            out.len()
        );
    }
}
