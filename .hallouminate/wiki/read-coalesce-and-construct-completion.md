# Read coalescing and construct completion

`tilth_read` plans its `paths` before it renders them, so one call returns whole, unfragmented code.
The planner is `src/mcp/tools/read/plan.rs`; `plan_paths` and `construct_boundary` in `src/mcp/tools/read.rs` wire it in.
The construct lookup is `cut_construct` in `src/lang/outline.rs`. It uses `innermost_entry`, the same innermost-entry walk that resolves a line anchor in `src/edit/block.rs`, and `deep_outline_for_path`, which needs no `OutlineCache`.
It came from idea 4 on [gepa-v3 evolve campaign state](benchmark-gepa-v3-evolve.md).

## What the planner does

- **Coalesce.** Sections of one file that overlap or lie at most `COALESCE_GAP` (3) lines apart merge into one `path#a-b` section with one `[path#TAG]` header. The merged section takes the slot of its first request, so output order stays deterministic.
- **Complete.** An explicit `#n-m` range whose end cuts a construct grows to the construct's end when the whole response stays within the limit. The limit is the smaller of `budget` and the host cap, less the cut-note and header reserves and 160 bytes of framing per request.
- **Fall back.** When the growth does not fit, the range stops on the line before the construct and ends with `... Next: tilth_read(paths: ["path#start-end"])`. A single-path read also gets `next_view: "path#start-end"` in its JSON header. The range keeps its end, and the continuation covers the rest of the construct, in three cases: the construct alone would not fit, the construct began before the range, or a request of the section starts inside the construct.
- **Overflow.** When the section does not fit even after it stops, the planner keeps the request as written and adds no continuation. The host cut then names the rest of the requested range, so no requested line falls out of the `Next:` chain.
- **Host cut.** A response cut at `HOST_VISIBLE_MAX` inside a construct moves its cut back to the line before the construct, under the same "fits alone" rule. The existing cut note then names the construct's start.
- **Large files.** A file larger than `MAX_PARSED_FILE_BYTES` (500 KB) gets no construct lookup, because the parsed-file cache does not keep it and every call would parse it again. Its ranges never grow, and a host cut inside one of its constructs stays where it falls.

Seen-line provenance needs no new code: `record_shown` still records the numbered lines that each part's span of the final text shows.

## Why the gap is 3

A section costs about 25 tokens of framing, which is about three source lines.
In the gepa-v3 r1-r6 batches, same-file pairs 1-3 lines apart were neighbouring definitions (`ClientIP`/`RemoteIP`, gap 2).
The next cluster, at 8-20 lines, was deliberate picks: single lines 10 or 20 apart in `render/render_test.go`. A larger gap would chain those into one section and double the output.

## Gotchas

- Python's root `module` node parses into the deep outline as a whole-file `Module` entry. `cut_construct` therefore never counts a `Module`; without that, every Python range grew to end of file.
- A container (class, struct, impl, interface, enum) with child definitions is a clean stop between its members. A container without children, such as a struct with fields only, is a construct to complete.
- `mcp::tests::documents_direct_reads_do_not_parse` pins that a direct range read does not parse. The planner parses lazily, and only when a range ends before the file's last line.
- `plan_paths` returns `None` for a malformed request (missing `cwd`, more than 20 paths, a non-string path). The renderer then reports the error unchanged.
- Coalescing rewrites the request list, and `CutCtx.requests` must be the rewritten list, because `Part.request` indexes into it.

## Measured baseline (before the change)

Across 769 tilth-arm sessions and 2,656 `tilth_read` calls in gepa-v3 r1-r6, there were:

- 275 continuation reads of the same file, 38 of them after a tilth cut;
- 504 consecutive reads that extend a same-file span by overlap or a gap of at most 3 lines;
- 10 batch calls with same-file sections that overlap (7) or lie at most 3 lines apart (3).

The measurement script and its definitions are in the body of PR #415.

_Source: PR #415 (branch t3/coalesce-complete-read-ranges) · Updated: 2026-10-30_
