---
status: trusted
last_verified: 2026-08-16
confidence: high
sources:
  - /home/paul/.local/share/cheese/paulnsorensen-tilth/specs/tilth-search-v2-roadmap.md
  - /home/paul/.local/share/cheese/paulnsorensen-tilth/specs/tilth-search-v2-trial.md
---
# Search and Dependency Discovery Domain Model

**Search v2 request** — An ordered batch of 1–10 `{query, path?}` objects with an optional shared positive budget; the server owns routing.
_Avoid_: kinded query, mode-selected query
_Code_: NEW ENTITY (schema in `src/mcp/tools/definitions.rs`, engine in `src/mcp/tools/search_v2.rs`)

**Search result envelope** — A JSON-text response with top-level `results`, `hints`, and `diagnostics`, preserving request order and per-query completeness; `routes_tried` never appears in it.
_Avoid_: prose response, mixed Markdown envelope
_Code_: NEW ENTITY (`src/mcp/tools/search_v2.rs`)

**Client profile** — Normalized MCP `initialize.params.clientInfo.name` used as one persistent-cache isolation key; a stable documented fallback key applies when clientInfo is absent.
_Avoid_: session identity, process identity
_Code_: NEW ENTITY (MCP initialize boundary in `src/mcp/mod.rs`; today the handler ignores `req.params`)

**Worktree key** — Canonical Git top-level plus absolute Git dir identifying one independent checkout.
_Avoid_: path-only key, git-common-dir key
_Code_: NEW ENTITY (`src/index/deps`)

**Dependency index** — Private per-client, per-worktree redb-derived state containing per-file outgoing edges and reverse indexes.
_Avoid_: shared dependency database, source of truth
_Code_: NEW ENTITY (`src/index/deps`)

**Verified-only partial** — A core search result plus only dependency facts proven fresh before the internal sub-deadline; stale edges are omitted.
_Avoid_: stale-with-warning
_Code_: NEW ENTITY (`src/index/deps`)

**Cold partial** — The verified-only partial computed with zero index available (missing shards); coverage metadata and a typed continuation are attached. First-class, not an error state.
_Avoid_: degraded mode, fallback result
_Code_: NEW ENTITY (AC-7 contract, `src/mcp/tools/search_v2.rs`)

**Graduation manifest** — A prepared per-harness record that freezes every matched v1/v2 threshold and real-call sample floor before trial advertisement; `[BLOCKED]` markers record unmeasured floors and the evaluator never passes while any remain.
_Avoid_: calendar sunset, best-effort comparison
_Code_: NEW ENTITY (format/evaluator in `benchmark/graduation`, manifest artifacts under `benchmark/results`)

**Search continuation** — A typed follow-up for bounded expensive graph work such as callers, callees, siblings, tests, or completing a dependency refresh.
_Avoid_: unconditional expansion, public mode flag
_Code_: NEW ENTITY (`typed_hint` in `src/mcp/tools/search_v2.rs`)

**Search surface** — Which discovery registry the MCP server advertises: `v1`, `v2`, or `both`; selected by the `--search-surface` CLI flag, default `v1` until the graduation manifest authorizes the trial flip.
_Avoid_: mode, variant, profile
_Code_: NEW ENTITY (`src/main.rs` flag → `Services`)

**Trial registry** — The `both` surface: existing v1 discovery tools plus `tilth_search_v2`, `tilth_list` unchanged, no other new verb.
_Avoid_: dual registry
_Code_: `tool_definitions()` at `src/mcp/tools/definitions.rs:3` (gains a surface parameter)

**Telemetry sink** — Services-owned writer of versioned, content-free, size-bounded JSONL search telemetry under `$XDG_STATE_HOME/tilth/telemetry/`; `Session` and its retired `record_savings` counters are precedent only, never the integration point.
_Avoid_: logger, metrics store, Session counters
_Code_: NEW ENTITY (`src/telemetry.rs`)

**Spike verdict** — The machine-readable pass/fail artifact from the `spikes/redb-deps` self-contained crate; any failed gate halts only the index lane (partial halt) and reopens the backend decision.
_Avoid_: benchmark result
_Code_: NEW ENTITY (`spikes/redb-deps`)
