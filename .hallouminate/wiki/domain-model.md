---
status: trusted
last_verified: 2026-09-06
confidence: high
sources:
  - /home/paul/.local/share/cheese/paulnsorensen-tilth/specs/tilth-search-v2-roadmap.md
---
# Search and Dependency Discovery Domain Model

**Search v2 request** — An ordered batch of 1–10 query or follow entries with an optional shared positive budget.
Query entries accept `{query, glob?}`; follow entries accept `{follow: hint}`.
The server owns initial routing.[^continuations]
_Avoid_: kinded query, mode-selected query
_Code_: `src/mcp/tools/definitions.rs`

**Search result envelope** — A JSON-text response with top-level `results`, `hints`, and `diagnostics` preserves request order and completeness.
Statuses are `ok`, `partial`, `no_match`, and `ambiguous`.[^continuations]
_Avoid_: prose response, mixed Markdown envelope
_Code_: `src/mcp/tools/search_v2.rs`

**Client profile** — Normalized MCP `initialize.params.clientInfo.name` used as one persistent-cache isolation key.
_Avoid_: session identity, process identity
_Code_: NEW ENTITY (MCP initialize boundary in `src/mcp/mod.rs`)

**Worktree key** — Canonical Git top-level plus absolute Git dir identifying one independent checkout.
_Avoid_: path-only key, git-common-dir key
_Code_: NEW ENTITY (scope resolution near `src/mcp/tools/mod.rs`)

**Dependency index** — Private per-client, per-worktree redb-derived state containing per-file outgoing edges and reverse indexes.
_Avoid_: shared dependency database, source of truth
_Code_: NEW ENTITY (`src/index/deps/mod.rs`)

**Verified-only partial** — A core search result plus only dependency facts proven fresh before the internal sub-deadline; stale edges are omitted.
_Avoid_: stale-with-warning
_Code_: NEW ENTITY (`src/index/deps/mod.rs`)

**Graduation manifest** — A prepared per-harness record that freezes every matched v1/v2 threshold and real-call sample floor before trial advertisement.
_Avoid_: calendar sunset, best-effort comparison
_Code_: NEW ENTITY (`benchmark/experiments/tilth-search-v2-graduation.json`)

**Search continuation** — A server-emitted hint requests bounded callers, callees, siblings, tests, or dependency work.
The client echoes it through search without a fresh query or a public mode selector.
The hint preserves the concrete target and scope.[^continuations]
_Avoid_: unconditional expansion, public mode flag
_Code_: `src/mcp/tools/search_v2.rs`

[^continuations]: [Search continuation decision](./adr/tilth-search-v2-roadmap-006.md); user-approved PR #231 correction, 2026-09-06.

_Source: PR #231 user direction · Updated: 2026-09-06 · Supersedes: query-only request and unexecutable continuation sketches._
