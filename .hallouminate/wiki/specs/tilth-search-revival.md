# tilth_search revival (history)

Shipped as two PRs on 2026-05-29: PR-A #45 (`2c043ca`) and PR-B #46 (`a9b3857`).
The search-v2 engine (#193, made canonical by #231 on 2026-09-06) later replaced most of that surface.
The diagnostic empty-result header is the main part that survives. This page is a decision record, not live guidance.

## Problem

The #35 upstream merge (`2254b20`) cut the `tilth_search` wire layer back to one singular `query`.
It also dropped 11 `tool_search` integration tests from pre-merge `3801a4c`. Those tests were the spec.
P0 PR #38 (`2cf0411`) was not the cause. It added comma-split targets on top of the reduced form.

## Six feature clusters

1. Batch `queries: [{query, glob?, kind?}]`, cap 10 entries. Wire layer.
2. `if_modified_since` stubs for unchanged result sections. Wire layer.
3. A cache-token JSON first line. Wire layer.
4. A merged default: symbol, content, and caller facets when `kind` is absent. Wire and engine.
5. Edit-mode hashline anchors in expanded source. Deep engine change.
6. Diagnostic header fields and the glob-zero hint. Engine header.

The array form (outer batch) and the #38 comma-split (inner targets) are separate layers. Nothing from #38 was reverted.

## Decisions

- Split by risk. PR-A restored clusters 1-4 on the wire layer. PR-B did clusters 5-6 in the engine.
- PR-B changed the default output, so it needed a haiku gate on `rg_search_dispatch`, `rg_trait_implementors`, and `gin_servehttp_flow`.
- Thread a real `edit_mode` flag through `tool_search`, from 4 to 5 arguments.

## Current status

- Cluster 1 lives on in a narrower form. `tilth_search` takes `queries` with `maxItems: 10` (`src/mcp/tools/definitions.rs:70`), but entries have no `kind`.
- Clusters 2 and 4 are gone from search. A schema test lists `scope`, `expand`, `context`, and `if_modified_since` as dropped (`src/mcp/tools/definitions.rs:386`). Routing is automatic.
- Cluster 5 is replaced. #99 removed per-line hashes. Match headers now carry `path#TAG` (#309, `3151ce2`).
- Cluster 6 survives. `search_empty_header` (`src/format.rs:148`) emits `Files matched glob`, `Files searched`, and `Content hits`. Callers: `src/search/mod.rs:441` and `src/search/mod.rs:1650`.
- Observation: the `EmptyHint` text at `src/format.rs:161-163` still says "try kind: content" or "try kind: symbol". The search schema no longer has `kind`.

## Successor

The search-v2 engine: #193 and #231, with `adr/tilth-search-v2-roadmap-001`.

_Source: former `.cheese/specs/tilth-search-revival.md`; commits `2c043ca` (#45), `a9b3857` (#46), `415f66b` (#231), `3151ce2` (#309) · Updated: 2026-10-13_
