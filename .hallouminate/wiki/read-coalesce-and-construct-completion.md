# Read coalescing and construct completion

`tilth_read` plans its `paths` before it renders them, so one call returns whole, unfragmented code.
The planner is `src/mcp/tools/read/plan.rs`; `tool_read_paths` and `construct_boundary` in `src/mcp/tools/read.rs` wire it in.
`ReadCall::parse` checks the arguments and parses each path once into a `Request` (path plus `PathSuffix`). The planner takes and returns `Request` values, and the renderer reads them without a second parse. `suffix_span` is the one resolver from a suffix to a line span, for the planner and for `read_single_with_suffix`.
The construct lookup is `cut_construct` in `src/lang/outline.rs`. It uses `innermost_entry`, the same innermost-entry walk that resolves a line anchor in `src/edit/block.rs`, and `deep_outline_for_path`, which needs no `OutlineCache`.
The grow-or-stop decision is `FileView::complete` in `src/read/complete.rs`. `FileView<C>` measures numbered lines with a `LineCost`: `plan::Escaped` counts the JSON-escaped `N:line` that `tilth_read` sends, and `Plain` counts the padded `N  line` that the CLI prints. `plan::FileView` is the alias `complete::FileView<Escaped>`.
It came from idea 4 on [gepa-v3 evolve campaign state](benchmark-gepa-v3-evolve.md).

## What the planner does

- **Coalesce.** Sections of one file that overlap or lie at most `COALESCE_GAP` (3) lines apart merge into one `path#a-b` section with one `[path#TAG]` header. The merged section takes the slot of its first request, so output order stays deterministic.
- **Complete.** An explicit `#n-m` range whose end cuts a construct grows to the construct's end when the whole response stays within the limit. The limit is the smaller of `budget` and the host cap, less the cut-note and header reserves and 160 bytes of framing per request.
- **Fall back.** When the growth does not fit, the range stops on the line before the construct and ends with `... Next: tilth_read(paths: ["path#start-end"])`. The JSON header names the same request in `next_view`: a string for a single-path read, and for a batch an array of the shortened sections' requests in section order (see [Batch `next_view`](#batch-next_view)). The range keeps its end, and the continuation covers the rest of the construct, in three cases: the construct alone would not fit, the construct began before the range, or a request of the section starts inside the construct.
- **Overflow.** When the section does not fit even after it stops, the planner keeps the request as written and adds no continuation. The host cut then names the rest of the requested range, so no requested line falls out of the `Next:` chain.
- **Host cut.** A response cut at `HOST_VISIBLE_MAX` inside a construct moves its cut back to the line before the construct, under the same "fits alone" rule. The existing cut note then names the construct's start.
- **Large files.** A file larger than `MAX_PARSED_FILE_BYTES` (500 KB) gets no construct lookup, because the parsed-file cache does not keep it and every call would parse it again. Its ranges never grow, and a host cut inside one of its constructs stays where it falls.

Seen-line provenance needs no new code: `record_shown` still records the numbered lines that each part's span of the final text shows.

## CLI `--section`

`tilth <file> --section N-M` completes a construct in the same way. `cli_section` in `src/read/complete.rs` calls `FileView::complete`, and `run_inner` in `src/lib.rs` reads the range it returns.

- The limit is `--budget` tokens times 4 bytes, less 200 bytes and two copies of the path for the header and the note. Without `--budget`, the CLI has no limit, so the range always grows to the end of the construct.
- When the growth does not fit, the read ends with `... Next: tilth <path> --section A-B`. The path is the absolute path that the header shows, so the call works from any directory.
- Only an `N-M` range completes. A heading section reads as before.
- `tests/section_cli.rs` runs the built binary: a range grows, a small budget stops before the function, and a Python range never grows to the root module.

## Batch `next_view`

A batch header lists a continuation only when the body shows its `Next:` note. `render_read` keeps each section's request in `Slot.next`, and `batch_meta` builds the array from the final layout:

- `shown_next` gives a section a continuation only when its view shows lines. A stub, an error, or an outline adds no note and no entry, the same as the single-path filter.
- A section that the batch budget clips loses its note, so it adds no entry. A section moved to `── omitted (raise budget) ──` and a batch that collapses to counts add none.
- The key is absent when no entry remains. The single-path shape (`next_view` as a string) and the note text do not change.
- The body budget uses the header with every planned entry. The final header names the same entries or fewer, so it never makes the response larger than the budget.

Both notes, the planner's `Continuation::note` and the host `cut_note`, format the call through the one `next_call` helper, so the two `Next:` spellings match.

## Why the gap is 3

A section costs about 25 tokens of framing, which is about three source lines.
In the gepa-v3 r1-r6 batches, same-file pairs 1-3 lines apart were neighbouring definitions (`ClientIP`/`RemoteIP`, gap 2).
The next cluster, at 8-20 lines, was deliberate picks: single lines 10 or 20 apart in `render/render_test.go`. A larger gap would chain those into one section and double the output.

## Gotchas

- Python's root `module` node parses into the deep outline as a whole-file `Module` entry. `cut_construct` therefore never counts a `Module`; without that, every Python range grew to end of file.
- A container (class, struct, impl, interface, enum) with child definitions is a clean stop between its members. A container without children, such as a struct with fields only, is a construct to complete.
- `mcp::tests::documents_direct_reads_do_not_parse` pins that a direct range read does not parse. The planner parses lazily, and only when a range ends before the file's last line.
- `ReadCall::parse` reports a malformed request (missing `cwd`, more than 20 paths, a non-string path, an unknown `mode`) before the planner runs. Its checks keep the order the renderer used, so a call with several faults reports the same error.
- A rewritten request is `Request::with_range`: its `raw` is `shown#start-end`, and its suffix is the `LineRange` that string parses to.
- Coalescing rewrites the request list, and `CutCtx.requests` must be the rewritten list, because `Part.request` indexes into it.

## Measured baseline (before the change)

Across 769 tilth-arm sessions and 2,656 `tilth_read` calls in gepa-v3 r1-r6, there were:

- 275 continuation reads of the same file, 38 of them after a tilth cut;
- 504 consecutive reads that extend a same-file span by overlap or a gap of at most 3 lines;
- 10 batch calls with same-file sections that overlap (7) or lie at most 3 lines apart (3).

The measurement script and its definitions are in the body of PR #415.

_Source: PR #415 (branch t3/coalesce-complete-read-ranges); batch `next_view` from branch t3/batch-next-view-header · Updated: 2026-10-30_
