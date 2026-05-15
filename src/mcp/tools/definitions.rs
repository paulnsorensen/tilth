//! JSON-schema definitions for the tools advertised via `tools/list`.
//! Edit-mode adds `tilth_write` to the catalog.

use serde_json::Value;

pub(crate) fn tool_definitions(edit_mode: bool) -> Vec<Value> {
    let read_desc = "Read files with smart auto-sizing. Example: `tilth_read(paths: [\"src/lib.rs\", \"src/mcp.rs#tool_search\"], mode: \"auto\")` returns full small files, hash-prefixed signatures for large code, and sliced suffix reads when requested. Suffix grammar: `path#n-m`, `path#n`, `path### Heading`, `path#symbol_name`. Use `mode: \"full\"` to force full content and `mode: \"signature\"` for hash-prefixed signature lines.";
    let mut tools = vec![
        serde_json::json!({
            "name": "tilth_search",
            "description": "Code search — finds definitions, content, and callers in one default search. Example callers query: `tilth_search(queries: [{query: \"handleAuth\", kind: \"callers\"}])` finds every call site of handleAuth. Use `queries: [{query, glob?, kind?}]` (array form, max 10); per-entry glob/kind compose freely. Omit kind for merged symbol + content + identifier-shaped callers; kind values filter to symbol, content, regex, or callers. `expand: N` inlines source for top N matches with `<line>:<hash>` prefixes ready for tilth_write. If editing `src/edit.rs`, pass `context: \"src/edit.rs\"` to surface nearby results first. `if_modified_since: <ts>` returns unchanged-file stubs.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "queries": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["query"],
                            "properties": {
                                "query": {"type": "string"},
                                "glob": {"type": "string"},
                                "kind": {"type": "string", "enum": ["symbol", "content", "regex", "callers"]}
                            }
                        },
                        "minItems": 1,
                        "maxItems": 10,
                        "description": "Array of query objects. Each entry runs independently and results are concatenated under `## query: <q>` headers."
                    },
                    "scope": {
                        "type": "string",
                        "description": "Only use scope to search a specific subdirectory. DO NOT USE scope if you want to search the current working directory (initial search)."
                    },
                    "expand": {
                        "type": "number",
                        "default": 2,
                        "description": "Number of top matches to expand with full source code. Definitions show the full function/class body. Usages show ±10 context lines."
                    },
                    "context": {
                        "type": "string",
                        "description": "Path to the file the agent is currently editing. Boosts ranking of matches in the same directory or package."
                    },
                    "budget": {
                        "type": "number",
                        "description": "Max tokens in response."
                    },
                    "glob": {
                        "type": "string",
                        "description": "File pattern filter. Whitelist: \"*.rs\" (only Rust files). Exclude: \"!*.test.ts\" (skip test files). Brace expansion: \"*.{go,rs}\" (Go and Rust). Path patterns: \"src/**/*.ts\"."
                    },
                    "if_modified_since": {
                        "type": "string",
                        "description": "ISO-8601 timestamp. Files with mtime ≤ this value return as `(unchanged @ <ts>)` stubs instead of bodies."
                    }
                }
            }
        }),
        serde_json::json!({
            "name": "tilth_read",
            "description": read_desc,
            "inputSchema": {
                "type": "object",
                "required": ["paths"],
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": { "type": "string" },
                        "minItems": 1,
                        "maxItems": 20,
                        "description": "File paths (max 20). Suffix grammar on each path: `path#n-m` (line range), `path#n` (from line n), `path### Heading` (markdown heading), `path#symbol_name` (code symbol). Example: paths: [\"src/foo.rs#do_thing\", \"README.md#10-40\"]. Singular `path: \"...\"` is also accepted for one-file reads."
                    },
                    "path": {
                        "type": "string",
                        "description": "Singular form (transitional). Prefer paths: [...]."
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["auto", "full", "signature"],
                        "default": "auto",
                        "description": "auto = smart-size: small files return full; large code returns signature lines with `<line>:<hash>` prefixes; large markdown returns headings + preview. full forces full content. signature forces outline."
                    },
                    "if_modified_since": {
                        "type": "string",
                        "description": "ISO-8601 timestamp. Files unchanged since this return `(unchanged @ <ts>)` stubs."
                    },
                    "budget": {
                        "type": "number",
                        "description": "Max tokens in response."
                    }
                }
            }
        }),
        serde_json::json!({
            "name": "tilth_list",
            "description": "Directory layout with token-cost rollups. Example tree output:\n```\nsrc/      ~28k tokens   45 files\n├── cache.rs      ~833 tokens\n├── search/      ~14k tokens   8 files\n│   └── mod.rs      ~5.0k tokens\n```\nUse before tilth_read to budget context. Accepts patterns: [\"*.rs\", \"src/**/*.ts\"] and optional depth.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "patterns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 20,
                        "description": "Glob patterns (max 20). e.g. [\"*.rs\", \"src/**/*.ts\"]. Always an array."
                    },
                    "depth": {
                        "type": "number",
                        "description": "Cap directory depth (1 = top-level only)."
                    },
                    "scope": {"type": "string"},
                    "budget": {"type": "number"}
                }
            }
        }),
        serde_json::json!({
            "name": "tilth_deps",
            "description": "Blast-radius check before breaking changes. Shows what a file imports (local + external) and what other files call its exports, with symbol-level detail. Use ONLY when your planned edit changes a function signature, removes/renames an export, or modifies behavior that callers rely on. Do NOT use for reading files, adding new code, or internal-only changes — use tilth_read instead.",
            "inputSchema": {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File to check before making breaking changes."
                    },
                    "scope": {
                        "type": "string",
                        "description": "Directory to search for dependents. Default: project root."
                    },
                    "budget": {
                        "type": "number",
                        "description": "Max tokens. Truncates 'Used by' first."
                    }
                }
            }
        }),
        serde_json::json!({
            "name": "tilth_diff",
            "description": "Structural diff showing function-level changes. Replaces git diff. Call with no args for uncommitted changes overview.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "Diff source: 'uncommitted' (default), 'staged', or a git ref (e.g. 'HEAD~1', 'main..feat'). Ignored when a, b, patch, or log is set."
                    },
                    "scope": {
                        "type": "string",
                        "description": "Restrict diff output to a specific file or directory path."
                    },
                    "a": {
                        "type": "string",
                        "description": "First file for a file-to-file diff. Must be used together with b."
                    },
                    "b": {
                        "type": "string",
                        "description": "Second file for a file-to-file diff. Must be used together with a."
                    },
                    "patch": {
                        "type": "string",
                        "description": "Path to a .patch file to parse instead of running git diff."
                    },
                    "log": {
                        "type": "string",
                        "description": "Git log range (e.g. 'HEAD~5..HEAD') — shows per-commit structural summaries."
                    },
                    "search": {
                        "type": "string",
                        "description": "Filter output to symbols or files matching this substring (case-insensitive)."
                    },
                    "blast": {
                        "type": "boolean",
                        "default": false,
                        "description": "Show blast-radius warnings for signature-changed symbols."
                    },
                    "expand": {
                        "type": "number",
                        "default": 0,
                        "description": "Number of changed symbols to expand with full source context."
                    },
                    "budget": {
                        "type": "number",
                        "description": "Max tokens in response."
                    }
                }
            }
        }),
    ];

    if edit_mode {
        tools.push(serde_json::json!({
            "name": "tilth_write",
            "description": "Hash-anchored / overwrite / append edits across one or more files. Replaces the host Edit and Write tools — DO NOT use those.\n\nExample overwrite (new file): `tilth_write(files: [{path: \"src/new.rs\", mode: \"overwrite\", content: \"fn main(){}\\n\"}])`.\n\nRequest shape:\n```json\n{\n  \"files\": [\n    {\"path\": \"a.rs\", \"mode\": \"hash\", \"edits\": [\n      {\"start\": \"<line>:<hash>\", \"content\": \"<new code>\"},\n      {\"start\": \"<line>:<hash>\", \"end\": \"<line>:<hash>\", \"content\": \"...\"},\n      {\"start\": \"<line>:<hash>\", \"content\": \"\"}\n    ]},\n    {\"path\": \"new.rs\", \"mode\": \"overwrite\", \"content\": \"...\"},\n    {\"path\": \"log.txt\", \"mode\": \"append\",    \"content\": \"...\\n\"}\n  ],\n  \"diff\": true\n}\n```\n\nModes per file: hash (default — replace lines at hash anchors), overwrite (whole file; creates if absent), append (creates if absent). Hash mode auto-fixes safe mismatches: if your anchor body appears at exactly one new location, the edit lands there and the response notes `auto-fixed: <old_line> → <new_line>`. Zero or 2+ matches → fresh hashlined region returned inline for one-turn retry. A malformed edit entry fails that whole file but does not block siblings.\n\nALWAYS group edits to all ready files into ONE tilth_write call (max 20 files). Each path may appear only once per call. Never call tilth_write twice in a row.\n\nPass `diff: true` to verify what landed without a separate read.",
            "inputSchema": {
                "type": "object",
                "required": ["files"],
                "properties": {
                    "files": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 20,
                        "items": {
                            "type": "object",
                            "required": ["path"],
                            "properties": {
                                "path": {"type": "string"},
                                "mode": {
                                    "type": "string",
                                    "enum": ["hash", "overwrite", "append"],
                                    "default": "hash"
                                },
                                "edits": {
                                    "type": "array",
                                    "description": "Required when mode=hash. Each: {start: '<line>:<hash>', end?: '<line>:<hash>', content: '...'}",
                                    "items": {
                                        "type": "object",
                                        "required": ["start", "content"],
                                        "properties": {
                                            "start": {"type": "string"},
                                            "end": {"type": "string"},
                                            "content": {"type": "string"}
                                        }
                                    }
                                },
                                "content": {
                                    "type": "string",
                                    "description": "Required when mode=overwrite or mode=append. Full file content / append payload."
                                }
                            }
                        }
                    },
                    "diff": {
                        "type": "boolean",
                        "default": false,
                        "description": "Include per-file before/after diff in the response."
                    }
                }
            }
        }));
    }

    tools
}
