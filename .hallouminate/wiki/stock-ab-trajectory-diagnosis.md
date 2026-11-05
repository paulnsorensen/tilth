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

## Litmus re-run after the fixes (2026-11-04, 2026-11-05)

#433, #434, #435 and #436 fix causes 2, 3, 4, 5 and 8, and narrow cause 1. Two paid re-runs of `chain:rg-edit-6`, `tilth_stock`, 3 reps, at main `a9ab01f4` test them. **With `--bare`, as in the November 2 rows, tilth costs $2.17 per cell against $2.46 for baseline (−12%).** Before the fixes the gap was +30%. The first re-run omitted `--bare` and cost $2.62.

| Per cell, 3 reps | baseline_stock (Nov 2) | tilth_stock before | after, no `--bare` | after, `--bare` |
|---|---|---|---|---|
| `--bare` | yes | yes | no | yes |
| Correct | 3/3 | 3/3 | 3/3 | 3/3 |
| Cost (range) | $2.46 ($2.16–2.80) | $3.19 ($2.82–3.54) | $2.62 ($2.53–2.76) | $2.17 ($2.03–2.26) |
| Context tokens | 4.47M | 6.28M | 5.95M | 4.17M |
| First API call context | 7.4k | 9.3k | 11.5k | 9.3k |
| API calls | 51.7 | 56.7 | 60.7 | 52.3 |
| Actions per tool-using API call | 3.87 | 4.61 | 4.23 | 3.03 |
| Bash calls | 6.0 | 7.7 | 13.3 | 20.3 |
| `tilth_write` calls | n/a | 15.3 | 13.3 | 5.0 |
| `tilth_write` errors | n/a | 3.00 | 0.33 | 1.33 |
| Seen-line rejections | n/a | 2.67 | 0.33 | 0 |
| `mode=full` reads | n/a | 7.67 | 0 | 0 |
| Batches killed by a bad pattern | n/a | 2 (total) | 0 | 0 |
| Read result chars | 190k | 209k | 157k | 139k |
| Search result chars | 46k (`Grep`) | 98k | 87k | 57k |

- **Tilth is at parity or better.** n=3 and the cost ranges overlap, so the 12% is not significant. Context tokens are 7% below baseline, and API calls are equal.
- **Two of three `--bare` reps edit through the shell.** Under `--bare` no hook blocks shell edits. In turn 6, the `bool` to `Flow` migration, rep 0 makes 17 `perl -pi` edits and no `tilth_write` call. Rep 2 writes 11 Python edit scripts under `/tmp/mig`. Only rep 1 edits through `tilth_write` (14 calls). It costs $2.23, inside the baseline range. The November 2 tilth rows have no shell edits, and these runs do not show the cause.
- **Batching is not the gap.** Count actions, not tool calls. An action is one read path, search query, edit op, or built-in tool call. Tilth batches inside one call: 2.05 paths per read, 3.07 queries per search and 8.85 ops per write (no `--bare` run). Baseline sends parallel calls instead. Session analytics `model_turns.tool_calls` counts calls, so its 1.1 against 3.6 hides this. Shell edits count as one action each, so the `--bare` run falls to 3.03.
- **The first re-run used the user's harness.** Without `--bare`, `run.py` does not pass `--setting-sources ""`. The arm then ran the user's SessionStart hooks and loaded `~/.claude/CLAUDE.md`. Of 40 Bash calls, 28 use `eatmydata`, a rule from that file. Most of the extra Bash calls run `cargo check` or `cargo test`. The `--bare` re-run confirms the confound: the first call falls from 11.5k to 9.3k tokens, and API calls fall from 60.7 to 52.3.
- **Other gap sources.** Tilth search results are still larger than `Grep` results: 57k against 46k characters per cell. The tilth tool schemas add about 1.9k tokens to each API call.
- **Cause 1 is smaller, not gone.** Ambiguous search results fell from 3.67 per cell to 3.00 (no `--bare`) and 1.33 (`--bare`).
- **Setup gotchas.** `run.py` keeps only an allowlist of environment keys, so `XDG_STATE_HOME` does not reach tilth. The seen store then writes to `~/.local/state/tilth/seen`, one file per cell checkout. The CLI pin 2.1.310 had to be reinstalled with `mise install aqua:anthropics/claude-code@2.1.310` and passed through `TILTH_BENCH_CLAUDE`. Pass `--bare` to match the stock A/B rows. `bare` is part of the run key, so a `--bare` run does not reuse rows without it.
- **Limits.** n=3. The baseline rows come from November 2, not from the same day. The first re-run started before #432, so the `/tmp` aging guard did not apply. Spend: $7.87 (budget `litmus-rg-edit-6`) and $6.52 (budget `litmus-rg-edit-6-bare`).

_Source: stream analysis of `benchmark/results/stock-ab/rows_streams/` (budget `stock-ab-1`), request captures of the four arms' first API call, `chain:rg-edit-6` rep 0 trajectories, the 2026-11-04 and 2026-11-05 litmus streams and rows (`bare`, `git_sha`), and session-analytics `model_turns` · Updated: 2026-11-05 · Supersedes: none_
