# tilth_search timeouts: the serial basename-fallback walk

Diagnosed 2026-09-09 from session analytics (80 of ~100 `tilth_search` errors in 14 days were 90 s timeouts, all in one large monorepo with ~14 nested worktrees under `.worktrees/`). Tracked as GitHub issue #244.

## Cause

`basename_file_outline` (src/search/mod.rs, called from `format_search_result`) runs for every single-word query. When no collected match has a basename equal to the query, it calls `find_basename_fallback`, which walks `scope` with a **serial** `ignore::WalkBuilder` to depth 6. Before #244 that walker did not apply `skip_dir_entry`/`SKIP_DIRS`, so it descended into `node_modules`, `.git`, `.worktrees/*`, and every nested checkout. Sampling the symbolized binary showed 100% of the hot thread in this walk (`stat`/`opendir`), while the parallel search walker finished in under a second.

## Evidence

- CLI symbol search, `--scope <monorepo>/libs`: 0.3 s. `--scope <monorepo>` (root): 41 s. `--scope <monorepo>/.worktrees` (14 nested worktrees): 146 s.
- A plain checkout with `node_modules` and no nested worktrees still took 16 s at root.
- 64 of 80 timed-out calls used a single-word first query, the trigger condition.

## Gotchas

- `SKIP_DIRS` already lists both `.worktrees` (unconditional) and bare `worktrees` (skipped only when it holds nested checkouts; see `skip_dir_entry`). The main parallel walker was never the problem; only the fallback walker bypassed the skip list.
- Fastest confirmation of a fix: run the CLI with `--scope` at the repo root vs a subdirectory on any repo that has `node_modules`.

## Related

- GitHub issues #185 (all-or-nothing timeout) and #213 (cancel token) describe symptoms, not this cause. Re-evaluate both after #244 lands.
- #237 (per-batch hot path) lists the next expected wall: the per-entry `deps::open` + `reconcile` walk.
- The function is upstream-origin (2026-03-30), so the fix is a contribute-back candidate.