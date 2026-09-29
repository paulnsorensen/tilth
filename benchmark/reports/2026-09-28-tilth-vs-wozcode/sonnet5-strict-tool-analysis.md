# Strict Sonnet 5 tool usage

## Main findings

Both MCP arms batch useful work and complete every task cell.
Tilth makes fewer calls than WOZCODE, but returns more text and processes more context.
WOZCODE emits compact edit receipts and can replace repeated text in one operation.
These observations suggest mechanisms. They do not isolate the cause of the performance difference.

## Method and coverage

Use /Users/paul/.agents/skills/session-analytics/SKILL.md and its canonical DuckDB schema.
Database: .context/sonnet5-analytics/sessions.duckdb.
Only the 15 strict streams from 20260928_202801 enter this database.
Normalized copies map session_id to sessionId and preserve full messages and existing timestamps.
No timestamps are invented. The raw streams remain unchanged.
An empty HOME prevents ingestion of unrelated personal logs.

The database contains 5,010 canonical entries and 1,335 unique calls across 15 sessions.
Every call joins one result using harness, sessionId, and tool_use_id.
All assistant model records name claude-sonnet-5. No agents or skills are invoked.
Calls and results contain timestamps, but they do not expose server execution spans.
This report does not infer exact MCP latency or actual concurrency from emission times.

Canonical tool_results truncates content at 500 characters. Response measurements use full raw_entries instead.
Character counts exclude JSON transport escaping and combine text blocks. They are not tokenizer measurements.
Queries and outputs 01–08 remain beside the database.
Query 05 initially fails because mode is a reserved SQL word. The corrected query uses read_mode and passes.

## Tool mix

Counts include unsuccessful attempts and the incomplete native timeout trace.

| Arm | Total calls | File/intelligence tools | Bash | Multi-call assistant messages |
|---|---:|---|---:|---:|
| Native | 765 | 283 Edit, 151 Read, 93 Grep, 36 Glob, 1 Write | 201 | 88 |
| Tilth | 277 | 48 read, 38 search, 38 write, 15 list, 4 grok, 3 diff | 131 | 23 |
| WOZCODE | 293 | 113 Search, 45 Edit, 8 unavailable short-name Search attempts | 127 | 54 |

Tilth exposes distinct read, search, write, list, grok, and diff operations.
WOZCODE uses Search for both discovery and source reading, plus Edit for changes.
The eight bare Search attempts all return “No such tool available”. They are not successful fallback reads.
Neither MCP arm calls native Read, Edit, Write, Grep, or Glob.

## Batching shapes

| Operation | Calls | Multi-item calls | Items | Largest batch |
|---|---:|---:|---:|---:|
| Tilth read paths | 48 | 26 | 149 | 16 paths |
| Tilth search queries | 38 | 31 | 97 | 6 queries |
| Tilth write file sections | 38 | 15 | 104 | 13 files |
| WOZCODE Search patterns | 113 | 30 | 198 | 13 patterns |
| WOZCODE Edit replacements | 45 | 34 | 242 | 30 replacements |

These item counts are not equivalent units.
WOZCODE groups replacements, including several replacements in one file. Tilth groups file sections, each containing operations.
WOZCODE has 12 multi-file edit calls and 110 file targets across calls. Its largest edit covers 13 files.
Tilth has 15 multi-file write calls and 104 file sections. All 104 sections report applied.

Tilth emits 300 replace_text operations and 123 line-replace operations.
WOZCODE emits 222 single replacements and 20 replace_all operations.
One WOZCODE receipt reports 30 replaced occurrences from one replace_all operation.
Its batch operation therefore removes repeated instruction text as well as tool calls.

Both arms also emit multiple separate tool calls in some assistant messages.
This is different from a multi-file array within one call. Neither proves that the server executes work concurrently.
The older wiki finding of zero multi-call Sonnet turns does not describe these instructed migration traces.

## Read arguments

Tilth requests 149 paths: 92 plain paths and 57 numeric ranges. It uses no symbol suffixes in this run.
It omits mode in 43/48 calls and requests full mode in five calls.
None of its read calls sets a custom budget.
Therefore, line-range reading is common but not dominant here. These counts do not describe agents generally.

WOZCODE uses file_glob_patterns arrays, optional content_regex, and optional surrounding-line counts.
It also accepts range-style patterns, such as context.go#1140-1320, in the observed successful calls.
Its Search calls request content 83 times, paths only 13 times, and match counts three times.
Fourteen calls omit output_mode. Fifty-one calls include content_regex.
One mistaken file_path argument fails; Search requires a supported search parameter.

Tilth repeats six exact path requests within sessions: 149 total versus 143 distinct session/path pairs.
WOZCODE repeats 27 exact pattern requests: 198 total versus 171 distinct session/pattern pairs.
Different ranges, search regexes, and edits can make repeated file access necessary. These counts are not waste estimates.
Neither tool receives if_modified_since in these traces, despite timestamp hints in responses.

## Response shapes and sizes

| Tool | Total returned characters | Median per call | p90 per call | Largest |
|---|---:|---:|---:|---:|
| Tilth read | 426,069 | 3,445 | 23,447 | 45,585 |
| Tilth search | 119,092 | 2,693 | 6,364 | 9,936 |
| Tilth write | 45,925 | 513 | 4,040 | 5,498 |
| WOZCODE Search | 441,533 | 1,221 | 14,453 | 33,753 |
| WOZCODE Edit | 16,192 | 218 | 969 | 1,390 |

All tilth MCP responses total 632,676 characters. All WOZCODE MCP responses total 457,725 characters, 27.65% less.
The tools return different content in different calls. This is not a controlled compression ratio.

Tilth reads return headings, absolute paths, file tags, numbered source lines, and timestamps.
Tilth searches return JSON envelopes with previews, completeness, diagnostics, and continuation hints.
Tilth writes return per-file applied status, updated tags, source excerpts, and omitted-line notices.
All 38 write responses contain omitted-line notices. These notices describe abbreviated write receipts, not failed edits.

WOZCODE Search returns XML-like cwd, search_result, and file wrappers around content or search matches.
Its whole-file example contains source without per-line numbering.
WOZCODE Edit returns relative paths, adjusted line ranges, edit counts, and replacement summaries instead of source excerpts.
The largest WOZCODE edit receipt is 1,390 characters; the largest tilth write receipt is 5,498.

No tilth read or WOZCODE Search response contains the inspected truncation, next_view, omitted-lines, or budget markers.
This does not mean every search returns an entire file. Explicit ranges and output modes intentionally restrict content.

## Errors and recovery

Tilth has four explicit MCP errors: one missing dependency file, one missing directory, and two unresolved sse.Event grok requests.
All 38 write calls report successful sections; no section rejection appears in their receipts.
WOZCODE has one MCP Search parameter error and eight unavailable bare Search attempts.
No WOZCODE Edit call reports an error in the inspected receipts.

In WOZCODE repetition 0, a failed bare Search is followed by the namespaced MCP Search and then a 13-file content batch.
In tilth repetition 0, a missing sse.go read is followed by directory listings that locate the dependency.
These are visible recovery sequences, not hidden successful fallbacks.

The common Bash guard denies 38 tilth calls, 42 WOZCODE calls, and 76 native calls.
Other Bash errors include temporary compile failures while the migration is incomplete.
The canonical permission_denials table reports zero because it misses this hook-error wording.
Full response inspection identifies the denials; zero in that derived table does not mean zero denials.

Native has eight Glob timeouts across five traces, plus malformed searches and wrong dependency paths.
These failures and guard recovery can affect the native comparison. Do not attribute the entire gap to batching.

## Implications

1. Keep batching. These traces demonstrate successful multi-file reads and writes.
2. Test compact tilth write receipts as a separate treatment. Preserve the tags needed for safe subsequent writes.
3. Consider repeated-text replacement semantics separately. WOZCODE replace_all handles repeated test call-site changes compactly.
4. Fix short-name tool confusion in the WOZCODE harness before treating it as irreducible model overhead.
5. Keep failures and guard denials in measured totals. Do not replace poor cells with successful retries.

These are follow-up candidates, not implemented changes or proven causal explanations.
Use additional tasks and a controlled receipt-format experiment before changing the default product strategy.

## Evidence map

- Coverage/model policy: 01_coverage.sql and .txt.
- Tool mix/error flags: 02_tools.sql and .txt.
- Argument keys and examples: 03_arguments.sql and .txt.
- Array batching and distinct edit files: 04_batches.sql and .txt.
- Read modes, ranges, and repeats: 05_reads.sql and .txt.
- Full response sizes: 06_response_sizes.sql and .txt.
- Error candidates: 07_errors.sql and .txt. Source-text keyword matches are not automatically classified as errors.
- Complete inspected response records: 08_shapes.sql and .json.
- Run outcomes: .context/sonnet5-strict-results.md.
