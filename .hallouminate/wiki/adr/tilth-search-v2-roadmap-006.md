---
status: trusted
last_verified: 2026-09-06
confidence: high
sources:
  - https://github.com/paulnsorensen/tilth/issues/190
  - .hallouminate/wiki/adr/tilth-search-v2-roadmap-004.md
  - session telemetry 2026-08-26..2026-09-06
---
# Keep tilth_grok and tilth_deps MCP Verbs at Cutover

At the search-v2 cutover, tilth keeps the `tilth_grok` and `tilth_deps` MCP verbs. ADR-001 planned to fold both behind search. The cutover retires only the v1 `tilth_search` surface and the `tilth_search_v2` alias. `tilth_grok` and `tilth_deps` stay this round.

## Context

ADR-001 set a five-verb target: `search`, `read`, `list`, `diff`, and conditional `write`. It planned to remove public `grok` and `deps` after the measured trial. ADR-004 gated the cutover on evidence, not on a calendar.

The cutover session had to decide the final verb set. Search-v2 enriches a unique hit with bounded grok core context and verified-only dependency impact, so search covers part of the grok and deps value. It does not cover every direct grok or deps call.

Session telemetry from 2026-08-26 to 2026-09-06 measured real use:

- v1 `tilth_search`: 745 calls. `kind` was set on 97% (content 505, symbol 227, regex 87, callers 42). `glob` 388, `expand` 70, `scope` 17, `context` 0, `if_modified_since` 0.
- `tilth_search_v2`: 56 calls.

The `callers` kind had 42 real calls per week with no v2 equivalent at trial end. Direct `tilth_grok` and `tilth_deps` calls carry their own weekly volume that search enrichment does not replace.

## Decision

- Keep `tilth_grok` and `tilth_deps` as MCP verbs at cutover.
- Remove only the v1 `tilth_search` schema, the `tilth_search_v2` alias, the `--search-surface` flag, and the `SearchSurface` machinery.
- Add a per-query `kind: "callers"` override to the new `tilth_search`, because 42 real calls per week have no other v2 path. No other caller-supplied `kind` returns; all remaining routing stays deterministic.
- Leave the public Rust `run_grok` and `run_deps` APIs unchanged.
- Revisit folding `grok` and `deps` into `search` in a later round, gated on fresh telemetry.

## Alternatives rejected

- **Fold grok and deps now (ADR-001 target):** removes verbs that still carry unique weekly volume; search enrichment does not replace every direct call.
- **Keep the general `kind` parameter:** returns intent classification to the caller and repeats the adoption failure ADR-002 aimed to fix.
- **Drop callers with no replacement:** loses 42 real calls per week of caller search with no v2 equivalent.

## Consequences

The final MCP verb set is `search`, `read`, `list`, `deps`, `grok`, `diff`, and conditional `write` — two verbs above the ADR-001 target. The extra verbs are evidence-backed, not permanent. A later round can retire them when telemetry shows search enrichment covers their use. The single caller-facing `kind` value (`callers`) is a bounded, measured exception to deterministic routing.
