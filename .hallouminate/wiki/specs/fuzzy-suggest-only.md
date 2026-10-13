# Fuzzy path resolution: drop auto-open, keep "did you mean" suggestions (history)

Shipped as #111 (`cc317fa`, 2026-07-04). Fuzzy path resolution is suggest-only, and `src/read/fuzzy_path.rs` has no auto-open branch.
This page is a decision record, not live guidance.

## Proposal

User decision 2026-07-04: the auto-open half of #139 did not come up often enough to earn its keep.
The spec converged on suggestion-only, which matches upstream (jahala `01405a55`, "fuzzy path resolution is suggest-only"). The change removes a fork divergence.

- Drop the auto-open machinery: the `Resolved` branch, `FuzzyHit`, `log_auto_open`, `search_auto_open_body`, and the score-floor and margin gate.
- `resolve_fuzzy_path` always returns the ranked top-K as `Suggestions`. The former winner becomes suggestion 1.
- Keep suggestions on both call paths: the basic read fallback and the MCP search-miss path.
- Rewrite auto-open tests to assert the did-you-mean list contains the expected candidate.

## Decisions

- Re-derive from upstream `01405a55` instead of cherry-picking, because fork files diverged.
- Keep the pre-check on the search-miss path, so a normal empty symbol search does not walk the tree.
- Out of scope: suggestion ranking (`score_candidates`, nucleo config, `SUGGESTION_K`), `src/diff/`, `src/edit/`, and any version bump.
- The prompts needed a check for auto-open wording. A mention would have required a surgical edit and a byte-lock test update.
- Verification: no `auto_open`, `FuzzyHit`, or `search_auto_open_body` reference remains in `src/`. The did-you-mean list appears for a path-like query with zero hits.

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
- The walk prunes by `.tilthignore`, not `.gitignore` (`src/read/fuzzy_path.rs:12-15`).
- Stale doc: `src/read/fuzzy_path.rs:77` names `diff::not_found_error`, which the fork removed with `src/diff/`.

_Source: `.cheese/specs/fuzzy-suggest-only.md` (moved here); former `.cheese/specs/fuzzy-path-resolution.md`; commits `f12e74e` (#57), `cc317fa` (#111) · Updated: 2026-10-13_
