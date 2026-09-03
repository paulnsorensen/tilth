# Hints: advisory vs corrective (and why the "duplication" isn't real)

Session 2026-08-09. Landed in PR #184 (`src/hint.rs`).

## The split

tilth emits two kinds of agent-facing guidance. They are **not** interchangeable
and must not be unified:

- **Advisory** — success-path guidance appended to a normal result. Empty search
  hints (`format.rs`), `no_callers_message` (`search/callers.rs`), the grok
  scope-too-large note (`search/grok.rs`), batch nudges (`session.rs`). These now
  live in `src/hint.rs` as the `Hint` enum.
- **Corrective** — teaching errors that turn a failed call into a successful
  retry. `edit/mismatch.rs`, `edit/apply.rs`, `edit/json.rs`, baked into
  `thiserror` `#[error(...)]` variants. **These stay put.**

## Don't "DRY" the corrective messages

A recurring trap: the five re-read messages *look* duplicated. They are not.
Zero byte-identical pairs — they are five different instructions:

| Site | Instruction |
|---|---|
| `mismatch.rs` Drift | refresh the tag before retrying |
| `mismatch.rs` Fabricated | copy a current `[path#tag]` header, never invent one |
| `mismatch.rs` UnseenAnchor | re-read the region you intend to edit |
| `mismatch.rs` TextMatch | the file also changed since the tag was minted |
| `apply.rs` TextUnmatched | text not found; re-read and retry (+ preview) |

Same verb, different advice. The "widen/narrow scope" cluster is the same story —
`format.rs` says *try kind=content*, `callers.rs` says *widen scope*, `grok.rs`
says *your search was truncated*. Three unrelated messages.

Collapsing any of these is a **teaching-precision regression** in the subsystem
whose design ([[adr/tilth-write-teaching-errors]] ADR-001/002) is priced on one
accurate correction per failed call. A semantic-similarity scan will report these
as duplicates; open the actual strings before believing it.

## Why the table exists anyway

Not for DRY — nothing was deduplicated. For:

1. One place to audit and iterate advisory prose against haiku benchmarks.
2. The carrier a future verbosity knob (`TILTH_HINTS`, matching the
   `TILTH_TIMEOUT` / `TILTH_FULL_SIZE_CAP` env-var precedent) would gate.

## Constraints baked into the design

- **Static-only.** Anything interpolating a runtime value (`{target}`,
  `{scope_disp}`, `{preview}`) stays at its call site. `text()` returns
  `&'static str`; no template mini-language.
- **`EmptyHint` stays.** It is the dispatch key (which search kind ran), not the
  text. `search_empty_header`'s signature is unchanged; the mapping to `Hint`
  happens inside the body.
- **Byte-identical output.** The extraction changed no emitted string and edited
  no test. Keep that property on any follow-up.

## If you add the verbosity knob

`Display::fmt` takes only `&self` and cannot see a level. Making corrective hints
level-gated therefore requires moving remediation *out* of the error variants and
appending at the response boundary — `dispatch_tool` in `src/mcp/mod.rs` already
returns `Result<String, String>` and already appends advisory tips on the Ok arm
via `append_batch_nudge`. That is the natural seam. Note that `Off` must never
silence `tilth_write` teaching errors.

## Pre-existing gate failures (as of this session)

Unrelated to hints, confirmed against a clean tree at the merge base:

- `mcp::tests::batch_budget_represents_every_query` fails on `main`.
- `cargo clippy --all-targets -- -D warnings` reports an `unused variable: full`
  and a `doc_markdown` error in `src/mcp/tools/read.rs`. CI runs the non-
  `--all-targets` form, which is clean.
- `mcp::tools::diff::tests::relative_file_pair_anchors_under_cwd` is flaky under
  concurrent `set_current_dir` in the diff tests; passes alone and on repeat.
