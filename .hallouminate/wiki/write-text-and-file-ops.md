# `tilth_write` text-anchored and file-creation ops

`replace_text` and `create_file` joined the JSON op vocabulary in PR #177;
PR #179 added the one-sentence block-op teaching to the tool description.
The ops are small, but almost every invariant below was settled by review
*after* a first implementation that looked right and was not — this page is
the record of which mistakes are already paid for. The surrounding model
(whole-file tag, seen-lines gate, 3-way-merge recovery) is unchanged and is
permanent fork law; see [Edit-anchor design](edit-anchor-design.md) and
[ADR — tilth_write JSON-native ops](adr/tilth-write-json-ops.md).

## Uniqueness is counted over *overlapping* occurrences

`find_text_span` (`src/edit/apply.rs`) is the whole safety story for
`replace_text`: an `old` that matches once is a span, anything else is an
error. The first implementation counted non-overlapping matches, so
`old: "---"` against `"----"` counted **one** occurrence — the uniqueness
guard failed open and silently applied one of several candidate spans. The
scan now advances past only the first char's UTF-8 width, so a
self-overlapping needle is correctly ambiguous.

Consequence to keep in mind when reading errors: the scan **stops at the
second hit**, so `TextAmbiguous { count: 2 }` is a lower bound, not a total.
That is deliberate — `old` is caller-supplied and bounded only by the
snapshot cap, and counting every occurrence is O(n·m): measured **187s for a
200KB `old` against a 400KB file**, enough to wedge the single-threaded
server. An empty `old` is rejected at both the JSON schema and the resolver
(`TextOldEmpty`) rather than being allowed to match every byte boundary.

## Same-line swaps coalesce; overlapping byte ranges still error

Text swaps lower onto the same line-addressed op machinery as everything
else, so two *disjoint* `replace_text` ops on one line both produced the same
covering line span and collided in `reject_overlaps` — a conflict invented by
the lowering, not present in the edit. `lower_text_swaps` groups swaps whose
covering line spans touch into a single `LineOp::Swap`; only genuinely
overlapping byte ranges error.

Two subtleties worth not re-deriving:

- Runs are extended by "next swap's start line ≤ the run's last end line",
  not by an exact `(start_line, end_line)` key. Exact keying splits two swaps
  that share a start line but differ in end line — precisely the pair that
  coalescing exists to join.
- Resolved swaps are sorted by byte start before grouping, so the pair named
  in an `Overlap` error is deterministic. Grouping through a `HashMap` left
  it iteration-order dependent — the same class of bug as
  [diff symbol-order nondeterminism](diff-symbol-order-nondeterminism.md).

## A failed text anchor must not be reported as tag drift

Both recovery strategies in `src/edit/recovery.rs` discard their
`ApplyError`, so a `replace_text` whose `old` no longer resolves against the
live file surfaced as **bare drift** — sending the agent to re-read a file
whose text simply lacks the anchor, a loop the re-read cannot break. The
recovery path now re-lowers against live purely to recover that diagnosis and
raises `MismatchError::TextMatch`, which carries the `ApplyError` itself so
callers branch on the variant instead of parsing rendered text
(`ApplyError::is_text_match_failure` lives beside the variants so a new
text-anchor error cannot silently fall through a consumer's match).

The re-lowering is gated on the section actually containing a `TextSwap`:
lowering a block op re-parses the outline uncached (~86ms on 735KB of Rust)
to produce a diagnosis it cannot produce.

## `create_file` commits by `hard_link`, not by rename

`create_file` deliberately does **not** reuse `atomic_write_bytes`' stage-then-
`rename` pattern (`src/util.rs`): `rename` replaces an existing destination,
and staging through `fs::write` at a predictable temp name follows a symlink
someone planted there. `atomic_create_bytes_no_replace` instead opens the
temp with `create_new(true)` (`O_CREAT|O_EXCL` — the exclusive open is what
makes the staging write safe) and commits with `hard_link`, which fails on an
existing destination rather than replacing it. Both guarantees then hold: a
failed or interrupted write leaves the destination absent, and an existing
destination is never clobbered.

That atomic create is also the **only** "already exists" gate. The
`path.exists()` pre-checks that once guarded it followed symlinks and were
racy (a dangling symlink at the target defeated them), so they were deleted
rather than kept as a friendlier early error.

Three ergonomics rules that look arbitrary and are not:

- A `create_file` section must be **tagless** (`src/mcp/tools/write.rs`).
  There is nothing to tag on a file that does not exist yet, and a tagged
  section means the agent meant `replace` — so it is a teaching refusal, not
  a silent tag ignore.
- The create arm emits the fresh `[path#TAG]` header (not the body the agent
  just authored) and records the snapshot, so the next edit needs no re-read.
  Without it an agent had to re-read a file it had just written — and
  `replace_text` requires a tag, so the friction was immediate.
- `create_file` creates missing parent directories, matching `move_file`.
  Before that a missing parent surfaced as `NotFound` **attributed to the
  target path** — "no such file" for the file being created, which reads as
  nonsense.

## Open

- Issue #175 — a `replace_all` escape hatch for `replace_text`; today the
  uniqueness guard has no opt-out, so a deliberate repo-wide substitution
  must be spelled as one op per site.
- `<speculative>` The dominant invented field name in the July write-error
  taxonomy was `find` (the find/replace mental model) — see
  [usage analytics](usage-analytics-2026-07.md). `replace_text` gives that
  mental model a legal op for the first time, so the op-shape error share
  should fall; nothing has re-measured it since #177 merged.
