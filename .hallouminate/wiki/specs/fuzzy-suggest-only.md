# Fuzzy path resolution: drop auto-open, keep "did you mean" suggestions

## Provenance

Upstream-sync round 2, checklist item 3 (`.cheese/notes/upstream-sync-checklist.md`). User decision 2026-07-04: the auto-open half of #139 didn't earn its keep ("didn't come up often enough") — converge on suggestion-only, which is also where upstream landed (jahala `01405a55`, "fuzzy path resolution is suggest-only — auto-open removed"). This *removes* a fork-law divergence rather than recording one.

## Contract

Mirror upstream `01405a55`'s design, adapted to fork code (the commit is reachable in-repo via the `jahala` remote — study its full diff including test changes; it is the reference implementation, but fork files have diverged so re-derive rather than blind cherry-pick):

1. **Drop the auto-open machinery** in `src/read/fuzzy_path.rs`: the `FuzzyResolution::Resolved` branch, `FuzzyHit`, `log_auto_open`, `search_auto_open_body`, and `apply_gate`'s score-floor/margin gate. `resolve_fuzzy_path` always returns the ranked top-K as `Suggestions` — the former `Resolved` winner becomes suggestion #1 in the same did-you-mean format.
2. **Keep suggestions on both call paths**: the basic-path read fallback (`lib::fuzzy_path_fallback`) and the MCP search-miss path (`src/mcp/tools/search.rs:278` region). Follow upstream's treatment of `is_path_like` (upstream removed the auto-open check it fed; suggestions were never gated by it — verify what remains needed on the search-miss path so a normal empty symbol search still doesn't walk the tree, and keep that pre-check if the fork needs it for that reason).
3. **Tests**: rewrite fork tests that assert auto-open behavior to assert the suggestion output instead; upstream's test changes in `01405a55` are the model. Suggestion assertions must be real (assert the did-you-mean list contains the expected candidate, not just non-empty).
4. **Prompts check**: grep `prompts/mcp-base.md` and `prompts/mcp-edit.md` for any mention of fuzzy auto-open behavior. If present: update the wording (surgically — no bloat), update both byte-lock tests in `src/mcp/mod.rs`, and run `./scripts/regen-agents-md.sh`. If absent: no prompt change.

## Cut list (out of scope)

- No changes to suggestion ranking/scoring (`score_candidates`, nucleo matcher config, `SUGGESTION_K`).
- No changes to `src/diff/`, `src/edit/`, or anything outside the fuzzy-path feature and its call sites/tests/prompts.
- No version bump.

## Verification

- `cargo test` green, `cargo clippy -- -D warnings` clean, `cargo fmt --check` clean.
- A search for a path-like query with zero hits returns a did-you-mean suggestion list (assert via existing/updated unit tests).
- No remaining references to `FuzzyHit`, `search_auto_open_body`, `auto_open` anywhere (`grep -rn auto_open src/` empty).
- If prompts changed: byte-lock tests updated in the same commit, AGENTS.md regenerated.

## Environment

- Branch from `origin/main` in a dedicated worktree; branch name `refactor/fuzzy-suggest-only`.
- Note: PR #108 (`port/ascii-and-dep-bumps`) touches `src/search/mod.rs` output glyphs; expected disjoint from this work, but if you touch shared output strings, use ASCII (`--`, `|`, `->`, `>`) to match where #108 is headed.
- Commit only — no push/PR (orchestrator handles).

## Predecessor: fuzzy-path-resolution (auto-open)

The predecessor spec `fuzzy-path-resolution` shipped auto-open in #57 (`f12e74e`, 2026-05-30). #111 (`cc317fa`, 2026-07-04, merged) removed auto-open. The spec retired on 2026-09-06 with this page as its successor.

It replaced a basename-only Levenshtein suggestion that read only `path.parent()`. A wrong directory component got no suggestion.

Evidence that set the matcher choice:

- Session analytics over about 1,649 path-like tilth calls found a 0.4% miss rate.
- Genuine misses had 0 transposition or substitution typos. The recoverable misses were a wrong directory or a missing prefix.
- About 70% of misses were not recoverable: stale worktree paths and moved or deleted files.
- Research agreed: LLM path errors are structural, not character-level.

Decision: use `nucleo-matcher` in `match_paths` mode, a subsequence matcher that is path-aware. Its blind spot is transposition typos, which agents do not produce. No edit-distance step was needed. The spec called the feature low-impact developer-experience insurance.

The auto-open design used a confidence gate (unique hit, or a score margin over a floor) with different read and search profiles. Search auto-open required a separator and an extension. The step-down plan (basename match plus edit distance) was documented but not built.

## Current state (verified 2026-10-13)

- `src/read/fuzzy_path.rs` is suggest-only: `FuzzyResolution` has `Suggestions` and `None` (`:32-37`), `SUGGESTION_K = 3` (`:29`), and `MAX_FUZZY_CANDIDATES = 20_000` (`:26`).
- The walk prunes by `.tilthignore`, not `.gitignore` (`:12-15`).
- Stale doc: `:77` names `diff::not_found_error`, which the fork removed with `src/diff/`.

_Source: `.cheese/specs/fuzzy-suggest-only.md` (moved here); former `.cheese/specs/fuzzy-path-resolution.md`; commits `f12e74e` (#57), `cc317fa` (#111) · Updated: 2026-10-13_
