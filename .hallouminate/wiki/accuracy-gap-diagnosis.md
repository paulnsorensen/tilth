# Accuracy gap diagnosis: ripgrep-2209 and coreutils-6682

No tilth tool gave wrong or incomplete output in the 29 failing `tilth_forced` cells on these two tasks.
The failures come from view shape: a tilth search hit shows one function, so the agent does not meet the caller or the callee code that the bug lives in.
Agents with the built-in tools read one contiguous `Read` window or a `Grep` hit that shows it.
This is goal `diagnose-accuracy-failures` of the [tilth-vs-builtin roadmap](roadmaps/tilth-vs-builtin/index.md).

## Data

- Tilth cells: 40 per task from four arms on 2026-11-06 (`ab-write-md-seed`, `ab-write-md-winner`, `bench-prs-pr443`, `bench-prs-pr444` in `benchmark/results/`, local and untracked). ripgrep-2209 passes 21/40, coreutils-6682 30/40.
- Baseline cells: 4 distinct cells per task in `benchmark/results/evolve/tools-sonnet-r1/rows.jsonl`, all correct. The rows file lists each cell twice (one `reused` copy), so count distinct `run_key` values.
- With 4 baseline cells, the pass-rate difference is not significant (Fisher p = 0.12 for ripgrep, 0.56 for coreutils). Goal `confirm-accuracy-gap` measures it with 20 reps of both arms.
- Every tilth miss fails one held-out test: `regression::r2208` (ripgrep) or `test_mkdir::test_mkdir_parent_mode_skip_existing` (coreutils).

## The correct fix and the common wrong patch

- **coreutils-6682** (`src/uu/mkdir/src/mkdir.rs`): the gold fix makes `create_dir` report whether it created the directory, and `mkdir()` calls `chmod(path, mode)` only then. All 10 failing cells add `if recursive && path.is_dir() { return Ok(()) }` in `create_dir`. `mkdir()` still runs `chmod` on the existing directory.
- **ripgrep-2209** (`crates/printer/src/util.rs`, `Replacer::replace_all`): the gold fix stops at matches past `range.end` and copies the tail only up to `range.end`. All 19 failing cells add `if m.start() >= range.end { return false; }` in the closure. The default `Matcher::replace_with_captures_at` (`crates/matcher/src/lib.rs:923-943`) still ends with `dst.extend(&haystack[last_match..])`, so the look-ahead tail is copied.

## Causes

| Task | Cause | Failing cells |
|---|---|---|
| coreutils | Patched the callee from a search hit; never read the caller `mkdir()` | 6 |
| coreutils | Read `mkdir()` and still patched only the callee | 4 |
| ripgrep | Never read the default `replace_with_captures_at` body | 13 |
| ripgrep | Read that body and still wrote only the guard | 6 |
| both | Ran the existing tests, saw green, and stopped; no existing test covers the bug | 29 |

## Baseline against tilth

- **coreutils:** all 4 baseline cells start with `Read` of `mkdir.rs` at offset 155-165, limit 80-90. That window holds `create_dir`, `mkdir()` and its `chmod` call. The caller was seen in 4/4 baseline cells, 0/10 failing tilth cells and 29/30 passing tilth cells.
- **ripgrep:** 3 of 4 baseline cells `Grep` for `fn replace_with_captures_at`, and the result includes the body with the tail line. The fourth reads `lib.rs:920-964` after its first edit and fixes the tail.
- Reading `lib.rs:923-943` before the first write separates tilth outcomes: 15 of 21 passing cells against 7 of 19 failing cells (counts are about ±2).
- **Not the cause:** volume of code seen (baseline median 2.6k characters on coreutils and 7.1k on ripgrep, less than both tilth groups), blocked Bash (equal in both arms), and the ambiguous search response for `replace_with_captures_at` (6 of 19 failing and 7 of 21 passing cells saw it).
- The tool output was correct in the cells checked. `fn create_dir` returned the full body with a `fetch_callers` hint, and `fn replace_all` returned the full body with a `fetch_callees` hint. No agent followed a hint.

## Levers to test

1. Put the call sites of a resolved function in its search hit, one line each with the enclosing function, so the caller is visible without a follow-up. This adds output, so measure it against goal `trim-search-output`.
2. When a hit calls a default trait method, show that body or name its location.
3. Push agents to write a failing test before the first edit. Prompt steering has had no detectable effect so far, so a prompt change alone is unlikely to help.

Test a lever with replay of the stored cells first (output only), then with a paid run on these two cheap tasks.

_Source: trajectory analysis of the cells above, scripts in the session scratchpad `diag/` directory, report `.cheese/explore/accuracy-failures.md` (local) · Updated: 2026-11-08 · Supersedes: none_
