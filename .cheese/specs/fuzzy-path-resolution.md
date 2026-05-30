---
slug: fuzzy-path-resolution
source: mold-handshake
intent: When an agent gives tilth a slightly-off file path, resolve it to the right file via fuzzy path matching and auto-open it, instead of only emitting a "did you mean" suggestion.
blast_radius: medium
inputs: A path-like query (to tilth_read or a fallthrough tilth_search) that does not resolve to an existing file.
outputs: The contents of the best-matching real file with an explicit correction header, or an unchanged NotFound when no candidate clears the confidence gate.
verification: cargo test (new unit tests for resolve_fuzzy_path + integration tests for auto-open headers); existing read/search tests still green.
---

## Context

tilth currently only *suggests* on a missing path. Three sites construct a `NotFound`
with a basename-only, correct-parent-directory-only Levenshtein suggestion:

- `read::read_file` NotFound branch — `src/read/mod.rs:42` → `suggest_similar(path)`
- `single_query_search` — `src/lib.rs:433` → `suggest_similar_file(scope, text)`
- `multi_word_concept_search` — `src/lib.rs:479` → `suggest_similar_file(scope, first_word)`

`suggest_similar` (`src/read/mod.rs:526`) reads only `path.parent()`, so a wrong
*directory* component (`src/serch/foo.rs`) yields nothing — it catches only a typo in
the final segment. It never auto-resolves; the agent pays an extra round-trip.

Grounding (this design session):

- tilth's own zsh completions come from `clap_complete` (`src/main.rs:169`), not fzf —
  irrelevant to this feature; fzf is not a library anyway.
- fzf's algorithm (modified Smith-Waterman, special-position bonuses) is reimplemented
  as the `nucleo-matcher` crate, which ships a purpose-built `Config::DEFAULT.match_paths()`
  mode that weights characters after a path separator.
- Empirical (session-analytics over ~1,649 path-like tilth calls): ~0.4% miss rate; of
  genuine misses, **0 transposition/substitution typos**, the recoverable ones were
  WRONG_DIR / missing-prefix (e.g. `_mcp_core.py` → `src/milknado/_mcp_core.py`); ~70%
  were unrecoverable (stale worktree paths, moved/nonexistent files).
- External taxonomy (briesearch): LLM path errors are structural (wrong dir, partial,
  plausible-nonexistent), not character-level — LLMs emit tokens, not keystrokes.

Conclusion driving the matcher choice: a subsequence/path-aware matcher (nucleo) is the
right primitive. Its one blind spot — transposition typos — is an error class agents
essentially never produce, so no edit-distance cascade is needed. This is **DX insurance,
not a high-impact fix**: it addresses a small, low-frequency slice. Build it lean.

## Contract

Add a single fuzzy path resolver and wire it into the three NotFound sites. On a missing
path-like query, walk the gitignore-pruned tree, score every file's scope-relative path
against the query with `nucleo-matcher` (`match_paths`), and — when a confidence gate
passes — auto-open the winning file with an explicit correction header. When the gate
does not pass, fall back to ranked suggestions; when nothing is a subsequence match,
behave exactly as today (unchanged NotFound).

nucleo's `fuzzy_match` returns `None` unless the query is a subsequence of the candidate.
That is the hard filter that correctly rejects stale/garbage/nonexistent paths (the ~70%
unrecoverable slice) — they never auto-resolve.

### Seam

New module `src/read/fuzzy_path.rs`. The matcher lives entirely behind this function so a
future step-down (see Non-goals) is a body swap, not a rewrite.

```rust
pub struct FuzzyHit { pub path: PathBuf, pub score: u16 }

pub enum FuzzyResolution {
    Resolved(FuzzyHit),       // gate passed → caller may auto-open
    Suggestions(Vec<String>), // ambiguous / low-confidence → "did you mean" list
    None,                     // no subsequence candidate → unchanged NotFound
}

pub enum GateProfile { Read, Search }

/// Cold-path only — never called on a successful read. Reuses `search::walker(scope, None)`
/// for gitignore-aware enumeration.
pub fn resolve_fuzzy_path(scope: &Path, query: &str, gate: GateProfile) -> FuzzyResolution;
```

### Behaviour by call site

- **Read** (`src/read/mod.rs:42`): `GateProfile::Read` — broad whole-tree walk, lenient
  gate. `Resolved` ⇒ read the resolved file and prepend a header:
  `# <real-path> (corrected from "<query>")`. `Suggestions` ⇒ populate `NotFound.suggestion`.
  `None` ⇒ current behaviour.
- **Search fallthrough** (`src/lib.rs:433` and `src/lib.rs:481`): `GateProfile::Search` —
  **tighter**. Auto-open only when (a) the query is unambiguously path-like (contains a
  separator AND has a file extension) and (b) there is a clear single-winner margin.
  `Resolved` ⇒ open the file with an explicit, distinct header that states the switch:
  `# <real-path> (resolved from path-like query "<query>") · no search matches; closest file auto-opened`.
  Otherwise `Suggestions` feed the existing suggestion field. `None` ⇒ current behaviour.

The header text differs between Read and Search on purpose: a file appearing from a
*search* call must announce why, so the agent is never confused about the response shape.

### Confidence gate

- Candidate must be a nucleo subsequence match (score `Some`).
- `Resolved` iff: unique candidate, OR `top.score >= second.score * MARGIN` AND
  `top.score >= MIN_SCORE`. Otherwise `Suggestions(top_k)` with `k = 3`.
- `GateProfile::Search` applies a stricter `MARGIN`/`MIN_SCORE` and the path-like
  precondition above.
- All threshold constants (`MARGIN`, `MIN_SCORE`, search vs read variants, `k`) live in
  one `const` block at the top of `fuzzy_path.rs`, defaulted then tuned against the
  acceptance fixtures. No magic numbers scattered across the gate logic.

### Candidate enumeration

Reuse `search::walker(scope, None)` (the parallel gitignore-aware `ignore` walker already
used by `glob.rs:35`). Collect scope-relative path strings for files only. Bound the
candidate set with a `const MAX_FUZZY_CANDIDATES`; if the walk hits the cap, stop and
`log`/note the truncation in the response — never silently cap (per project rules).

## Acceptance

- `resolve_fuzzy_path` resolves a WRONG_DIR / missing-prefix case
  (`_mcp_core.py` → `src/milknado/_mcp_core.py`, synthesized fixture) to `Resolved`.
- Resolves a PARTIAL/basename-only query (`symbol.rs` → `src/search/symbol.rs`) under
  `GateProfile::Read`.
- Resolves a DELETION typo (`serch/symbol.rs` → `src/search/symbol.rs`).
- Returns `Suggestions` (NOT `Resolved`) for an ambiguous basename with two equally-good
  candidates (e.g. two `mod.rs`), under both profiles.
- Returns `None` for a garbage/stale path that is not a subsequence of any file
  (the unrecoverable slice stays an unchanged NotFound).
- `GateProfile::Search` does NOT auto-open a non-path-like query (no separator/extension)
  even when a fuzzy candidate exists — it suggests instead.
- `read_file` auto-open path prepends the `corrected from` header; the search auto-open
  path prepends the distinct `resolved from path-like query … auto-opened` header.
- No regression: an exact existing-path read does no walk and is byte-identical to today;
  happy-path reads/searches never invoke `resolve_fuzzy_path`.
- `cargo test`, `cargo clippy -- -D warnings`, `cargo fmt --check` all green.

## Non-goals

- **No edit-distance / transposition recovery.** Settled by evidence: agents don't make
  character-transposition path errors. The existing `strsim`-based `suggest_similar` and
  `edit_distance` stay as-is for now (and `strsim` remains required by heading suggestion
  in `collect_atx_headings`); this spec does not remove them.
- **No `path`/`paths` (or `query`/`queries`) alias leniency** — that API was just
  tightened; re-loosening now is churn, out of scope.
- **No change to `classify.rs`.** Path-like misses keep flowing through `Fallthrough`;
  resolution happens at the NotFound sites on the cold path, so `classify` stays a cheap
  byte-matcher with no filesystem walk added.
- **No basename pre-filter / index optimization in v1.** A whole-tree walk on the cold
  miss path is acceptable; narrowing candidates by basename is a noted follow-up only.

## Step-down (documented contingency — NOT built)

If nucleo proves wrong — unacceptable dependency weight, or the whole-tree walk is too
slow on very large repos — swap the *body* of `resolve_fuzzy_path` (same signature) to a
no-dependency basename-anywhere match over the same `search::walker`: exact-basename hits
first, then the existing `edit_distance` as a tiebreaker, with an absolute distance
budget (which also makes the confidence gate easier to tune than nucleo's relative score).
This covers the same WRONG_DIR / missing-prefix cases the logs show, at the cost of the
partial-path ranking nucleo gives. Do not build both; nucleo ships, this is Plan B.

## Build notes

- Add `nucleo-matcher` to `Cargo.toml` (the standalone matcher crate, not the full
  `nucleo` runtime). Pin a version; verify the `Config::DEFAULT.match_paths()` and
  `Pattern`/`Matcher` API against the installed version before coding.
- Version bump touches both `Cargo.toml` and `npm/package.json` per project rules if a
  release follows.
