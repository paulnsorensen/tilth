status: ok
next: press
artifact: .cheese/specs/mcp-27d-feedback.md
Six MCP fixes from 27d prod data: structured JSON cache-token header, per-tool batching directives, empty-result hint table with file/glob counts, drop dead `context` field (incl. ranking branch), anchor counter-example in write.md, integration test plus prompt tightening for the hash-mismatch auto-fix path.

# Package-ready report

**Branch.** `paulnsorensen/mcp-27d-feedback` off `paulnsorensen/diff-23-24` (PR 25 head `5f367853`). Becomes PR 26 in the stack.

**Changes (by finding).**

- **F1 — Structured cache token.** `src/mcp/iso.rs::with_header` now emits `{"if_modified_since": "<ts>"}` as the leading line (single JSON object, on its own line). `unchanged_stub` reuses the new shared `iso_ts` helper rather than parsing back out of a prose header. `prompts/tools/search.md` + `prompts/tools/read.md` documentation updated to point at the JSON-line shape. Tests in `iso.rs` and the three call sites in `src/mcp/mod.rs` rewritten to round-trip the JSON field instead of asserting on the prose prefix. `<certain>` — round-trip verified by `iso::tests::with_header_prefixes_json_cache_token`.
- **F2 — Per-tool batching directives.** Prepended the `ALWAYS group … ONE tilth_<tool> call … Never call tilth_<tool> twice in a row.` shape to `search.md`, `read.md`, and `list.md`, matching `write.md` verbatim. Tool descriptions are compiled in via `include_str!`, so no further wiring required.
- **F3 — Empty-result differentiation.** Added `format::EmptyHint` (Symbol / Content / Regex / Callers / Merged) plus `format::search_empty_header(query, scope, files_matched_glob, files_searched, content_hits, kind)` which emits a 4-line block with the three counts and a kind-aware hint per the spec's dispatch table. `count_files_for_empty(scope, glob)` in `src/search/mod.rs` runs a single counting walker pass (mirroring the size/minified skip rules in `content::search`) only when matches is empty. `format_search_result` now takes `kind: EmptyHint, glob: Option<&str>` and switches to the empty header on zero matches; the multi-symbol expanded path does the same per query. Six unit tests in `src/format.rs` cover all five hint branches. Callers (`callers::search_callers_expanded`) keeps its richer existing zero-result formatter — the `EmptyHint::Callers` variant exists so the dispatch table stays exhaustive and unit-testable.
- **F5 — Drop dead `context` field.** Schema property dropped from `definitions.rs::tilth_search`. Args parsing + propagation removed from `mcp/tools/search.rs`. The `If editing src/edit.rs, pass context: …` sentence removed from `prompts/tools/search.md`. Internal `context: Option<&Path>` parameter stripped from `search_symbol*`, `search_content*`, `search_regex*`, `multi_symbol_inner`, `symbol::search`, `content::search`, and the four `*_raw` variants. `rank::sort` no longer takes a context arg; `context_proximity`, `shared_prefix_depth`, and the local `package_root` helper are gone; the `prefers_same_subtree_as_context` rank test is gone. Four `None`-passing call sites in `src/lib.rs` updated. `<certain>` — full lib build + 453 tests green; no `dead_code` warnings remain.
- **F6 — Anchor counter-example in `write.md`.** WRONG/RIGHT block inserted between the request shape and the modes paragraph, covering the three production failure modes (body included, bare line, trailing pipe).
- **F7 — Hash-mismatch auto-fix path.** Added `tool_write_auto_fix_shift_returns_fresh_region_not_relocation` in `src/mcp/mod.rs` which simulates the spec's realistic agent retry (write C0, shift to C1, submit C0's anchor). Test demonstrates that `probe_one_auto_fix` cannot recover the original body in the body-moved case because `capture_hash_original` reads from the CURRENT file at the agent's claimed line — by then the body has shifted out of that slot, so the 12-bit hash alone can't reconstruct it. The probe correctly falls through to the fresh hashlined region (Ambiguous path) and leaves the file untouched. Per the spec's path 2, `prompts/tools/write.md` is tightened: the claim is now "Hash mode tolerates a stale anchor hash: if the line at your claimed `<line>` still holds the same content you read (byte-exact, hash drifted only because the line was re-hashed), the edit re-applies and the response notes `auto-fixed: <line> → <line>`." `prompts/mcp-base.md`'s 1-line summary updated to match. `AGENTS.md` regenerated. `<certain>` — the broken-as-advertised relocation case is now documented as test + prompt-narrowed contract; future design changes that add real relocation should flip the test red.

**Out of scope (per spec).** F4 (already shipped in PR 25 commit `5f36785`), F8 (`tilth_deps` / `tilth_diff` sample too small).

**Quality gates.**

- `cargo build` — clean.
- `cargo clippy -- -D warnings` — clean.
- `cargo fmt --check` — clean.
- `cargo test` — **453 passed** (3 suites). Up from 442 on the base (6 new format `empty_header` tests + 1 new `auto_fix` integration test + plumbing-driven test renames net +11).
- `scripts/regen-agents-md.sh` re-run; the resulting `AGENTS.md` diff is one line — `tolerates stale anchor hashes` swapped for `auto-fixes safe mismatches`, matching the tightened F7 contract.

**Residual risk.**

- `<speculative>` — F3's `count_files_for_empty` does an extra walker pass on the empty-result path. The walker skips junk dirs and re-applies the size/minified-name filters from `content::search`, but I haven't separately benchmarked the cost. 38% of `tilth_search` calls in the 27d window return zero matches, so this fires on roughly that fraction of queries; on the empty path the original search already terminated cheaply, so the extra walk should be a small wedge of an already-cheap response.
- `<don't know>` — whether haiku will respect the F2 batching directives after the F1 JSON-line lands above them. The two changes are independent in shape but stack on the same first-page real estate of each tool's prompt. The benchmark in the spec (`rg_search_dispatch`, `rg_trait_implementors`, `gin_servehttp_flow`) is the right verifier; I did not run it under `--auto`.
- `<certain>` — F7 narrows the advertised feature but does NOT add genuine body-relocation. If we ever want the "agent's anchor body re-located across a real shift" case to fire, the design needs either per-session content snapshots or a body field in the edit request. Documented in the new test's docstring so the next reader sees the limitation up front.

**Suggested next.** `/press paulnsorensen/mcp-27d-feedback` to harden the F3 empty-state surface and the F7 auto-fix contract.
