# Stock A/B trajectory diagnosis

In the [November 2026 stock-prompt A/B](benchmark-gepa-v3-evolve.md#november-2026-stock-prompt-ab), tilth lost to the stock arm only on the four chain tasks.
Tool behavior, not the prompt, drives most of that gap: ambiguous search output, unused continuation hints in every search, a seen-line record that resets between chain sub-tasks and disagrees with signature-mode output, and tag-less `replace_text` rejections.
A preamble cannot remove these causes. Fix the tools first, then steer.

## Data and dedup rules

- Source: `benchmark/results/stock-ab/rows_streams/` (untracked) at candidate `aec0efd4`, `claude-sonnet-5-5`, CLI 2.1.310, `--strict-file-tools --bare`.
- One stream name, minus its numeric prefix, can exist in several run dirs. The 12 `chain:rg-edit-6` cells re-ran after the grader fix, and some cells re-ran after interrupts.
  Keep the file from the run dir that the stored row's `trajectory_path` names. That is the attempt whose row the result store kept, not a newer partial attempt from an interrupted rerun.
  For a chain row, it names the `.turn1` trajectory; take the base and `.turnN` streams from the same run dir.
  In this A/B, that run dir is also the latest one for all 216 rows.
- A chain cell has one base stream and one `.turnN` stream per sub-task. The base stream repeats the turn streams, so count only the turn streams.
- An API call is a distinct assistant `message.id`.
- A first pass without these rules counted chain output twice and over-counted API calls. Its numbers, for example "5–12% more API calls", are wrong.

## Where the gap is

Per chain cell, `tilth_stock` against `baseline_stock` (12 cells each):

| measure | baseline_stock | tilth_stock |
|---|---|---|
| API calls | 33.8 | 34.3 |
| read output | `Read` 87.5k chars in 28.4 calls | `tilth_read` 89.5k in 9.8 |
| search output | `Grep` 22.2k in 11.9 | `tilth_search` 61.6k in 11.2 |
| edit output | `Edit` 9.1k in 67.5 | `tilth_write` 3.4k in 8.2 |
| Bash output | 4.7k | 6.8k |

- Net extra tool output is 37.6k chars per chain cell. Search alone adds 39.4k. Reads add 2.0k, and edits save 5.9k.
- Single tasks are at parity: 14.1 against 14.5 API calls, and $0.27 per cell for both arms.
- Context carries forward. In `chain:rg-edit-6` rep 0, the edit sub-task started at 72k context tokens for tilth against 48k for baseline. Every later call re-reads the extra 24k that the five read sub-tasks left.

## Causes found in the trajectories

1. **Ambiguous search fallback.** In `chain:rg-edit-6` rep 0, sub-task 1 (`rg_trait_implementors`), the query `trait Matcher` resolved `ambiguous`, because the six `impl Matcher for` blocks count as definitions of `Matcher`.
   The response searched `Matcher` as text. It printed a whole-file overview of an unrelated file (`crates/pcre2/src/matcher.rs`, its builder methods) and 28 usages: 9.3k chars.
   A regex query in the same batch, `impl.* Matcher for`, returned the 6 correct hits in 821 chars.
   Baseline answered with one `Grep` (450 chars) and one 90-line `Read`, in 3 API calls against tilth's 6.
   Ambiguous results average 1.1–1.3 per chain cell across the tilth arms (7.5–11.9k chars, up to 19% of chain search output).
2. **One invalid structural pattern fails the whole batch.** The first search of that sub-task sent four queries. One pattern did not parse, so the call returned only `invalid structural pattern` for all four. This happened in 2–3 chain batches per arm.
3. **The seen-line record resets between chain sub-tasks.** Each sub-task runs as a new `claude -p --resume` process (`_chain_turn_argv` in `benchmark/run.py`), so it starts a new MCP server with an empty record. The agent still holds the earlier reads and tags in context.
   In rep 0, sub-task 3 displayed `crates/searcher/src/searcher/glue.rs` lines 11–300 and 310–360. The edit sub-task's `tilth_write` at line 166 was rejected (`displayed: 58, 1-15`). The agent re-read lines 1–352 in `mode=full` (13.3k chars) and retried.
   Rejections run 0.33–0.67 per chain cell against 0.07–0.14 per single cell.
   Real sessions also restart the server on `--resume`, so this is not only a harness artifact.
4. **A signature-mode section read prints every line but records only the signature lines.** In the same edit sub-task, `tilth_read` of `crates/matcher/src/lib.rs#546-732` with `mode=signature` printed all 187 numbered lines under the tag. The next rejection listed only lines 629 and 648 as displayed.
   Two `replace_text` writes at line 635 were rejected, and the agent re-read lines 625–732 in `mode=full`.
   This is observed in the stream and not yet reproduced on main. Related: #305.
5. **`replace_text` is gated by seen lines.** An exact `old` match already proves that the agent holds the text. Both rejections in cause 4 were `replace_text` ops.
6. **`mode=full` compliance decays in long sessions.** `mode=full` is 2–6% of `tilth_read` calls in single tasks and 18–30% in chains (30–41% of chain read chars).
   The `prompts/mcp.md` rule reaches every arm: request captures show it in the first message.
   Appending the preamble lowered the rate (stock 26%, steer 18%). The replace arm did not lower it (30%).
7. **Continuation hints ride every search and are never used.** Each resolved definition carries four hints (`fetch_callers`, `fetch_callees`, `fetch_siblings`, `fetch_tests`), and each repeats the full target object.
   Hints are 14% of chain search output and 8% of single-task search output. No tilth arm sent a `follow` query in any stored stream.
   The rest of the chain search output is the preview text (52%) and resolved definition bodies in `core` (20%). `items` and `candidates` are under 2%.
8. **`replace_text` without a tag is rejected, though the server names the tag.** This is the most common `tilth_write` error: 111 times in 162 tilth cells (0.56 per chain cell, 0.72 per single cell).
   The error reads `replace_text/rewrite needs a tag; this session already displayed [path#EEAB]. Retry with tag "EEAB"`. Each one costs an API round trip.

## Levers to beat the stock arm

Ordered by evidence. Prompt steering cannot remove causes 1–5, 7 or 8, so the tool fixes come first.

1. **Fix the seen-line record (causes 3–5).** Record exactly the lines that a response prints. Accept a `replace_text` whose `old` matches exactly, without the gate. Optionally, persist displayed ranges per path and tag in a cwd-scoped state file, so a resumed session keeps them. Each fix removes a rejection round trip and a full re-read.
   For cause 8, apply a tag-less `replace_text` with the session's current tag for that path, when the `old` text matches exactly once.
2. **Resolve search ambiguity (cause 1).** Honor a kind keyword in the query (`trait`, `struct`, `fn`, `impl`). Do not count an `impl X for Y` block as a definition of `X`. On ambiguity, return the candidate list only, with no file overview and no usage dump. This overlaps idea 1 (explicit search modes) in [benchmark-gepa-v3-evolve](benchmark-gepa-v3-evolve.md#ideas-to-push-cost-per-correct-lower).
3. **Report errors per query in a search batch (cause 2).**
4. **Compact usage hits.** Print one line per usage hit by default, as `Grep` content mode does, and expand only definitions. Measure this against the 4-line construct context of #416, which made 12% of search-read-write sequences read-free for 7% more search output.
5. **Drop continuation hints from default search output (cause 7).** Hints cost 14% of chain search output and no agent followed one. Emit them on request, or as one short line per result. This extends #238, which targets the duplicated `items` and `preview` payloads (under 1% here).
6. **Steer after levers 1–5 land.** Put these lines near the top of `prompts/mcp.md`:
   - "Lines from an earlier read stay editable with `replace_text`; do not re-read them to edit." This is true only after lever 1.
   - "To list where something occurs, use a regex query; it prints one line per hit."
   - Keep the `mode` rule, and enforce it in the tool where possible.
7. **Trim the preamble.** Drop the Tools lines that repeat `prompts/mcp.md`, about 300 tokens per call. The effect is small.

## Earlier ideas and issues against these causes

| earlier idea or issue | cause it meets | would it have helped here |
|---|---|---|
| Idea 1: explicit search modes | 1 | Yes. In the same batch, the regex query returned the 6 correct hits in 821 chars against 9.3k for the auto-resolved query. |
| Idea 2: edit from search (#416, landed) | none | No. It adds search output, and chain edits failed on the seen-line record, not on missing search context. |
| Idea 3: exact-text edits | 5, 8 | Only with lever 1. `replace_text` exists, but the gate and the tag rule reject it. |
| Idea 4: coalesce reads (#415, landed) | none | No measurable effect: reads are at parity with `Read` (89.5k against 87.5k chars per chain cell). |
| Idea 5: malformed `tilth_write` payloads | 8 | Yes. The tag-less `replace_text` error is its current form, at 111 errors. |
| Idea 6: batch discovery | 2 | Agents already batch. One bad pattern then fails the whole batch, so lever 3 is needed first. |
| Idea 7: smaller schemas and instructions | prefix | Partly. The prefix is 10–23% of the extra context, and the `tilth_write` schema is the largest part. |
| #238: search contract shape | 7 | Partly. The duplicated `items` and `preview` are under 1% here; the unused hints are 14%. |
| #305: truncated reads marked as seen | 4 | The same fix (record only displayed lines), in the opposite direction. |
| #187: placeholder tags and recovery errors | 8 | Yes, if the recovery applies the named tag instead of asking for a retry. |
| Preamble steering (append or replace) | 6 | Partly. Appending lowered chain `mode=full` from 26% to 18% of reads, but no preamble arm beat the stock tilth arm on cost. |

## How to test

- **Offline first.** `benchmark/evolve/replay.py` re-sends a recorded cell's tilth calls to a candidate binary with no model calls. It reports result chars by tool and new errors.
  It measures output size and gate rejections for the same calls. It cannot show how the agent would react.
  Two gaps: `_cells` selects only `tilth_forced` rows, and a chain row's `trajectory_path` names only the `.turn1` trajectory. It also replays into one persistent server, so it hides cause 3.
- **Minimal repro tasks.** Use `rg_trait_implementors` for cause 1 and the edit sub-task of `chain:rg-edit-6` for causes 3–5.
- **Paid check.** Run `chain:rg-edit-6` and `chain:express-edit-6`, `tilth_stock` against `baseline_stock`, with more reps. The chains carry the whole gap.

_Source: stream analysis of `benchmark/results/stock-ab/rows_streams/` (budget `stock-ab-1`), request captures of the four arms' first API call, `chain:rg-edit-6` rep 0 trajectories · Updated: 2026-11-04 · Supersedes: none_
