# tilth MCP overhaul: agent-facing surface v2 (history)

Drafted 2026-05-13 and shipped in #23 (`da4c58e`, 2026-05-15). The spec retired on 2026-09-06.
Successors: the whole-file-tag edit model (#116) and `adr/tilth-search-v2-roadmap-001`.
The batched read and search shapes survive. The write modes and the hash-based search output do not.
This page is a decision record, not live guidance.

## Evidence

Session analytics covered 80,515 tool calls in 3,490 sessions:

- 82% of `tilth_read` calls used the singular `path` form.
- 97% of `tilth_files` calls used a singular `pattern`.
- 59% of `tilth_search` calls used `kind: content`, the most grep-like mode.
- 2% used `kind: callers`, the most differentiated feature.
- 0% passed `context`.
- 16% set `diff: true` on edits. 11% passed `expand: 0` on search.
- About 4% of edits hit a hash mismatch, which cost three turns to re-read and retry.

A comparison with woz found better session economics there: caching, range slicing, and a signature mode.

## Proposal

- Four tools: `tilth_search`, `tilth_read`, `tilth_list` (replaces `tilth_files`), `tilth_write` (replaces `tilth_edit`).
- `tilth_search`: `queries: [{query, glob?, kind?}]` only, a merged default, and top-level `expand` and `context`.
- Hash prefixes on expanded search lines, so search goes to write in two turns.
- `tilth_read`: `paths` with `#start-end`, `#heading`, and `#symbol` suffixes, and `mode: auto | full | signature`.
- `tilth_write`: per-file `mode: hash | overwrite | append`, strict auto-fix on hash mismatch, and `diff?: boolean`.
- `if_modified_since` on read and search. A thin server instruction block, with detail in tool descriptions.

## What shipped and what lasted

- Shipped and live: batched `queries` (cap 10), the path suffix grammar, `tilth_read` modes, and `diff` on `tilth_write`.
- Shipped, now read-only: `if_modified_since`. The search schema test lists it as dropped (`src/mcp/tools/definitions.rs:386`).
- Shipped, then removed: `hash | overwrite | append` modes (`src/mcp/write.rs` in #23). #35 stripped them, #37 restored atomic create, and #116 replaced them with whole-file tags and 3-way-merge recovery. No `overwrite` mode exists in `src/` now.
- Shipped, then retired: `tilth_list` (#282, `d88bdf0`).
- Dropped: top-level `expand` and `context`, and per-entry `kind`. The same test lists `expand` and `context` as dropped.
- Replaced: hash prefixes in search. Match headers now carry `path#TAG` (#309).

## Lessons

- Singular-form strictness failed for `path` and `pattern`. Object-shaped query entries made the array form stick.
- Undiscovered features (callers, context) needed examples or removal, not more prose.
- The write-mode split added surface that the whole-file tag later made unnecessary.

## Successor

#116 (JSON `edits` array on whole-file tags) and `adr/tilth-search-v2-roadmap-001`.

_Source: former `.cheese/specs/tilth-mcp-overhaul.md`; commits `da4c58e` (#23), `32940ef` (#37), `d88bdf0` (#282); PR #116 · Updated: 2026-10-13_
