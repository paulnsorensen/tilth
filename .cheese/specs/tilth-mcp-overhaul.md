# tilth MCP overhaul — agent-facing surface v2

**Status:** Draft (curdled from `/mold` 2026-05-13)
**Driver:** Session-analytics findings + woz comparison (`docs/woz-vs-tilth.md`)
**Slug:** `tilth-mcp-overhaul`

---

## Problem

Agents use tilth's MCP tools mostly as drop-in replacements for grep/cat/find. Empirical findings from `~/.claude/analytics/sessions.duckdb` (80,515 tool calls across 3,490 sessions):

- **82%** of `tilth_read` calls use the singular `path:` form (docs say it's rejected; empirically it works at the same error rate as `paths: [...]`).
- **97%** of `tilth_files` calls use singular `pattern:`.
- **59%** of `tilth_search` calls use `kind: content` — agents reach for the most grep-like mode because `symbol`-default's declaration-only filtering surprises them.
- **2%** use `kind: callers` — tilth's most differentiated feature is effectively undiscovered.
- **0%** ever pass `context:` (proximity ranking) despite it being documented.
- 16% set `diff: true` on edits; 11% pass `expand: 0` on search (real signal for a minimal mode).
- ~4% of edits hit hash mismatch, forcing a 3-turn re-read-and-retry dance.

Tilth has the better semantics (AST, multi-language outlines, hash-anchor safety, callers/siblings inlined) and woz has the better session economics (caching, path-range slicing, search+read folded, signature mode). This spec closes the economics gap without losing semantics.

## Acceptance criteria

1. Four MCP tools exposed: `tilth_search`, `tilth_read`, `tilth_list`, `tilth_write`. No others (deps + diff remain, untouched by this spec).
2. `tilth_search` returns merged results across symbol + content + callers in a single pass by default; `kind:` filters the merged set.
3. `tilth_search` accepts `queries: [{query, glob?, kind?}]` only — the singular `query:` form is not exposed. Per-query `glob` and `kind` work independently. (The structural shift to query objects is expected to make this strictness stick where path/pattern strictness failed.)
4. `tilth_search` expanded source lines carry `<line>:<hash>` prefixes identical to `tilth_read`. Search→write is achievable in 2 turns without a `tilth_read` step.
5. `tilth_read` accepts only `paths: [...]` with suffix forms: `#<start>-<end>` (line range), `#<heading text>` (markdown headings), `#<symbol name>` (code symbols). The standalone `section`/`sections` params are removed.
6. `tilth_read` accepts `mode: "auto" | "full" | "signature"`. `auto` is the default and uses size-and-language heuristics: small code → full; large code → signatures; small markdown → full; large markdown → headings + 3-line preview per heading; structured (JSON/YAML/TOML) → outline. **Signature output (in `signature` mode AND `auto` for large code) prefixes each signature line with `<line>:<hash>`** so the agent can pipe directly into `tilth_write` hash-mode without a follow-up full read.
7. `tilth_list` replaces `tilth_files`. Accepts `patterns: [...]` and optional `depth:`. Output is a tree hierarchy with per-file token counts and per-directory rollups (shape β).
8. `tilth_write` replaces `tilth_edit`. Per-file `mode: "hash" | "overwrite" | "append"`. `hash` retains today's anchor safety. `overwrite` and `append` require no pre-read; both create the file if absent.
9. `tilth_write` auto-fixes hash mismatches **strictly**: re-read fresh, fingerprint the original anchor range body, apply only if it appears at exactly one new location. 0 or ≥2 matches → return the new content of the affected region inline so the agent can retry in 1 turn.
10. `tilth_write` exposes `diff?: boolean` (default `false`). When `true`, response includes a per-file before/after diff so the agent can verify the edit landed without a separate `tilth_read`. Description leads with a concrete "when to use" example.
11. `tilth_read` and `tilth_search` accept `if_modified_since: <iso-ts>`. Each response includes `Results as of <iso-ts>` header. Files with mtime ≤ supplied timestamp are returned as `(unchanged)` stubs (no body).
12. Server-level `instructions` field is reduced to a thin overview (~5 lines). Detailed batching rules, kind semantics, mode semantics, and worked examples live inside each tool's `description` field.
13. Per-tool descriptions lead with a worked example for the most differentiated capability:
    - `tilth_search`: a `queries: [{query, kind: "callers"}]` example
    - `tilth_read`: an `auto` mode example showing the signature/full split
    - `tilth_list`: a tree-output example
    - `tilth_write`: an `overwrite` example for new-file creation
14. Backward-incompatible changes are explicit: `tilth_files` → `tilth_list`, `tilth_edit` → `tilth_write`, removed `section`/`sections` params, removed comma-separated `query:` form, removed singular `pattern:`. (Singular `path:` on `tilth_read` is permanently accepted; singular `query:` on `tilth_search` is not exposed because the new object-shaped queries already make the array form structurally obvious.)

## Scope

### In scope

- All four tools listed above
- `if_modified_since` caching primitive across read + search
- Auto-fix on hash mismatch (strict fingerprint)
- Hash-on-expand in search output
- Per-tool description rewrite (markdown source in `prompts/`)
- Thin server `instructions` block
- Migration of `prompts/mcp-base.md` and `prompts/mcp-edit.md` content into per-tool descriptions; thin overview remains at server level

### Out of scope

- `tilth_deps` (unchanged)
- `tilth_diff` (unchanged for now; may pull back later per user note)
- Smart-quote / Unicode-typography fuzzy matching (not needed under hash anchoring)
- Notebook (`.ipynb`) cell semantics
- Session memory equivalent to woz Recall
- DB introspection equivalent to woz Sql
- Removing the existing `[shown earlier]` session-dedup behavior (kept as-is)

## Design

### Tool: `tilth_search`

```
tilth_search(
  queries: [{                              # required array; min 1; max 10
    query: string,
    glob?: string,                         # whitelist/exclude for THIS query
    kind?: "symbol" | "content" | "regex" | "callers"  # filter for THIS query
  }],
  expand?: number = 2,                     # 0 = location list only; N = inline source for top N
  context?: string,                        # path to file being edited; ranking boost only
  if_modified_since?: string
)
```

Rules:
- `queries` is the only entry form. Even single-query calls wrap: `queries: [{query: "handleAuth"}]`. Object shape (vs raw strings) makes the array contract structurally obvious.
- Default behavior runs symbol + content + (callers if query is identifier-shaped per `classify.rs`) and merges results.
- `kind:` (per-query) filters the merged set down. `"callers"` is a search **type** (find call sites of symbol Y); `context:` is a ranking **lens** (boost results near the file I'm editing) — they're orthogonal and compose.
- `expand: 0` → no source bodies, location + outline + calls/siblings block names only.
- `expand: N` → top-N matches inline source with `<line>:<hash>` prefixed lines.
- `context: "src/foo.rs"` → ranking boost (never a filter). Matches in `src/foo.rs`, its directory siblings, and its imports/importers rank higher. Most useful for content/regex queries where proximity to the current edit matters. Description must include a concrete example: "If editing `src/edit.rs`, pass `context: \"src/edit.rs\"` to surface results in or near the edit target first."
- `if_modified_since:` → matches in files with mtime ≤ ts return as `(unchanged @ <iso-ts>)` stubs.

Response header: `Results as of <iso-ts>`

### Tool: `tilth_read`

```
tilth_read(
  paths: string[],                         # max 20
  mode?: "auto" | "full" | "signature",    # default "auto"
  if_modified_since?: string
)
```

Path suffix grammar:
- `<path>` — read entire file under `mode` rules
- `<path>#<n>-<m>` — read lines n..m (forces `full` for the range)
- `<path>#<n>` — read from line n to end (forces `full`)
- `<path>#<heading text>` — markdown only; resolve to heading's section
- `<path>#<symbol name>` — code only; resolve to symbol's source range

Mode `auto` heuristic:
| Language family | Small file | Large file |
|---|---|---|
| Code (Rust, TS, Python, Go, …) | full content + hashes | signatures only (outline), each signature line prefixed with `<line>:<hash>` |
| Markdown | full content | headings + ~3 lines preview per heading |
| Structured (JSON/YAML/TOML) | full content | outline (today's behavior) |
| Other text | full content | full content (no outline available) |

Cutoff: define `LARGE_FILE_TOKENS` constant (~2k tokens, validate against today's `src/read/mod.rs` threshold).

Modes `full` and `signature` are escape hatches that override the heuristic.

### Tool: `tilth_list`

```
tilth_list(
  patterns: string[],                      # max 20
  depth?: number
)
```

Output (shape β):
```
src/                      ~28k tokens   45 files
├── cache.rs              ~833 tokens
├── classify.rs           ~2.9k tokens
├── lib.rs                ~1.2k tokens
├── search/               ~14k tokens   8 files
│   ├── mod.rs            ~5.0k tokens
│   └── symbol.rs         ~2.1k tokens
└── ...
```

Directory rollups: `~Nk tokens   M files` aggregate over the subtree.

### Tool: `tilth_write`

```
tilth_write(
  files: [{
    path: string,
    mode?: "hash" | "overwrite" | "append",   # default "hash"

    # hash mode:
    edits?: [{
      start: "<line>:<hash>",
      end?:  "<line>:<hash>",
      content: string                          # "" = delete range
    }],

    # overwrite | append:
    content?: string
  }],
  diff?: boolean = false                       # show per-file before/after diff in response
)
```

Rules:
- `hash` mode → today's hash-anchored behavior + auto-fix on mismatch.
- `overwrite` → write whole file; create if absent.
- `append` → append bytes to file; create if absent.
- One mode per file. Mixed-mode batching across files in one call is allowed.
- `diff: true` → response includes a per-file unified diff (before/after). Use when verifying an edit landed correctly without paying for a `tilth_read` follow-up. Description must include a one-line guidance: "Pass `diff: true` when you need to confirm exactly what changed and don't already have the new content cached."

Auto-fix algorithm (hash mode, strict):
1. Anchor `start: "<L>:<H>"` doesn't match → flag mismatch internally.
2. Re-read file fresh. Compute fingerprint = byte content of the original anchor range (`start..end` if present, else line `start`).
3. Search the new file for that fingerprint.
4. **Exactly one match** → apply edit at that new location. Response notes `auto-fixed: <old_line> → <new_line>`.
5. **Zero or two-plus matches** → response includes the new content of the affected region with hashes, error flagged for that file only. Other files in the batch proceed independently.

Per-file independence preserved: one file's failure does not block siblings.

### Server `instructions` field

```
tilth — AST-aware code intelligence MCP server.

Four tools, all batch-capable via array inputs:
  • tilth_search — find by symbol/content/regex/callers (merged by default)
  • tilth_read   — load files with smart auto-sizing (full / signature / preview)
  • tilth_list   — directory layout with token-cost rollups
  • tilth_write  — hash-anchored / overwrite / append; auto-fixes safe mismatches

Each tool's description carries detailed usage, examples, and batching rules.
DO NOT use host Grep, Read, Glob, Write, or Edit.
```

(~10 lines. All semantics live in tool descriptions.)

### Per-tool description structure

Each tool description follows this layout:
1. **One-line purpose** (replaces what)
2. **Worked example** of the most differentiated capability
3. **Parameters** in schema-order with semantic notes
4. **Batching rule** (specific to this tool)
5. **Output shape**

This keeps the highest-leverage content in every tool's prompt-cached schema block, so guidance survives compaction better than a top-of-prompt server instructions block.

## Migration / breaking changes

| Old | New | Migration |
|---|---|---|
| `tilth_files(pattern: "...")` | `tilth_list(patterns: ["..."])` | Tool renamed; accept singular `pattern:` and array `patterns:` for one minor version to ease migration; warn on singular |
| `tilth_files(patterns: [...])` | `tilth_list(patterns: [...])` | Tool renamed only |
| `tilth_edit(files: [...])` | `tilth_write(files: [...])` with `mode: "hash"` default | Tool renamed; behavior unchanged in hash mode |
| `tilth_edit(..., diff: true)` | `tilth_write(..., diff: true)` | Param preserved; behavior unchanged. Description sharpened for clarity. |
| `tilth_read(path: "...")` | `tilth_read(paths: ["..."])` | Accept singular `path:` permanently (analytics show 82% usage; not worth fighting) |
| `tilth_read(path: "x", section: "10-20")` | `tilth_read(paths: ["x#10-20"])` | `section:`/`sections:` accepted for one minor version with deprecation note |
| `tilth_search(query: "x")` (singular) | `tilth_search(queries: [{query: "x"}])` | Singular `query:` is NOT exposed. Accept for one minor version as a transitional alias that wraps into a one-element `queries`, then remove. |
| `tilth_search(kind: "any")` | drop `kind` → search-all is the new default | `kind: "any"` accepted as a no-op |

Version bump: minor (additive defaults + new tool names) with a documented deprecation window of 2 minor releases for the transitional accept-old-form behavior.

## Test plan

- **classify.rs integration tests**: every supported QueryType variant flows through tilth_search and produces expected merged results.
- **Queries-only validation**: `tilth_search(query: "x")` returns a clear error in the post-deprecation release; the transitional release accepts it as a one-element wrap.
- **Suffix grammar**: tilth_read accepts `#100-200`, `#100`, `#<heading>`, `#<symbol>`; invalid suffixes fail clearly.
- **Auto mode heuristic**: golden tests for small-code/large-code/small-md/large-md/structured/other, asserting body vs signature vs preview.
- **Auto-fix strict**: cases with 0, 1, 2+ fingerprint matches; verify exactly-one applies, others return new content.
- **Per-file independence**: mixed-mode batch with one failure does not affect siblings.
- **Cache stub**: `if_modified_since` on unchanged file returns `(unchanged)` stub; on changed file returns full payload.
- **Hash-on-expand**: expanded source lines in tilth_search output carry valid hashes that round-trip through tilth_write.
- **Benchmark gate**: re-run `benchmark/run.py` on haiku for `rg_search_dispatch`, `rg_trait_implementors`, `gin_servehttp_flow` and verify cost-per-correct improves or holds.

## Non-goals

- Replacing the existing `OutlineCache` (build-time cache). It stays; `if_modified_since` is independent (agent-facing response cache).
- Removing `[shown earlier]` session dedup.
- Removing or merging `tilth_deps` / `tilth_diff`.
- Adding a `kind: "any"` value (collapsed into the new default behavior).
- Adding fuzzy old_string matching to overwrite/append modes.

## Open implementation questions

(Decide during `/cook`, not during spec.)

- Exact `LARGE_FILE_TOKENS` cutoff for the mode-auto heuristic.
- Whether tilth_search's auto-detection of identifier-shaped queries (for callers inclusion) uses `classify.rs::looks_like_exact_symbol` or a separate predicate.
- Whether the `tilth_write` mode flag accepts a short-form alias (e.g., `mode: "w"` for overwrite, `mode: "a"` for append).
- Per-tool description token budget — target ≤500 tokens each so total description payload stays under ~2k tokens.

## Blast radius

Touched modules:
- `src/mcp.rs` — tool registration, server instructions
- `src/classify.rs` — informs search ranking
- `src/search/mod.rs` — merged-search default, expand-with-hashes, `if_modified_since`
- `src/read/mod.rs` — auto/full/signature mode, suffix grammar, `if_modified_since`
- `src/edit.rs` — rename to `tilth_write`, add overwrite/append modes, auto-fix
- `prompts/mcp-base.md` — collapse to overview
- `prompts/mcp-edit.md` — collapse to overview
- `AGENTS.md` — regenerated by `scripts/regen-agents-md.sh`
- New: per-tool description files (or inline in `mcp.rs`)

Verdict: **medium-to-high**. Touches every MCP entry point and changes the public schema. Migration window required.

## References

- `docs/woz-vs-tilth.md` — full surface comparison and recommendations
- `~/.claude/analytics/sessions.duckdb` — empirical usage analytics
- `prompts/mcp-base.md`, `prompts/mcp-edit.md` — current instructions
- `src/classify.rs` — existing query classification (informs search default)
