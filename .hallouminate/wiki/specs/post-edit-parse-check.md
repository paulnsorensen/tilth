# Post-edit parse check (history)

Shipped for `tilth_edit` in #21 (`5504a5f`, 2026-05-13). #99 (`77259ba`, 2026-07-03) deleted it with the per-line hash model.
The spec retired on 2026-09-06. Issue #221 is the open successor: an advisory parse check for `tilth_write`.
This page is a decision record, not live guidance.

## Proposal

`tilth_edit` wrote any content the model sent. A broken file stayed silent until the model ran `cargo check` or `tsc`.
The spec parsed the file after the write with the grammar from `outline_language()`.
It reported only the `ERROR` and `MISSING` nodes that the edit introduced.

- Hook: in `apply_edits`, after `fs::write` and before the diff block.
- Diff: multiset subtraction keyed by `(ErrorKind, detail)`. The key has no position, so shifted lines do not flag old errors.
- Output: a `── parse ──` block, one `:<line> <ERROR|MISSING> <detail>` row per error, cap 10 rows.
- Detail: error-node text cut to 40 characters, or the expected kind for `MISSING`.
- No grammar (Dockerfile, Make): no block, no failure.

## Decisions

- Advisory only. The write stays on disk and the result kind does not change.
- Reason: a multi-file rename can pass through valid broken states. A rollback also hides what the model tried.
- On by default, with no flag. The cost is below one millisecond.
- Out of scope: type checking, lint, JSON/YAML/TOML validation, per-edit attribution, and reports of fixed errors.

## What shipped and what did not

- #21 added `src/edit_parse_check.rs` (347 lines) and wired it into `src/edit.rs`.
- #99 removed the per-line hash edit path and deleted `src/edit_parse_check.rs` with it.
- The whole-file-tag model (#116) did not carry the check forward.

## Current status

`src/edit/` has no post-write parse check on this branch. Searches for `ParseReport`, `is_missing`, and `has_error` find no production use in `src/edit/` or `src/mcp/`.
Issue #221 ("Add an advisory post-write parse check to tilth_write") is OPEN.
The design above is a good start for #221. The hook point must move to the `tilth_write` apply path.

## Acceptance (for #221)

The seven cases below carry over to the `tilth_write` check:

1. Clean edit, clean file: no `── parse ──` block.
2. Clean edit, pre-broken file: no block. Pre-existing errors stay silent.
3. Edit introduces a syntax error: a block with the new error, anchored to its line.
4. Edit introduces 15 errors: list the top 10, then `... and 5 more (15 new, 15 total)`.
5. File with no grammar (Dockerfile, Make): no block, no crash.
6. Multi-file batch where one file breaks: only that file's section shows a block.
7. Edit fixes a pre-existing error: no block. Version 1 does not report fixes.

Overflow format: `... and N more (X new, Y total)`. `X` is the new-error count. `Y` is the total post-edit error count.
Sort rule: sort errors by `(line, col)`. `col` is a 0-indexed sort key and never appears in the output.

Version 2 candidates:

- Per-edit attribution: map each error to the edit range that contains it.
- Report fixed errors next to regressions.
- Structured parse for JSON, YAML, and TOML.
- Opt-out flag, only if benchmarks justify it.

## Successor

Issue #221.

_Source: former `.cheese/specs/post-edit-parse-check.md`; commits `5504a5f` (#21), `77259ba` (#99); issue #221 · Updated: 2026-10-13_
