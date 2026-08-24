---
status: trusted
last_verified: 2026-08-23
confidence: high
sources:
  - src/mcp/mod.rs (worktree/searchv2, commit 3c18b50)
  - prompts/mcp-v2-note.md
  - ~/.claude/analytics/sessions.duckdb (session logs)
  - $XDG_STATE_HOME/tilth/telemetry/current.jsonl
---
# ADRs — search-v2 adoption (slug: tilth-search-v2-adoption)

Session 2026-08-23; follow-up to the trial slice (161dc46 / 1fb9ea3 on `worktree/searchv2`).

### ADR-001: Surface-aware instructions teach `tilth_search_v2` [status: accepted]

- **Context:** One week after the trial slice landed and the live config flipped to `--search-surface both`, session analytics showed **zero** natural `tilth_search_v2` calls against 113 v1 `tilth_search` calls in one day; the only telemetry was Aug-16 smoke tests. Cause: `build_instructions(edit_mode)` was surface-blind — instructions routed "find/explore → `tilth_search`" and never mentioned v2. Under `v2`-only (the matched benchmark cells) instructions pointed at tools not even advertised.
- **Decision:** `build_instructions(edit_mode, surface)` appends `prompts/mcp-v2-note.md` (DO-NOT-framed trial routing note, ~400 bytes) on non-`v1` surfaces. `v1` output stays byte-identical (guarded by `build_instructions_v1_surface_unchanged`); AC-13 caps unaffected (v1 = 9,367 B; `both` = 10,762 B vs TRIAL_CAP 16,000).
- **Rejected:** editing mcp-base/mcp-edit directly (breaks v1 byte-locks and the 13,779-char cap contract); relying on tool descriptions alone (proven insufficient — the tool was advertised for a week with zero pickup).
- **Consequence:** natural-adoption telemetry now measures "did models pick v2 when taught", not "did models spontaneously discover an untaught tool". Commit `3c18b50`.

### ADR-002: Local deploy path is the npm nightly wrapper, not a bare binary [status: accepted]

- **Context:** `~/.local/bin/tilth` (first on PATH, ahead of `~/.cargo/bin/tilth`) is a **symlink** to `@paulnsorensen/tilth-nightly/run.js`; the real binary lives at `<pkg>/bin/tilth`. A `cp new-binary ~/.local/bin/tilth` follows the symlink and clobbers `run.js`.
- **Decision:** to deploy a local build, copy to `~/.local/lib/node_modules/@paulnsorensen/tilth-nightly/bin/tilth` (unlink first if "Text file busy" — a live MCP server holds the inode); `run.js` is restorable from repo `npm/run.js`. Note an `npm i -g` nightly update replaces `bin/tilth` with the published main-branch build, reverting any branch-only surface (e.g. search-v2).

## Trial status as measured 2026-08-23

- Graduation authority = matched benchmark cells (ADR tilth-search-v2-roadmap-004); natural adoption is a separate metric; codex/omp real-call floors still `[BLOCKED]` (F004).
- v2 engine health from the 123 smoke events: routes symbol 91 / path 12 / ambiguous 7 / regex 6 / miss 4 / literal 3; latency p50 451 ms, p90 502 ms; result tokens p50 1,327; zero partial/timeout.
