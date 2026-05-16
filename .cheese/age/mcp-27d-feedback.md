status: ok
next: cure
artifact: .cheese/press/mcp-27d-feedback.md
Six MCP fixes from 27d prod data; review surfaced three medium-stake findings (one efficiency, one complexity, one NIH) — no high-stake issues, no spec drift, tests strong.

# Age Report — mcp-27d-feedback

## Orientation
The diff is the six-fix MCP feedback package: JSON cache-token header (F1), batching directives (F2), empty-result hint table with file/glob counts (F3), removal of the dead `context` field including its ranking branch (F5), anchor counter-example in `write.md` (F6), and a tightened auto-fix contract paired with an integration test (F7). The contract is sound — no high-stake findings, hardening from `/press` locks the new behaviour at the `tool_search` seam. Three medium-stake findings are mechanical refactors safe to apply in cure.

## Press findings
The press report (`.cheese/press/mcp-27d-feedback.md`) flagged two LOW findings — `count_files_for_empty` is exercised only indirectly via integration tests, and the F7 narrowed contract is covered by paired tests — and recommended no fixes. Both stand: no unresolved items carry forward.

## Medium-stake findings

- **[efficiency]** `src/search/mod.rs:319-332` — `multi_symbol_inner` calls `count_files_for_empty(scope, glob)` inside the per-query loop. Each call re-runs the parallel directory walker. A 5-query batch where every query returns zero matches walks the scope five times despite `(scope, glob)` being identical. Lift the call out of the loop (compute lazily on first empty query, reuse for the rest) or memoise via `Option<(usize, usize)>` initialised on first need. The result is invariant across queries — no per-query state is consulted.

- **[complexity]** `src/search/mod.rs:83-128` and `src/search/content.rs:60-90` — `count_files_for_empty` re-implements the per-file skip chain (minified-name check + 500 KB size cap) instead of sharing the rule with `content::search`. Drift risk: if a future PR widens `content::search` to skip another category (e.g. unmarked minified bundles via `is_minified_by_content`, which `content::search` already does at lines 91-96 of `content.rs`), `Files searched: N` from the empty-header walker will silently overcount. Extract a private helper such as `fn should_search_file(path: &Path) -> bool` (or share the constant + the two checks) so both callers consult one rule. The pure walker-stat case in `count_files_for_empty` already skips the heavier 100 KB content check — calling out the asymmetry plus a comment that this is deliberate (or making them match) closes the gap.

- **[nih]** `src/mcp/iso.rs:81-87` — `with_header` builds the JSON cache-token line with `format!("{{\"if_modified_since\": \"{ts}\"}}")` rather than `serde_json::json!({"if_modified_since": ts}).to_string()`. The codebase already depends on `serde_json` (see `src/mcp/tools/definitions.rs`), and the encoder owns quote/backslash/newline escaping. The current `iso_ts` output is constrained to `YYYY-MM-DDTHH:MM:SSZ` so the manual approach is currently safe, but a future change to `iso_ts` (a timezone offset variant, a fractional-second extension) silently breaks JSON parseability with the manual writer. Switch to `serde_json::json!` so the call site asserts shape rather than relying on the producer's discipline.

## Confidence
`certain` on all three findings — each cites a concrete file:line pair, the production code is in the parent context, and each recommendation is a mechanical refactor I have evidence is correct. No evidence sources were unavailable.

## Next step
Selection prompt rendered inline — pick findings to cure or `none` to stop.
