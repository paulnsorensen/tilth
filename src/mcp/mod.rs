use std::io::{self, BufRead, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, OnceLock};

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::cache::OutlineCache;
use crate::index::bloom::BloomFilterCache;
use crate::session::Session;
use crate::timeout::{self, spawn_with_timeout, SpawnFailure, ThreadTracker};

mod iso;
mod path_suffix;
mod tools;

use tools::{tool_definitions, tool_read, tool_search_v2, tool_write};

/// Shared dependencies passed through the request → dispatch pipeline.
#[derive(Clone)]
struct Services {
    cache: Arc<OutlineCache>,
    session: Arc<Session>,
    bloom: Arc<BloomFilterCache>,
    tracker: Arc<ThreadTracker>,
    client_profile: Arc<OnceLock<String>>,
    telemetry: Arc<crate::telemetry::TelemetrySink>,
}

impl Services {
    fn new() -> Self {
        Self {
            cache: Arc::new(OutlineCache::new()),
            session: Arc::new(Session::new()),
            bloom: Arc::new(BloomFilterCache::new()),
            tracker: Arc::new(ThreadTracker::new()),
            client_profile: Arc::new(OnceLock::new()),
            telemetry: Arc::new(crate::telemetry::TelemetrySink::new()),
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

    fn telemetry(&self) -> &crate::telemetry::TelemetrySink {
        &self.telemetry
    }

    /// The telemetry client key: the client-declared name normalized at
    /// `initialize`, or a stable fallback when the host omitted `clientInfo`.
    fn client_key(&self) -> &str {
        self.client_profile
            .get()
            .map_or("unknown-client", String::as_str)
    }
}

// Sent to the LLM via the MCP `instructions` field during initialization.
// The string lives in prompts/mcp.md so it can be versioned and rendered as
// Markdown. AGENTS.md is regenerated from the same file via
// scripts/regen-agents-md.sh, keeping the human-facing copy in lockstep with
// what MCP hosts receive in the `instructions` field.
const SERVER_INSTRUCTIONS: &str = include_str!("../../prompts/mcp.md");

/// The cwd-guidance span in prompts/mcp.md. Exact substring of the file,
/// guarded by `cwd_guidance_spans_present` so an edit that drops or reworks the explicit-cwd directive fails the test rather than
/// silently changing the model-facing cwd contract.
#[cfg(test)]
const CWD_PATHS_SPAN: &str = "DO NOT omit `cwd`: set it to the absolute checkout directory on every call. Relative paths/scopes anchor there; absolute paths pass through. The server cannot see your shell cwd; `..` in relative paths is refused.";

/// The complete MCP `instructions` string served at `initialize`.
fn build_instructions() -> String {
    SERVER_INSTRUCTIONS.trim_end().to_string()
}

/// Change the process working directory, logging failures to stderr.
///
/// A swallowed chdir leaves the server searching the wrong root while every
/// later tool call still looks successful, so the operator needs a grep-able
/// line when the configured root is unusable.
fn chdir_or_log(path: &Path) {
    if let Err(e) = std::env::set_current_dir(path) {
        eprintln!(
            "tilth: failed to set working directory to {}: {e}",
            path.display()
        );
    }
}

/// The current working directory, logging to stderr and falling back to an
/// empty path when `current_dir` fails (rare, but previously swallowed silently).
fn current_dir_or_log() -> PathBuf {
    match std::env::current_dir() {
        Ok(dir) => dir,
        Err(e) => {
            eprintln!("tilth: failed to read current dir: {e}");
            PathBuf::new()
        }
    }
}

/// Normalizes an MCP client's declared name into a stable telemetry client
/// key: lowercase, internal whitespace runs collapsed to `-`. Absent or blank
/// names fall back to a stable placeholder when a host omits `clientInfo`.
fn normalize_client_key(name: Option<&str>) -> String {
    match name.map(str::trim) {
        Some(n) if !n.is_empty() => n
            .to_lowercase()
            .split_whitespace()
            .collect::<Vec<_>>()
            .join("-"),
        _ => "unknown-client".to_string(),
    }
}

/// MCP server over stdio. Always serves the edit surface: `tilth_write` is
/// registered and `tilth_read` prints whole-file-tag (`[path#TAG]` + numbered
/// lines) output.
///
/// `scope` overrides the default search root. When provided, tilth chdir's to it
/// at startup so all tools, git commands, and searches use the correct project root.
/// This fixes MCP hosts that launch tilth with cwd=/ (e.g., Codex).
pub fn run(scope: Option<&Path>) -> io::Result<()> {
    // Resolve the project root and chdir to it.
    // Priority: explicit --scope > package_root(cwd) > cwd. The server never
    // chdirs on client roots — path anchoring is driven entirely by the
    // per-call `cwd` parameter.
    if let Some(s) = scope {
        if s.is_dir() {
            chdir_or_log(s);
        }
    } else {
        let cwd = current_dir_or_log();
        if let Some(root) = crate::lang::package_root(&cwd) {
            chdir_or_log(root);
        }
    }
    let services = Services::new();
    let stdin = io::stdin();
    let stdout = io::stdout();
    serve(stdin.lock(), stdout.lock(), &services)
}

/// The JSON-RPC stdio loop, extracted from [`run`] for testability. Reads one
/// message per line, dispatches requests through [`handle_request`], and writes
/// each response. The server never initiates a request of its own — there is no
/// `roots/list` handshake, so nothing is emitted that the client did not ask for.
fn serve(reader: impl BufRead, mut writer: impl Write, services: &Services) -> io::Result<()> {
    for line in reader.lines() {
        let line = match line {
            Ok(line) => line,
            Err(e) => {
                eprintln!("tilth: stdin read error, shutting down: {e}");
                return Err(e);
            }
        };
        if line.is_empty() {
            continue;
        }

        // Parse as generic JSON first — could be a request or a notification.
        let msg: Value = match serde_json::from_str(&line) {
            Ok(v) => v,
            Err(e) => {
                write_error(&mut writer, None, -32700, &format!("parse error: {e}"))?;
                continue;
            }
        };

        // Must have "method" to be a request or notification.
        let method = match msg.get("method").and_then(Value::as_str) {
            Some(m) => m.to_string(),
            None => continue, // Not a request — skip (could be an unexpected response)
        };

        let id = msg.get("id").cloned();
        if id.is_none() {
            // Notifications have no id — silently drop them per JSON-RPC spec.
            continue;
        }

        let params = msg.get("params").cloned().unwrap_or(Value::Null);

        let req = JsonRpcRequest {
            _jsonrpc: "2.0".to_string(),
            id,
            method,
            params,
        };

        let response = handle_request(&req, services);
        serde_json::to_writer(&mut writer, &response)?;
        writer.write_all(b"\n")?;
        writer.flush()?;
    }

    Ok(())
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
    match req.method.as_str() {
        "initialize" => {
            let instructions = build_instructions();
            let client_name = req
                .params
                .get("clientInfo")
                .and_then(|c| c.get("name"))
                .and_then(Value::as_str);
            let _ = services
                .client_profile
                .set(normalize_client_key(client_name));
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
                "tools": tool_definitions()
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

fn append_nudge(body: String, tip: Option<String>) -> String {
    let Some(tip) = tip else {
        return body;
    };
    // Exactly one blank line between the body and the tip, whatever trailing
    // newlines the body carries.
    let mut out = body.trim_end_matches('\n').to_string();
    out.push_str("\n\n");
    out.push_str(&tip);
    out
}

/// Execute a tool by name with the given arguments. Returns formatted output or error string.
/// No classifier involved — the caller specifies the tool explicitly.
fn dispatch_tool(tool: &str, args: &Value, services: &Services) -> Result<String, String> {
    // Budget validation only applies to tools that honour the budget param.
    // tilth_write ignores budget; rejecting budget:0 for it
    // produces a confusing read-oriented error on non-read operations.
    let budget_aware = tool == "tilth_read";
    if budget_aware {
        if let Some(b) = args.get("budget") {
            if !matches!(b.as_u64(), Some(n) if n >= 1) {
                return Err(format!(
                    "budget must be a positive integer ≥ 1 (got {b}); omit it for the default {}",
                    crate::budget::DEFAULT_BUDGET
                ));
            }
        }
    }
    let result = match tool {
        "tilth_read" => tool_read(args, services.cache(), services.session()),
        "tilth_search" => dispatch_search_v2(args, services),
        "tilth_write" => tool_write(args, services.session(), services.bloom(), services.cache()),
        _ => Err(format!("unknown tool: {tool}")),
    };
    // Observe every dispatch — an errored call still advances/resets the
    // batch streak — but only successful responses can carry a tip.
    // `tilth_search` is exempt: its response is pure JSON and drops any tip,
    // so observing it would break an unrelated streak for a response nobody
    // can read a tip from.
    let tip = if tool == "tilth_search" {
        None
    } else {
        services.session().nudge(tool, args, result.is_ok())
    };
    result.map(|body| append_nudge(body, tip))
}

fn dispatch_search_v2(args: &Value, services: &Services) -> Result<String, String> {
    let client = services.client_key();
    let worktree = args.get("cwd").and_then(Value::as_str).unwrap_or_default();
    tool_search_v2(
        args,
        services.cache(),
        services.session(),
        services.bloom(),
        services.telemetry(),
        client,
        worktree,
    )
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
        Err(SpawnFailure::Timeout) => {
            eprintln!(
                "tilth: tool '{tool_name}' timed out after {}s",
                timeout.as_secs()
            );
            Err(format!(
                "tool timed out after {}s — the operation took too long. \
                 Try: reduce scope, use section instead of full, or set \
                 TILTH_TIMEOUT=<seconds> to increase the limit.",
                timeout.as_secs()
            ))
        }
        Err(SpawnFailure::Panic) => {
            eprintln!("tilth: tool '{tool_name}' panicked during execution");
            Err("tool panicked during execution".into())
        }
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

    /// Tool handlers now require an absolute `cwd`. Injects a default so the
    /// behavior tests below can focus on the handler under test: absolute paths
    /// in the fixtures pass through unchanged; relative names anchor under this
    /// root. Tests that assert the missing-`cwd` refusal live in the per-tool
    /// modules (read.rs, search.rs, …) and build their args without this helper.
    fn tc(args: &Value) -> Value {
        let mut a = args.clone();
        if a.get("cwd").is_none() {
            a["cwd"] = serde_json::json!("/");
        }
        a
    }

    /// The nudge must ride the REAL dispatch path: two consecutive
    /// single-item reads through `dispatch_tool` end with the tip after
    /// exactly one blank line, whatever trailing newlines the body carries.
    /// Guards against the append being dropped from `dispatch_tool` (the
    /// other nudge tests exercise `Session` directly and would stay green).
    #[test]
    fn dispatch_tool_appends_batch_nudge_after_one_blank_line() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("a.rs"), "fn a() {}\n").unwrap();
        std::fs::write(dir.path().join("b.rs"), "fn b() {}\n").unwrap();
        let cwd = dir.path().to_str().unwrap();
        let services = Services::new();

        let first = dispatch_tool(
            "tilth_read",
            &serde_json::json!({ "paths": ["a.rs"], "cwd": cwd }),
            &services,
        )
        .unwrap();
        assert!(
            !first.contains("TIP:"),
            "first call must not nudge: {first}"
        );

        let second = dispatch_tool(
            "tilth_read",
            &serde_json::json!({ "paths": ["b.rs"], "cwd": cwd }),
            &services,
        )
        .unwrap();
        let tip = "TIP: batch into one call — paths: [\"a.rs\", \"b.rs\"].";
        assert!(
            second.ends_with(&format!("\n\n{tip}")),
            "tip must follow exactly one blank line: {second:?}"
        );
        assert!(
            !second.ends_with(&format!("\n\n\n{tip}")),
            "double blank line before tip: {second:?}"
        );
    }

    /// A read inside `cwd` prints the cwd-relative path in its `[path#TAG]`
    /// line, and that relative path plus tag round-trips into `tilth_write`.
    #[test]
    fn relative_read_tag_round_trips_into_write() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir(dir.path().join("sub")).unwrap();
        std::fs::write(dir.path().join("sub/a.txt"), "alpha\nbeta\n").unwrap();
        let cwd = dir.path().to_str().unwrap();
        let services = Services::new();

        let read = dispatch_tool(
            "tilth_read",
            &serde_json::json!({ "paths": ["sub/a.txt"], "cwd": cwd }),
            &services,
        )
        .unwrap();
        assert!(!read.contains(cwd), "in-cwd path must be relative: {read}");
        let tag = read
            .lines()
            .find_map(|l| l.strip_prefix("[sub/a.txt#")?.strip_suffix(']'))
            .unwrap_or_else(|| panic!("relative tag line missing: {read}"));

        let write = dispatch_tool(
            "tilth_write",
            &serde_json::json!({
                "cwd": cwd,
                "edits": [{ "path": "sub/a.txt", "tag": tag,
                    "ops": [{ "op": "replace_text", "old": "beta", "new": "BETA" }] }]
            }),
            &services,
        )
        .unwrap();
        assert!(write.starts_with("## sub/a.txt\napplied"), "{write}");
        assert!(!write.contains(cwd), "{write}");
        assert_eq!(
            std::fs::read_to_string(dir.path().join("sub/a.txt")).unwrap(),
            "alpha\nBETA\n"
        );
    }

    /// File content that mentions the absolute cwd must never be rewritten:
    /// only path labels (headers, tags, receipts) render relative.
    #[test]
    fn content_containing_cwd_is_not_rewritten_in_any_view() {
        let dir = tempfile::tempdir().unwrap();
        let cwd = dir.path().to_str().unwrap();
        let source = format!("pub const ROOT: &str = \"{cwd}/data\";\n\npub fn f() {{}}\n");
        std::fs::write(dir.path().join("c.rs"), &source).unwrap();
        let needle = format!("{cwd}/data");
        let services = Services::new();

        for mode in ["signature", "stripped", "full"] {
            let out = dispatch_tool(
                "tilth_read",
                &serde_json::json!({ "paths": ["c.rs"], "mode": mode, "cwd": cwd }),
                &services,
            )
            .unwrap();
            assert!(
                out.contains(&needle),
                "{mode} view lost the absolute path: {out}"
            );
            let header = out.lines().next().unwrap();
            assert!(
                !header.contains(cwd),
                "{mode} header must be relative: {header}"
            );
        }

        let read = dispatch_tool(
            "tilth_read",
            &serde_json::json!({ "paths": ["c.rs"], "cwd": cwd }),
            &services,
        )
        .unwrap();
        let tag = read
            .lines()
            .find_map(|l| l.strip_prefix("[c.rs#")?.strip_suffix(']'))
            .unwrap_or_else(|| panic!("relative tag line missing: {read}"));
        let err = dispatch_tool(
            "tilth_write",
            &serde_json::json!({
                "cwd": cwd,
                "edits": [{ "path": "c.rs", "tag": tag,
                    "ops": [{ "op": "replace_text", "old": format!("{cwd}/missing"), "new": "x" }] }]
            }),
            &services,
        )
        .unwrap_err();
        assert!(
            err.contains(&format!("{cwd}/missing")),
            "error must echo `old` verbatim: {err}"
        );
    }

    /// A path outside `cwd` stays absolute in read and write output.
    #[test]
    fn outside_cwd_path_stays_absolute() {
        let cwd_dir = tempfile::tempdir().unwrap();
        let other = tempfile::tempdir().unwrap();
        let outside = other.path().join("o.txt");
        std::fs::write(&outside, "one\ntwo\n").unwrap();
        let cwd = cwd_dir.path().to_str().unwrap();
        let abs = outside.to_str().unwrap();
        let services = Services::new();

        let read = dispatch_tool(
            "tilth_read",
            &serde_json::json!({ "paths": [abs], "cwd": cwd }),
            &services,
        )
        .unwrap();
        let tag_line = read
            .lines()
            .find(|l| l.starts_with('[') && l.contains('#'))
            .unwrap();
        assert!(tag_line.starts_with(&format!("[{abs}#")), "{read}");
        let tag = tag_line.rsplit_once('#').unwrap().1.trim_end_matches(']');

        let write = dispatch_tool(
            "tilth_write",
            &serde_json::json!({
                "cwd": cwd,
                "edits": [{ "path": abs, "tag": tag,
                    "ops": [{ "op": "replace_text", "old": "two", "new": "TWO" }] }]
            }),
            &services,
        )
        .unwrap();
        assert!(write.starts_with(&format!("## {abs}\napplied")), "{write}");
    }

    #[test]
    fn unregistered_tool_names_get_the_plain_unknown_tool_error() {
        let services = Services::new();
        let args = serde_json::json!({ "cwd": "/" });
        for tool in ["tilth_edit", "tilth_grok", "tilth_bogus"] {
            let err = dispatch_tool(tool, &args, &services).unwrap_err();
            assert_eq!(err, format!("unknown tool: {tool}"));
        }
    }

    #[test]
    fn repeated_queries_and_follows_keep_the_json_budget_envelope() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("a.rs"),
            "fn foo() {}\nfn caller() { foo(); }\n",
        )
        .unwrap();
        let cwd = dir.path().to_str().unwrap();
        let services = Services::new();
        let args = serde_json::json!({"queries": [{"query": "foo"}], "cwd": cwd});
        let initial = dispatch_tool("tilth_search", &args, &services).unwrap();
        let initial: Value = serde_json::from_str(&initial).unwrap();
        let hint = &initial["hints"][0];
        for entry in [
            serde_json::json!({"follow": hint}),
            serde_json::json!({"follow": hint}),
            serde_json::json!({"query": "foo"}),
            serde_json::json!({"query": "bar"}),
        ] {
            let output = dispatch_tool(
                "tilth_search",
                &serde_json::json!({"queries": [entry], "cwd": cwd, "budget": 1000}),
                &services,
            )
            .unwrap();
            let payload: Value = serde_json::from_str(&output).expect("one JSON envelope");
            assert_eq!(payload["results"].as_array().unwrap().len(), 1);
            assert!(!output.contains("TIP:"));
            assert!(crate::types::estimate_tokens(output.len() as u64) <= 1000);
        }
    }

    #[test]
    fn rejected_search_selectors_error() {
        let dir = tempfile::tempdir().unwrap();
        let cwd = dir.path().to_str().unwrap();
        let services = Services::new();
        for selector in ["kind", "expand", "context"] {
            let mut args = serde_json::json!({"queries": [{"query": "foo"}], "cwd": cwd});
            args[selector] = serde_json::json!("symbol");
            assert!(dispatch_tool("tilth_search", &args, &services).is_err());
            args.as_object_mut().unwrap().remove(selector);
            args["queries"][0][selector] = serde_json::json!("symbol");
            assert!(dispatch_tool("tilth_search", &args, &services).is_err());
        }
    }

    #[test]
    fn instructions_teach_replace_text_before_line_ops() {
        let rt = SERVER_INSTRUCTIONS
            .find("`replace_text` swaps")
            .expect("replace_text taught in edit instructions");
        let line_ops = SERVER_INSTRUCTIONS
            .find("line ops use copied integer")
            .expect("line ops taught in edit instructions");
        assert!(rt < line_ops, "replace_text must lead the op teaching");
    }

    /// An errored dispatch of a different tool must break the streak — the
    /// old `result.map` shortcut skipped observation entirely and let the
    /// tip claim a consecutive pair that never happened.
    #[test]
    fn errored_dispatch_of_different_tool_resets_batch_nudge_streak() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("a.rs"), "fn a() {}\n").unwrap();
        std::fs::write(dir.path().join("b.rs"), "fn b() {}\n").unwrap();
        let cwd = dir.path().to_str().unwrap();
        let services = Services::new();

        for path in ["a.rs", "b.rs"] {
            if path == "b.rs" {
                // Errored non-batchable call between the two reads.
                dispatch_tool(
                    "tilth_write",
                    &serde_json::json!({ "edits": [] }),
                    &services,
                )
                .expect_err("write without cwd must error");
            }
            let body = dispatch_tool(
                "tilth_read",
                &serde_json::json!({ "paths": [path], "cwd": cwd }),
                &services,
            )
            .unwrap();
            assert!(
                !body.contains("TIP:"),
                "streak must reset across the errored call: {body}"
            );
        }
    }

    /// A `tilth_search` dispatch must not touch nudge state at all: its JSON
    /// response can never carry a tip, so observing it would break a read
    /// streak that the agent never actually broke.
    #[test]
    fn search_dispatch_leaves_nudge_state_untouched() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("a.rs"), "fn a() {}\n").unwrap();
        std::fs::write(dir.path().join("b.rs"), "fn b() {}\n").unwrap();
        let cwd = dir.path().to_str().unwrap();
        let services = Services::new();

        dispatch_tool(
            "tilth_read",
            &serde_json::json!({ "paths": ["a.rs"], "cwd": cwd }),
            &services,
        )
        .unwrap();
        let search = dispatch_tool(
            "tilth_search",
            &serde_json::json!({ "queries": [{ "query": "a" }], "cwd": cwd }),
            &services,
        )
        .unwrap();
        assert!(
            !search.contains("TIP:"),
            "search responses never carry a tip: {search}"
        );

        let second = dispatch_tool(
            "tilth_read",
            &serde_json::json!({ "paths": ["b.rs"], "cwd": cwd }),
            &services,
        )
        .unwrap();
        assert!(
            second.ends_with("\n\nTIP: batch into one call — paths: [\"a.rs\", \"b.rs\"]."),
            "the read streak must survive the interleaved search: {second:?}"
        );
    }

    /// Integration seam: the real JSON-RPC dispatch path (`dispatch_tool`) must
    /// refuse a missing `cwd` for EVERY advertised path-taking tool, not just the
    /// per-handler unit helpers. Each tool is given its other required params so
    /// the refusal is specifically the cwd teaching error, not a different
    /// missing-param error. Guards against a future tool being wired into
    /// dispatch without the `require_cwd` gate.
    #[test]
    fn dispatch_refuses_missing_cwd_for_every_path_tool() {
        let services = Services::new();
        let cases = [
            ("tilth_read", serde_json::json!({ "paths": ["x.rs"] })),
            (
                "tilth_search",
                serde_json::json!({ "queries": [{ "query": "x" }] }),
            ),
            (
                "tilth_write",
                serde_json::json!({ "edits": [{ "path": "a.rs", "ops": [{ "op": "delete", "start": 1, "end": 1 }] }] }),
            ),
        ];
        for (tool, args) in cases {
            let err = dispatch_tool(tool, &args, &services)
                .expect_err(&format!("{tool} must refuse a missing cwd, got Ok"));
            assert!(
                err.contains("cwd") && err.contains("absolute checkout directory"),
                "{tool} dispatch must refuse missing cwd with the teaching error: {err}"
            );
        }
    }

    #[test]
    fn dispatch_search_invalid_budget_records_telemetry() {
        let temp = tempfile::tempdir().unwrap();
        let mut services = Services::new();
        services.telemetry = Arc::new(crate::telemetry::TelemetrySink::for_test(temp.path()));
        let args = serde_json::json!({"cwd": temp.path(), "queries": [{"query": "anything"}], "budget": 0});
        let err = dispatch_tool("tilth_search", &args, &services).unwrap_err();
        assert!(err.contains("budget"));
        let log = std::fs::read_to_string(temp.path().join("current.jsonl")).unwrap();
        assert_eq!(log.lines().count(), 1);
        let record: Value = serde_json::from_str(log.lines().next().unwrap()).unwrap();
        assert_eq!(record["outcome"], "error");
        assert_eq!(record["error_class"], "bad_budget");
    }

    // -- serve: no unsolicited roots/list handshake ---------------------------

    /// After the roots removal, `serve` must never emit a request of its own.
    /// Feeding an `initialize` that advertises the `roots` capability must yield
    /// exactly one message — the initialize response — and never a `roots/list`
    /// request. Guards the deleted post-initialize handshake.
    #[test]
    fn serve_emits_no_roots_list_after_initialize() {
        let services = Services::new();
        let input = concat!(
            r#"{"jsonrpc":"2.0","id":1,"method":"initialize","#,
            r#""params":{"capabilities":{"roots":{"listChanged":true}}}}"#,
            "\n"
        );
        let mut out: Vec<u8> = Vec::new();
        serve(input.as_bytes(), &mut out, &services).expect("serve drains cleanly on EOF");
        let out = String::from_utf8(out).expect("utf8 output");
        assert!(
            !out.contains("roots/list"),
            "server must not emit a roots/list request: {out}"
        );
        // Exactly one JSON message (the initialize response) is written.
        let lines: Vec<&str> = out.lines().filter(|l| !l.is_empty()).collect();
        assert_eq!(
            lines.len(),
            1,
            "exactly one response line expected: {out:?}"
        );
        let resp: serde_json::Value = serde_json::from_str(lines[0]).expect("valid JSON response");
        assert_eq!(
            resp["id"], 1,
            "the one message must be the initialize response"
        );
        assert!(
            resp.get("result").is_some(),
            "initialize must succeed: {resp}"
        );
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
    // These tests pin the per-mode MCP `instructions` strings to their byte
    // shapes so drift is flagged loudly: any prompt edit must update the
    // assertions below.

    #[test]
    fn server_instructions_byte_lock() {
        assert_eq!(
            SERVER_INSTRUCTIONS.len(),
            1978,
            "SERVER_INSTRUCTIONS byte count drifted from baseline"
        );
        assert!(SERVER_INSTRUCTIONS.starts_with(
            "tilth — code intelligence MCP server. Replaces grep, cat, and host edit tools.\nDO NOT use shell for repo content reads (cat/head/tail/sed/grep/rg) and DO NOT use host Edit/Write; use tilth tools. Shell is for directory browsing (ls/find), Git review/history, tests, builds, and non-file operations."
        ));
        assert!(SERVER_INSTRUCTIONS.ends_with("DO NOT re-read expanded search content."));
        assert!(
            !SERVER_INSTRUCTIONS.contains("\n\n\n"),
            "SERVER_INSTRUCTIONS must not introduce triple newlines"
        );
        assert!(SERVER_INSTRUCTIONS.contains(
            "edits: [{path: \"src/a.rs\", tag: \"1A2B\", ops: [...]}, {path: \"src/b.rs\""
        ));
        assert!(
            SERVER_INSTRUCTIONS.contains("line ops use copied integer"),
            "op grammar pointer must remain in SERVER_INSTRUCTIONS"
        );
        assert!(
            SERVER_INSTRUCTIONS.contains("must escape tabs/newlines"),
            "control-char escape rule must remain in SERVER_INSTRUCTIONS"
        );
        assert!(
            SERVER_INSTRUCTIONS
                .contains("DO NOT use `mode: full` to edit; read the section you change."),
            "section-read edit guidance must remain in SERVER_INSTRUCTIONS"
        );
        assert!(
            !SERVER_INSTRUCTIONS.contains("mcp__"),
            "instructions must use protocol tool names, not client-specific prefixes"
        );
    }

    /// ADR-003's hard surface cap. The spec elevated "the cap never yields" to
    /// a quality gate but shipped no guard.
    ///
    /// Drives `serve` and counts the bytes a client actually
    /// receives — envelopes, `serverInfo`, and JSON escaping included. An
    /// earlier version of this guard summed `SERVER_INSTRUCTIONS.len()` with
    /// the tool JSON and reported ~200 chars of headroom that did not exist:
    /// it omitted the envelopes and mixed byte and char counts under one cap.
    #[test]
    fn mcp_surface_stays_within_cap() {
        const CAP: usize = 13_779;
        let services = Services::new();
        let input = concat!(
            r#"{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}"#,
            "\n",
            r#"{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}"#,
            "\n"
        );
        let mut out: Vec<u8> = Vec::new();
        serve(input.as_bytes(), &mut out, &services).expect("serve drains cleanly on EOF");
        let out = String::from_utf8(out).expect("utf8 output");
        let surface: usize = out.lines().filter(|l| !l.is_empty()).map(str::len).sum();
        assert!(
            surface <= CAP,
            "MCP surface is {surface} bytes, over the {CAP} cap by {}. \
             Trim a description — the cap does not yield.",
            surface.saturating_sub(CAP)
        );
    }

    #[test]
    fn incremental_write_reuses_tree_through_production_requests() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("incremental.rs");
        let before = format!("// {}\nfn main() {{ Some(café); }}\n", dir.path().display());
        let after = before.replace("café", "thé");
        std::fs::write(&path, &before).unwrap();
        let services = Services::new();
        let old = services.cache().get_or_parse(&path).unwrap();
        let old_allocation = (
            old.content().as_ptr(),
            old.content().len(),
            old.content().capacity(),
        );
        let replacement_input_allocation = (after.as_ptr(), after.len(), after.capacity());
        let old_shape = old.tree().root_node().to_sexp();
        let witness = crate::lang::treesitter::ParseWitness::new(&after);
        let read =
            serde_json::json!({"cwd": dir.path(), "paths": ["incremental.rs"], "mode": "full"});
        dispatch_tool("tilth_read", &read, &services).unwrap();
        let tag = format!("{:04X}", crate::edit::tag::compute_file_hash(&before));
        let write = serde_json::json!({"cwd": dir.path(), "edits": [{
            "path": "incremental.rs", "tag": tag,
            "ops": [{"op": "replace_text", "old": "café", "new": "thé"}]
        }]});
        let result = dispatch_tool("tilth_write", &write, &services).unwrap();
        assert!(result.contains("applied"), "{result}");
        let read = dispatch_tool("tilth_read", &read, &services).unwrap();
        assert!(read.contains("Some(thé)"), "{read}");
        let search = serde_json::json!({"cwd": dir.path(), "queries": [{
            "pattern": "Some($A)", "language": "rust"
        }]});
        let result = dispatch_tool("tilth_search", &search, &services).unwrap();
        let result: Value = serde_json::from_str(&result).unwrap();
        assert_eq!(result["results"][0]["total_matches"], 1);
        // Compact match: [start_line, end_line, start_byte, end_byte, captures].
        let capture = &result["results"][0]["items"][0]["matches"][0][4]["A"][0];
        let start = capture[2].as_u64().unwrap() as usize;
        let end = capture[3].as_u64().unwrap() as usize;
        assert_eq!(&after[start..end], "thé");
        assert_eq!(
            witness.count(),
            0,
            "a verified warm write must not fully reparse"
        );
        assert_eq!(witness.incremental_count(), 1);
        let current = services.cache().get_or_parse(&path).unwrap();
        assert_eq!(current.content().as_str(), after);
        assert!(!Arc::ptr_eq(&old, &current));
        let current_allocation = (
            current.content().as_ptr(),
            current.content().len(),
            current.content().capacity(),
        );
        assert_eq!(current_allocation.1, replacement_input_allocation.1);
        assert!(current_allocation.2 >= current_allocation.1);
        assert_eq!(old.content().as_str(), before);
        assert_eq!(
            (
                old.content().as_ptr(),
                old.content().len(),
                old.content().capacity(),
            ),
            old_allocation,
            "publishing a replacement must retain the old reader allocation"
        );
        assert_eq!(old.tree().root_node().to_sexp(), old_shape);
        let warm = services.cache().get_or_parse(&path).unwrap();
        assert!(Arc::ptr_eq(&current, &warm));
        assert_eq!(
            (
                warm.content().as_ptr(),
                warm.content().len(),
                warm.content().capacity(),
            ),
            current_allocation
        );
    }

    fn incremental_request(services: &Services, path: &std::path::Path, ops: &Value) -> Value {
        let read = serde_json::json!({
            "cwd": path.parent().unwrap(), "paths": [path], "mode": "full"
        });
        dispatch_tool("tilth_read", &read, services).unwrap();
        let source = std::fs::read_to_string(path).unwrap();
        let tag = crate::edit::tag::format_tag(crate::edit::tag::compute_file_hash(&source));
        serde_json::json!({
            "cwd": path.parent().unwrap(), "edits": [{"path": path, "tag": tag, "ops": ops}]
        })
    }

    fn assert_same_nodes(actual: tree_sitter::Node<'_>, expected: tree_sitter::Node<'_>) {
        assert_eq!(actual.kind(), expected.kind());
        assert_eq!(actual.range(), expected.range());
        assert_eq!(actual.is_named(), expected.is_named());
        assert_eq!(actual.is_error(), expected.is_error());
        assert_eq!(actual.is_missing(), expected.is_missing());
        assert_eq!(actual.child_count(), expected.child_count());
        let mut actual_cursor = actual.walk();
        let mut expected_cursor = expected.walk();
        for (actual, expected) in actual
            .children(&mut actual_cursor)
            .zip(expected.children(&mut expected_cursor))
        {
            assert_same_nodes(actual, expected);
        }
    }

    #[test]
    fn incremental_write_coordinates_match_fresh_parses_and_captures() {
        for (language, name, comment, body, whole) in [
            (
                "rust",
                "source.rs",
                "//",
                "fn main() { Some(\"café\"); }",
                "fn other() { Some(\"whole\"); }",
            ),
            (
                "typescript",
                "source.ts",
                "//",
                "wrap(\"café\");",
                "wrap(\"whole\");",
            ),
            (
                "python",
                "source.py",
                "#",
                "wrap(\"café\")",
                "wrap(\"whole\")",
            ),
        ] {
            let dir = tempfile::tempdir().unwrap();
            let path = dir.path().join(name);
            let header = format!("{comment} {}\n", dir.path().display());
            let mut before = format!("{header}{body}\n");
            std::fs::write(&path, &before).unwrap();
            let services = Services::new();
            let pattern = if language == "rust" {
                "Some($A)"
            } else {
                "wrap($A)"
            };
            let search = serde_json::json!({
                "cwd": dir.path(), "queries": [{"pattern": pattern, "language": language}]
            });
            let variants = [
                serde_json::json!({"op": "replace_text", "old": "é", "new": "ê"}),
                serde_json::json!({"op": "replace_text", "old": "ê", "new": "©"}),
                serde_json::json!({"op": "insert_before", "line": 2, "content": format!("{comment} 火\n{comment} é")}),
                serde_json::json!({"op": "delete", "start": 2, "end": 3}),
                serde_json::json!({"op": "replace_text", "old": "\"caf©\"", "new": "\n    \"火\"\n"}),
                serde_json::json!({"op": "replace_text", "old": "\n    \"火\"\n", "new": "\"tea\""}),
                serde_json::json!({"op": "replace", "start": 1, "end": 2, "content": format!("{header}{whole}")}),
                serde_json::json!({"op": "replace", "start": 1, "end": 2, "content": ""}),
            ];
            for (index, op) in variants.into_iter().enumerate() {
                let old = services.cache().get_or_parse(&path).unwrap();
                let fresh_old = OutlineCache::new().get_or_parse(&path).unwrap();
                let request = incremental_request(&services, &path, &serde_json::json!([op]));
                let expected_source = match index {
                    0 => before.replace('é', "ê"),
                    1 => before.replace('ê', "©"),
                    2 => before.replacen('\n', &format!("\n{comment} 火\n{comment} é\n"), 1),
                    3 => format!("{header}{}\n", body.replace('é', "©")),
                    4 => before.replace("\"caf©\"", "\n    \"火\"\n"),
                    5 => before.replace("\n    \"火\"\n", "\"tea\""),
                    6 => format!("{header}{whole}\n"),
                    _ => String::new(),
                };
                let witness = (index != 7)
                    .then(|| crate::lang::treesitter::ParseWitness::new(&expected_source));
                let result = dispatch_tool("tilth_write", &request, &services).unwrap();
                assert!(result.contains("applied"), "{result}");
                let after = std::fs::read_to_string(&path).unwrap();
                assert_ne!(after, before, "{language} edit {index}");
                let current = services.cache().get_or_parse(&path).unwrap();
                assert_eq!(current.content().as_str(), after);
                assert_eq!(after, expected_source);
                if let Some(witness) = witness {
                    assert_eq!(witness.count(), 0, "{language} edit {index}");
                    assert_eq!(witness.incremental_count(), 1, "{language} edit {index}");
                }
                let fresh = OutlineCache::new().get_or_parse(&path).unwrap();
                assert_same_nodes(current.tree().root_node(), fresh.tree().root_node());
                assert_same_nodes(old.tree().root_node(), fresh_old.tree().root_node());
                assert_eq!(old.content().as_str(), before);
                let actual: Value = serde_json::from_str(
                    &dispatch_tool("tilth_search", &search, &services).unwrap(),
                )
                .unwrap();
                let expected: Value = serde_json::from_str(
                    &dispatch_tool("tilth_search", &search, &Services::new()).unwrap(),
                )
                .unwrap();
                assert_eq!(
                    actual["results"][0]["items"],
                    expected["results"][0]["items"]
                );
                assert_eq!(
                    actual["results"][0]["items"].as_array().unwrap().len(),
                    usize::from(index != 7),
                    "{language} edit {index}",
                );
                before = after;
            }
        }
    }

    #[test]
    fn incremental_write_external_replacement_during_parse_cannot_publish() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("race.rs");
        let before = format!("// {}\nfn before() {{}}\n", dir.path().display());
        let after = before.replace("before", "written");
        let external = before.replace("before", "external");
        std::fs::write(&path, &before).unwrap();
        let services = Services::new();
        let old = services.cache().get_or_parse(&path).unwrap();
        let request = incremental_request(
            &services,
            &path,
            &serde_json::json!([{
                "op": "replace_text", "old": "before", "new": "written"
            }]),
        );
        let witness = crate::lang::treesitter::ParseWitness::new(&after);
        let external_witness = crate::lang::treesitter::ParseWitness::new(&external);
        let (entered, resume) = witness.pause_next();
        std::thread::scope(|scope| {
            let writing = scope.spawn(|| dispatch_tool("tilth_write", &request, &services));
            entered
                .recv_timeout(std::time::Duration::from_secs(5))
                .unwrap();
            crate::util::atomic_write_bytes(&path, external.as_bytes()).unwrap();
            let replacement = services.cache().get_or_parse(&path).unwrap();
            assert_eq!(replacement.content().as_str(), external);
            resume.send(()).unwrap();
            let result = writing.join().unwrap().unwrap();
            assert!(result.contains("applied"), "{result}");
            assert!(Arc::ptr_eq(
                &replacement,
                &services.cache().get_or_parse(&path).unwrap()
            ));
        });
        assert_eq!(old.content().as_str(), before);
        assert_eq!(witness.count(), 0);
        assert_eq!(witness.incremental_count(), 1);
        assert_eq!(external_witness.count(), 1);
        assert_eq!(external_witness.incremental_count(), 0);
    }

    #[test]
    fn documents_reuse_real_parses_across_production_requests() {
        let dir = tempfile::tempdir().unwrap();
        let source = format!("// {}\nfn document_leaf() {{}}\nfn document_target() {{ document_leaf(); }}\nfn document_caller() {{ document_target(); }}\n", dir.path().display());
        std::fs::write(dir.path().join("document.rs"), &source).unwrap();
        let witness = crate::lang::treesitter::ParseWitness::new(&source);
        let services = Services::new();
        let search =
            serde_json::json!({"cwd": dir.path(), "queries": [{"query": "document_target"}]});
        let result = dispatch_tool("tilth_search", &search, &services).unwrap();
        assert!(result.contains("document_target"));
        let cold = witness.count();
        assert_eq!(cold, 1, "each cacheable revision needs one full parse");
        dispatch_tool("tilth_search", &search, &services).unwrap();
        assert_eq!(
            witness.count(),
            cold,
            "unchanged search request reparses source"
        );
        let read =
            serde_json::json!({"cwd": dir.path(), "paths": ["document.rs"], "mode": "signature"});
        dispatch_tool("tilth_read", &read, &services).unwrap();
        assert_eq!(
            witness.count(),
            cold,
            "outline read reparses the shared snapshot"
        );
        let response: Value = serde_json::from_str(&result).unwrap();
        let hints = response["hints"].as_array().unwrap();
        assert!(
            !hints.is_empty(),
            "search must emit a followable hint: {hints:?}"
        );
        for hint in hints {
            let follow = serde_json::json!({"cwd": dir.path(), "queries": [{"follow": hint}]});
            for _ in 0..2 {
                let response = dispatch_tool("tilth_search", &follow, &services).unwrap();
                let response: Value = serde_json::from_str(&response).unwrap();
                assert_ne!(response["results"][0]["status"], "error");
                assert_eq!(
                    witness.count(),
                    cold,
                    "follow reparses the shared snapshot: {hint}"
                );
            }
        }
        let read_symbol =
            serde_json::json!({"cwd": dir.path(), "paths": ["document.rs#document_target"]});
        dispatch_tool("tilth_read", &read_symbol, &services).unwrap();
        assert_eq!(
            witness.count(),
            cold,
            "symbol read reparses the shared snapshot"
        );
    }

    #[test]
    fn documents_expanded_search_reuses_sibling_and_callee_trees() {
        for (file, body) in [
            ("member.rs", "struct Worker { value: i32 }\nimpl Worker {\n fn helper(&self) {}\n fn run(&self) { self.helper(); let _ = self.value; }\n}\n"),
            ("member.go", "package fixture\ntype Worker struct { value int }\nfunc (w *Worker) helper() {}\nfunc (w *Worker) run() { w.helper(); _ = w.value }\n"),
        ] {
            let dir = tempfile::tempdir().unwrap();
            let source = format!("// {}\n{body}", dir.path().display());
            std::fs::write(dir.path().join(file), &source).unwrap();
            let witness = crate::lang::treesitter::ParseWitness::new(&source);
            let services = Services::new();
            let read = serde_json::json!({"cwd": dir.path(), "paths": [file], "mode": "signature"});
            dispatch_tool("tilth_read", &read, &services).unwrap();
            assert_eq!(witness.count(), 1);
            for _ in 0..2 {
                let result = crate::search::search_symbol_expanded(
                    "run", dir.path(), services.cache(), &crate::session::Session::new(),
                    services.bloom(), 1, None, None, false, None,).unwrap();
                assert!(result.contains("-- calls --"), "{file}: {result}");
                if file == "member.rs" {
                    assert!(result.contains("-- siblings --"), "{file}: {result}");
                } else {
                    // Go methods remain top-level entries. Check receiver extraction directly.
                    let parsed = services.cache().get_or_parse(&dir.path().join(file)).unwrap();
                    let names = crate::search::siblings::extract_sibling_references_from_tree(
                        parsed.content(), parsed.lang, parsed.tree(), (5, 5),
                    );
                    assert_eq!(names, ["helper", "value"]);
                }
                assert!(result.contains("helper") && result.contains("value"));
                assert_eq!(witness.count(), 1, "expanded {file} reparses its snapshot");
            }
        }
    }

    #[test]
    fn documents_direct_reads_do_not_parse() {
        let dir = tempfile::tempdir().unwrap();
        let source = format!("// {}\nfn byte_only() {{}}\n", dir.path().display());
        std::fs::write(dir.path().join("direct.rs"), &source).unwrap();
        let witness = crate::lang::treesitter::ParseWitness::new(&source);
        let services = Services::new();
        for args in [
            serde_json::json!({"cwd": dir.path(), "paths": ["direct.rs"], "mode": "full"}),
            serde_json::json!({"cwd": dir.path(), "paths": ["direct.rs#2-2"]}),
            serde_json::json!({"cwd": dir.path(), "paths": ["direct.rs"]}),
        ] {
            let response = dispatch_tool("tilth_read", &args, &services).unwrap();
            assert!(response.contains("fn byte_only() {}"));
        }
        assert_eq!(witness.count(), 0);
    }

    #[test]
    fn documents_large_sources_keep_existing_search_and_target_results() {
        let dir = tempfile::tempdir().unwrap();
        let source = format!("fn large_leaf() {{}}\nfn large_target() {{ large_leaf(); }}\nfn large_caller() {{ large_target(); }}\n{}", "// a long source comment with enough bytes to exceed the parsed cache size boundary\n".repeat(6500));
        let path = dir.path().join("large.rs");
        assert!(source.len() > 500_000 && source.len() < 1_000_000);
        std::fs::write(&path, &source).unwrap();
        let services = Services::new();
        assert!(services.cache().get_or_parse(&path).is_none());
        let (target, _, _) =
            crate::search::target::resolve_by_path_line(&path, 2, services.cache()).unwrap();
        assert_eq!(target.name, "large_target");
        let search = serde_json::json!({"cwd": dir.path(), "queries": [{"query": "large_target"}]});
        let response = dispatch_tool("tilth_search", &search, &services).unwrap();
        let response: Value = serde_json::from_str(&response).unwrap();
        assert!(
            response["results"][0]["target"].is_null(),
            "symbol discovery keeps its existing 500 KB limit"
        );
        let read =
            serde_json::json!({"cwd": dir.path(), "paths": ["large.rs"], "mode": "signature"});
        let response = dispatch_tool("tilth_read", &read, &services).unwrap();
        assert!(response.contains("large_target"));
        assert!(services.cache().get_or_parse(&path).is_none());
    }

    /// AGENTS.md is the human-facing copy of the embedded prompt file,
    /// generated by scripts/regen-agents-md.sh. Reproduce the regen composition
    /// here and fail on drift so an edit to prompts/ without a regen (or vice
    /// versa) is caught.
    #[test]
    fn agents_md_matches_prompt_sources() {
        const AGENTS_MD: &str = include_str!("../../AGENTS.md");
        let expected = format!(
            "<!-- generated from prompts/mcp.md by scripts/regen-agents-md.sh — do not edit directly -->\n\n{SERVER_INSTRUCTIONS}\n"
        );
        assert_eq!(
            AGENTS_MD.trim_end(),
            expected.trim_end(),
            "AGENTS.md is out of sync with prompts/ — run ./scripts/regen-agents-md.sh"
        );
    }

    #[test]
    fn server_instructions_fit_character_cap() {
        assert!(
            SERVER_INSTRUCTIONS.trim_end().chars().count() <= 2048,
            "instructions exceed 2048 characters"
        );
    }

    #[test]
    fn build_instructions_serves_the_prompt_file() {
        let s = build_instructions();
        assert_eq!(s, SERVER_INSTRUCTIONS.trim_end());
        assert!(s.contains("tilth_write"));
        assert!(
            s.len() <= 2048,
            "instructions must fit the 2KB MCP field: {} bytes",
            s.len()
        );
    }

    // -- tilth_read tool: batch reads, suffix grammar, view modes ----------
    // Restored from pre-merge 3801a4c (dropped by the #35 upstream merge).
    // These guard every behavior the batch-only read revert restored.

    /// Helper: parse the first line of a `tool_read` response as JSON when the
    /// header is present. Returns `None` when the response body has no JSON
    /// header (full content with no since/view-meta).
    fn parse_first_line_json(out: &str) -> Option<serde_json::Value> {
        let first = out.lines().next()?;
        serde_json::from_str(first).ok()
    }

    #[test]
    fn tool_read_paths_bare_string_coerces_and_nudges() {
        // A bare-string `paths` coerces to a single-element array and reads
        // the file; the response teaches batching via a nudge note.
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("a.rs");
        std::fs::write(&p, "fn a() {}\n").unwrap();
        let args = serde_json::json!({ "paths": p.to_str().unwrap() });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&tc(&args), &cache, &session)
            .expect("bare-string paths must coerce and read, not error");
        assert!(
            out.contains("fn a()"),
            "coerced read must return file content: {out}"
        );
        assert!(
            out.contains("paths: [\"a.rs\", \"b.rs\"]"),
            "response must teach batching via a nudge note: {out}"
        );
    }

    #[test]
    fn tool_read_paths_non_string_element_shows_corrected_shape() {
        // Arrays with non-string elements still error, but now the error
        // shows the corrected shape rather than a bare type complaint.
        let args = serde_json::json!({ "paths": [{"bad": true}] });
        let cache = OutlineCache::new();
        let session = Session::new();
        let err = tool_read(&tc(&args), &cache, &session)
            .expect_err("non-string array element must still error");
        assert!(
            err.contains("paths: [\"a.rs\", \"b.rs\"]"),
            "error must show the corrected shape: {err}"
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
            tool_read(&tc(&args), &cache, &session).expect_err("unknown mode must be rejected");
        assert!(err.contains("unknown read mode"), "unexpected error: {err}");
        assert!(
            err.contains("auto, full, signature, stripped"),
            "error must name all valid modes: {err}"
        );
        assert!(
            err.contains("already tagged and editable"),
            "error must explain every read is already tagged: {err}"
        );
    }

    /// `mode: "edit"` was never a valid mode value — every read is already
    /// tagged and editable, so the error must redirect the caller rather than
    /// just name it "unknown".
    #[test]
    fn tool_read_mode_edit_teaches_server_mode() {
        let args = serde_json::json!({
            "paths": ["a.rs"],
            "mode": "edit"
        });
        let cache = OutlineCache::new();
        let session = Session::new();
        let err =
            tool_read(&tc(&args), &cache, &session).expect_err("mode: edit must still be rejected");
        assert!(
            err.contains("auto, full, signature, stripped"),
            "error must name valid modes: {err}"
        );
        assert!(
            err.contains("\"edit\" is not a mode"),
            "error must redirect \"edit\" to the mode-only clause, not just call it unknown: {err}"
        );
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

        let result = tool_read(&tc(&args), &cache, &session).expect("batch read must succeed");

        for i in 0..file_count {
            assert!(
                result.contains(&format!("content-of-file-{i}")),
                "output must contain content of file {i}"
            );
        }

        assert!(
            !result.contains("> Note: paths accepts an array"),
            "well-formed array paths must not carry the coercion nudge: {result}"
        );
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
        let out = tool_read(&tc(&args), &cache, &session).expect("batch full read ok");
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
        let out = tool_read(&tc(&args), &cache, &session)
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
        let out = tool_read(&tc(&args), &cache, &session)
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
        let out = tool_read(&tc(&args), &cache, &session).expect("mixed batch succeeds");

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
        let result = tool_read(&tc(&args), &cache, &session);
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
        let out = tool_read(&tc(&args), &cache, &session)
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
        // These tests anchor at cwd `/`, so labels drop the leading slash.
        let qualified = format!("{}#ghost_symbol", p2.strip_prefix("/").unwrap().display());
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
        let out = tool_read(&tc(&args), &cache, &session)
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

    // -- batch tool_read --------------------------------------------------------

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
        let out = tool_read(&tc(&args), &cache, &session).expect("suffix accepted");
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
        let out = tool_read(&tc(&args), &cache, &session).expect("heading suffix");
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
        let out = tool_read(&tc(&args), &cache, &session).expect("stub ok");
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
        let out = tool_read(&tc(&args), &cache, &session).expect("content ok");
        assert!(out.contains("hello world"), "expected body: {out}");
    }

    /// `tilth_read` `path#n` (`FromLine`) suffix returns from line n to end.
    #[test]
    fn tool_read_from_line_suffix() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("f.txt");
        std::fs::write(&p, "l1\nl2\nl3\nl4\n").unwrap();
        let spec = format!("{}#3", p.to_str().unwrap());
        let args = serde_json::json!({ "paths": [spec] });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&tc(&args), &cache, &session).expect("from-line suffix ok");
        assert!(out.contains("l3"), "line 3 expected: {out}");
        assert!(out.contains("l4"), "line 4 expected: {out}");
        assert!(!out.contains("l1"), "line 1 must be excluded: {out}");
    }

    /// `mode: signature` emits numbered signature lines, not full bodies.
    #[test]
    fn tool_read_signature_mode_emits_numbered_signature_lines() {
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
        let out = tool_read(&tc(&args), &cache, &session).expect("signature ok");
        assert!(
            out.contains("[signature]"),
            "signature header missing: {out}"
        );
        // Exact-match locks the whole-file-tag `N:content` format: a
        // reintroduced per-line hash (`1:abc|fn ...`) or a 0-indexed number
        // (`0:fn ...`) both fail this equality.
        assert!(
            out.lines().any(|l| l == "1:fn signature_target() {"),
            "expected exact numbered signature line `1:fn signature_target() {{`, got: {out}"
        );
        assert!(
            !out.contains("body_marker"),
            "signature mode must not include function body: {out}"
        );
    }

    /// Auto mode uses the same numbered signature output for large code.
    #[test]
    fn tool_read_auto_large_code_emits_numbered_signature_lines() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("large.rs");
        let mut src = String::from("fn large_signature_target() {\n    let body_marker = 42;\n}\n");
        src.push_str(&"// padding padding padding padding\n".repeat(1000));
        std::fs::write(&p, src).unwrap();
        let args = serde_json::json!({ "paths": [p.to_str().unwrap()] });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&tc(&args), &cache, &session).expect("auto signature ok");
        assert!(
            out.contains("[signature]"),
            "signature header missing: {out}"
        );
        // Exact-match locks the `N:content` format (no per-line hash, 1-indexed).
        assert!(
            out.lines().any(|l| l == "1:fn large_signature_target() {"),
            "expected exact numbered signature line `1:fn large_signature_target() {{`, got: {out}"
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
        let out = tool_read(&tc(&args), &cache, &session).expect("auto small-code ok");
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
        let out = tool_read(&tc(&args), &cache, &session).expect("auto small-md ok");
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
        let out = tool_read(&tc(&args), &cache, &session).expect("auto large-md ok");
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
        // Values must exceed the outline's 40-char value-preview cap so the
        // keys outline compresses well below the never-worse gate's 80% floor —
        // with short values OGATE correctly returns full content and there is
        // no `[keys]` view to observe.
        let mut src = String::from("{\n  \"top_level_marker\": {\n");
        let long_value = "value-".repeat(40);
        for i in 0..500 {
            let _ = writeln!(src, "    \"padding_key_{i}\": \"{long_value}{i}\",");
        }
        src.push_str("    \"trailing_key\": null\n  }\n}\n");
        std::fs::write(&p, src).unwrap();
        let args = serde_json::json!({ "paths": [p.to_str().unwrap()] });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&tc(&args), &cache, &session).expect("auto structured ok");
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
        let out = tool_read(&tc(&args), &cache, &session).expect("auto other-text ok");
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
        let out = tool_read(&tc(&args), &cache, &session).expect("stripped ok");

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
        let out = tool_read(&tc(&args), &cache, &session).expect("stripped ok");
        // Lines 1, 3, 5 survive; gutter shows the original numbers.
        assert!(
            out.contains("1  fn alpha()")
                && out.contains("3  fn beta()")
                && out.contains("5  fn gamma()"),
            "expected original line numbers in gutter: {out}"
        );
    }

    /// Editable `<line>:<content>` anchors must NOT appear in stripped output
    /// even when the server is in edit mode — the line set is non-contiguous with the file on disk
    /// and would mislead the agent into trying to anchor a write.
    #[test]
    fn tool_read_stripped_suppresses_editable_anchors_in_edit_mode() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("nohash.rs");
        std::fs::write(&p, "fn keep() {}\n// stripped\nfn also() {}\n").unwrap();
        let args = serde_json::json!({
            "paths": [p.to_str().unwrap()],
            "mode": "stripped"
        });
        let cache = OutlineCache::new();
        let session = Session::new();
        // edit_mode = true intentionally — stripped MUST still suppress editable anchors.
        let out = tool_read(&tc(&args), &cache, &session).expect("stripped+edit ok");
        // Stripped output is non-contiguous with disk, so it must NOT present
        // editable `<line>:<content>` numbered anchors for the `fn keep()` line.
        assert!(
            !out.lines().any(|l| l.contains("fn keep()")
                && l.split_once(':')
                    .and_then(|(n, _)| n.trim().parse::<u32>().ok())
                    .is_some()),
            "stripped output must not present editable numbered anchors: {out}"
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
        let out = tool_read(&tc(&args), &cache, &session).expect("stripped+suffix ok");
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
        let err = tool_read(&tc(&args), &cache, &session).expect_err("unknown mode rejected");
        assert!(err.contains("stripped"), "error must list new mode: {err}");
        assert!(
            err.contains("already tagged and editable"),
            "error must explain every read is already tagged: {err}"
        );
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
        let out = tool_read(&tc(&args), &cache, &session).expect("auto sig ok");
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
        let out = tool_read(&tc(&args), &cache, &session).expect("explicit sig ok");
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
        let out = tool_read(&tc(&args), &cache, &session).expect("auto small ok");
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
        let out = tool_read(&tc(&args), &cache, &session).expect("auto large md ok");
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
            write!(src, "fn f{i}() {{\n    let l = {i};\n}}\n\n").unwrap();
        }
        std::fs::write(&p, src).unwrap();
        let args = serde_json::json!({
            "paths": [p.to_str().unwrap()],
            "budget": 400
        });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&tc(&args), &cache, &session).expect("budget read ok");
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
            write!(src, "fn f{i}() {{\n    let l = {i};\n}}\n\n").unwrap();
        }
        std::fs::write(&p, src).unwrap();
        let budget = 500u64;
        let args = serde_json::json!({
            "paths": [p.to_str().unwrap()],
            "budget": budget
        });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&tc(&args), &cache, &session).expect("budget read ok");
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

    // -- tilth_write tool: whole-file-tag op-grammar dispatch --------------
    // PR2 swapped tilth_write to the op-grammar blob surface; the deep
    // behaviour (round-trip, drift recovery, anchoring, file ops) is covered
    // in `mcp::tools::write::tests`. These lock the dispatch seam.

    fn edit_services() -> (Session, Arc<BloomFilterCache>) {
        (Session::new(), Arc::new(BloomFilterCache::new()))
    }

    #[test]
    fn tool_write_applies_op_grammar_blob_after_read() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let p = root.join("disp.rs");
        std::fs::write(&p, "one\ntwo\nthree\n").unwrap();
        let (session, bloom) = edit_services();
        let cache = OutlineCache::new();

        // Read in edit mode records the snapshot and emits the tag header.
        let read_out = tool_read(
            &serde_json::json!({"paths": [p.to_str().unwrap()], "mode": "full", "cwd": root.to_str().unwrap()}),
            &cache,
            &session,
        )
        .expect("edit-mode read");
        let tag =
            crate::edit::tag::format_tag(crate::edit::tag::compute_file_hash("one\ntwo\nthree\n"));
        assert!(
            read_out.contains(&format!("#{tag}]")),
            "read must emit [path#TAG]: {read_out}"
        );

        let edits = serde_json::json!([{ "path": p.to_str().unwrap(), "tag": tag, "ops": [{ "op": "replace", "start": 2, "end": 2, "content": "TWO" }] }]);
        let out = tool_write(
            &serde_json::json!({"edits": edits, "cwd": root.to_str().unwrap()}),
            &session,
            &bloom,
            &cache,
        )
        .expect("write ok");
        assert!(out.contains("applied"), "expected applied: {out}");
        assert_eq!(std::fs::read_to_string(&p).unwrap(), "one\nTWO\nthree\n");
    }

    #[test]
    fn tool_write_missing_edits_blob_is_top_level_error() {
        let (session, bloom) = edit_services();
        let err = tool_write(
            &serde_json::json!({}),
            &session,
            &bloom,
            &OutlineCache::new(),
        )
        .expect_err("missing edits → error");
        assert!(err.contains("edits"), "error must name the param: {err}");
    }

    // -- build_instructions: cwd guidance and routing spans -----------------

    #[test]
    fn build_instructions_no_trailing_whitespace() {
        let s = build_instructions();
        assert!(
            !s.ends_with('\n') && !s.ends_with(' '),
            "wire output must not end with whitespace"
        );
    }

    /// The retired v2 trial nudge must not resurface.
    #[test]
    fn build_instructions_never_mention_search_v2() {
        assert!(
            !build_instructions().contains("tilth_search_v2"),
            "instructions must not mention the retired tilth_search_v2"
        );
    }

    /// Guard the cwd-guidance span against markdown drift in the prompt file.
    #[test]
    fn cwd_guidance_spans_present() {
        assert!(
            SERVER_INSTRUCTIONS.contains(CWD_PATHS_SPAN),
            "PATHS cwd span drifted from prompts/mcp.md"
        );
    }

    /// The served instructions must fit Claude Code's 2KB `instructions`-field
    /// truncation (root cause: an 8.7KB composed prompt was truncated below the
    /// fold, so agents never saw the per-tool routing section) and must still
    /// carry the full routing surface: the cwd PATHS guidance, every tool, and
    /// the shell DO NOT lines.
    #[test]
    fn build_instructions_fit_2kb_and_carry_critical_spans() {
        let s = build_instructions();
        assert!(
            s.len() <= 2048,
            "instructions must fit the 2KB field: {} bytes",
            s.len()
        );
        assert!(s.contains(CWD_PATHS_SPAN), "missing PATHS span");
        for tool in ["tilth_search", "tilth_read", "tilth_write"] {
            assert!(s.contains(tool), "missing tool {tool}");
        }
        assert!(
            s.contains("{pattern: \"Some($A)\", language, glob?}"),
            "structural search entry shape must stay advertised"
        );
        assert!(!s.contains("tilth_list"), "retired tool in instructions");
        assert!(!s.contains("tilth_diff"), "retired tool in instructions");
        assert!(!s.contains("tilth_grok"), "retired tool in instructions");
        assert!(s.contains("shell `git diff` or `git log`"));
        assert!(s.contains("directory browsing (ls/find)"));
        assert!(
            s.contains("DO NOT use shell for repo content reads"),
            "missing shell DO NOT line"
        );
        assert!(
            s.contains("cat/head/tail/sed/grep/rg)"),
            "shell DO NOT line must enumerate the replaced commands"
        );
    }

    /// Dispatch rejects a non-positive `budget` (0, negative, non-integer)
    /// across all tools instead of silently defaulting — a sub-1 budget used
    /// to collapse batch output to useless stubs.
    #[test]
    fn dispatch_rejects_non_positive_budget() {
        let services = Services::new();
        for bad in [
            serde_json::json!(0),
            serde_json::json!(-1),
            serde_json::json!(0.5),
        ] {
            let args = serde_json::json!({ "queries": [{ "query": "foo" }], "budget": bad });
            let err = dispatch_tool("tilth_search", &args, &services)
                .expect_err("non-positive budget must be rejected");
            assert!(
                err.contains("positive integer"),
                "expected budget validation error for {bad}, got: {err}"
            );
        }
        // A valid budget still dispatches. Use CARGO_MANIFEST_DIR (compile-time-baked)
        // rather than current_dir() (process-global runtime state): parallel diff tests
        // call set_current_dir() and can race with this test, handing us a deleted
        // tmpdir path that fails canonicalize and then is_dir().
        let ok = serde_json::json!({
            "queries": [{ "query": "foo" }],
            "budget": 5000,
            "cwd": env!("CARGO_MANIFEST_DIR")
        });
        assert!(
            dispatch_tool("tilth_search", &ok, &services).is_ok(),
            "a valid budget must still dispatch"
        );
    }

    /// `tilth_write` does not consume budget; passing budget:0 must
    /// not produce a budget error — the error should come from their own
    /// parameter validation, not the budget gate.
    #[test]
    fn budget_validation_skipped_for_non_budget_tools() {
        // tilth_write with budget:0 → own empty-edits error, not budget error.
        let services = Services::new();
        let tmp = tempfile::tempdir().unwrap();
        let args = serde_json::json!({
            "budget": 0,
            "edits": [],
            "cwd": tmp.path().to_str().unwrap()
        });
        let err = dispatch_tool("tilth_write", &args, &services)
            .expect_err("empty edits array must be rejected");
        assert!(
            err.contains("no sections"),
            "error must come from the empty-edits check: {err}"
        );
        assert!(
            !err.contains("positive integer"),
            "budget gate must not fire for tilth_write: {err}"
        );
    }

    /// Multi-file read splits the budget per file so every file is
    /// represented even under a tight budget — no silent trailing drops.
    #[test]
    fn read_batch_budget_represents_every_file() {
        let tmp = tempfile::tempdir().unwrap();
        let names = ["a.rs", "b.rs", "c.rs"];
        let mut paths = Vec::new();
        for name in names {
            let p = tmp.path().join(name);
            let body: String = (0..400).fold(String::new(), |mut acc, i| {
                let _ = writeln!(acc, "let x_{i} = {i};");
                acc
            });
            std::fs::write(&p, format!("fn main() {{\n{body}}}\n")).unwrap();
            paths.push(p.to_str().unwrap().to_string());
        }
        let args = serde_json::json!({
            "paths": paths,
            "mode": "full",
            "budget": 600
        });
        let cache = OutlineCache::new();
        let session = Session::new();
        let out = tool_read(&tc(&args), &cache, &session).expect("batch read");
        for name in names {
            assert!(
                out.contains(name),
                "file '{name}' was silently dropped under a tight budget:\n{out}"
            );
        }
        assert!(
            out.contains("truncated"),
            "expected truncation marker:\n{out}"
        );
    }
}
