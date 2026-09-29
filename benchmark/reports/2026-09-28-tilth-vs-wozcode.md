# Tool Efficiency Report: tilth versus WOZCODE

## Summary

Target: tilth MCP. Harness: Claude Code, Sonnet 5 high.
Domains: mcp-health, error-forensics, fix-recommendations, plus canonical session queries and source inspection.
Four findings surface. Three alternatives remain below the bar.
No production code changes or new model benchmark calls occur during this investigation.

Tilth is already competitive on correctness: both MCP arms pass five of five cells.
WOZCODE averages 29.89 seconds less agent time and $0.2607 less reported cost per cell.
Across five repetitions, WOZCODE uses 18.42% less processed context and costs 17.22% less.
These are task-specific observations, not estimates of the effect of any proposed fix.

## Recommendations

| # | Severity | Confidence in observed issue | Domain | Issue | Recommendation |
|---|---|---|---|---|---|
| 1 | High | certain | Search semantics | Pasted Go signatures silently miss existing code in all five runs | Recognize declaration-shaped input or return a labeled, bounded literal fallback |
| 2 | Medium | certain | Response representation | Repeated absolute cwd prefixes consume 51,944 response characters | Render in-checkout paths relative to the explicit cwd; retain tags and displayed-line safety |
| 3 | Medium | certain | Edit expressiveness | Repeated migration edits require many single-match or line operations | Add explicitly requested, counted multi-occurrence replacement with the existing safety checks |
| 4 | Medium | certain | Response representation | Write receipts repeat path metadata and direct agents toward unrelated file prefixes | Use compact receipts with useful changed ranges; test source-excerpt removal separately |

The measured issues are certain. Their effect on future time, cost, and correctness remains speculative until controlled benchmarks run.

## 1. Fix misleading search misses first

All five tilth runs submit `func (c *Context) Render` and receive `resolved_as: regex`, `status: no_match`.
The fixture contains that exact signature prefix at context.go:1151.
The benchmark binary reproduces the miss. The escaped regex finds one match.
It also misses `.Render(c.Writer)` while its escaped form finds one match.

Cause: src/mcp/tools/search_v2.rs:408-415 selects regex whenever a metacharacter appears.
Signature normalization runs afterward, at lines 418-432.
Its helper, lines 588-615, only handles a declaration keyword followed by one identifier.
A Go receiver signature therefore never reaches usable normalization.

The 97 query entries contain 29 no_match responses, including 24 regex misses.
Do not call all 29 wrong: some searches intentionally check that old code is gone.
The five repeated signature misses are directly verified false negatives.
Four explicit MCP errors therefore understate the search friction in this run.

Preferred implementation choices:
- Recognize a narrow declaration shape before generic regex classification.
- Preserve receiver, scope, and glob information; do not collapse every Render method into one symbol.
- Alternatively, retain the regex result and return a labeled literal alternative when a bounded check finds exact source text.
- Preserve valid regex behavior, diagnostics, ordered batch results, and completeness reporting.
- Do not silently broaden the caller's glob or pretend an incomplete scan proves no match.

The existing ADR rejects caller-selected kind/expand/context and requires the JSON envelope.
Do not add a new search mode as an incidental fix. Resolve fallback semantics explicitly before implementation.
Regression cases must include receiver signatures, call expressions, genuine regex, expected misses, ambiguity, and incomplete scans.

Evidence: .context/sonnet5-analytics/11_search_reproduction.jsonl.
The probe uses target/release/tilth --mcp --edit without modifying the fixture.

## 2. Shorten paths before removing source information

Tilth responses repeat the full temporary checkout path in headings, tags, and reread instructions.
The path prefix is about a full line of text in this benchmark environment.

| Response class | Total characters | Repeated cwd-prefix characters |
|---|---:|---:|
| Read | 426,069 | 25,112 |
| Write | 45,925 | 26,832 |

Replacing those in-checkout prefixes with cwd-relative paths removes 51,944 characters in an offline replay.
That equals 8.21% of all tilth MCP response text and 29.69% of the measured response-character gap.
These are character savings, not token or dollar savings. Shorter real checkout paths reduce this opportunity.

In write receipts, absolute cwd prefixes alone account for 58.43% of returned characters.
The smallest treatment keeps tags, numbered excerpts, status, and external absolute paths unchanged.
It only uses relative display paths where the path resolves under the supplied cwd.
Do not remove cwd from tool arguments or infer it from the server process.

Source: src/mcp/tools/write.rs:197-224 and 586-602; src/edit/tag.rs:67-69.
Read formatting is reached through src/mcp/tools/read.rs:187-219 and src/read/mod.rs.
Snapshot keys must remain canonical; only their presentation changes.
Acceptance includes symlinked cwd, external absolute paths, unusual path characters, tag reuse, and multi-file batches.

## 3. Let one edit express repeated intent

Tilth emits 423 operations: 300 replace_text operations and 123 line replacements.
WOZCODE emits 242 replacements, including 20 replace_all operations.
These are not identical work units, but the migration exposes a concrete expressiveness difference.

In three tilth runs, render_test.go alone needs 41 line replacements per run.
One WOZCODE receipt reports 30 occurrences changed by one replace_all operation.
Tilth write arguments total 116,927 serialized characters versus 98,481 for WOZCODE Edit.
The 18,446-character difference is not all attributable to replace_all; tags and schemas also differ.

Current unique-match enforcement is explicit in src/edit/apply.rs:146-169.
Production lowers text swaps through src/edit/apply.rs:383-449.
The tool schema requires one unique old match at src/mcp/tools/definitions.rs:250,270.
Do not change the default meaning of replace_text or make accidental ambiguous matches succeed.

Proposed contract:
- The caller explicitly requests all occurrences in one file or bounded range.
- The caller supplies an expected occurrence count.
- Zero matches, count mismatch, overlap, unseen targets, or conflicting drift reject that file section without partial writes.
- Keep exact matching for bulk operations initially; do not multiply the whitespace-normalization fallback silently.
- Report the applied count and resulting ranges.
- Preserve existing behavior for ordinary unique-match edits.

This is an interface addition, not a small renderer change.
Check all exported callers and snapshot/merge contracts before implementation.
Tests must cover Unicode boundaries, overlapping needles, multiple operations, stale snapshots, unread spans, and large inputs.
The current matcher documents a prior pathological O(n·m) case. Do not reintroduce it when enumerating matches.

## 4. Make write receipts useful, not merely short

Tilth's write response totals 45,925 characters versus WOZCODE's 16,192.
All 38 tilth write responses include omitted-line notices.
The renderer shows only two lines around the first changed line, even when later regions also change.
When the first edit adds an import, the excerpt describes the import rather than the later signature migration.
Its reread hint can point to lines 1–60 regardless of where the next edit is needed.

Source: src/mcp/tools/write.rs:493-505,523-524,586-602.
Prefer one concise per-file result with status, tag, and bounded changed-range information.
Preserve detailed errors and normalization warnings.
Avoid wording that makes an abbreviated receipt look like an incomplete edit.

An offline status/tag-only rendering reduces these existing receipts from 45,925 to 8,333 characters.
This is an illustration, not a validated formatter or a proven safe default.
A receipt with no source cannot authorize hidden lines under the current seen-line contract.
The regression at src/mcp/tools/write.rs:2610-2662 explicitly requires rereading omitted source before a fresh-tag line edit.
Do not bypass that gate to obtain a smaller benchmark number.
Measure follow-up rereads and failures, not only immediate response size.

## What the gap actually contains

Tilth emits 277 calls versus 293 for WOZCODE, but uses 250 distinct assistant messages versus 233.
A message can contain multiple calls; fewer calls do not guarantee fewer model round trips.
Tilth has 7.30% more messages and 14.24% more processed context per message.
This arithmetic describes the aggregate context gap; it is not causal attribution.

The 174,951-character MCP response gap divides as follows:
- Read plus search: 103,628 characters, 59.23%.
- Write receipts: 29,733 characters, 17.00%.
- Other tilth tools: 41,590 characters, 23.77%.

Thus compact write receipts alone cannot explain or guarantee closing the whole gap.
Tilth's search previews account for 85,842 serialized characters; continuation hints account for 11,341.
No follow hints are used in these benchmark traces, but other workloads can need them.
Do not remove the continuation contract based on one task.

## Not the first changes to make

- Schema shrinking: tilth starts at a mean 11,840 context tokens; WOZCODE starts at 13,600. Tilth already has the smaller initial context here.
- More file-read parallelism: src/mcp/tools/read.rs:149-162 already uses Rayon. No measured server-execution bottleneck justifies that work.
- Lower read budgets: no inspected read response shows budget truncation. Indiscriminate shrinking can restore the reread problem already fixed.

Line numbers account for 46,365 read-response characters, but they support safe line operations.
Their removal changes the edit contract. Start with redundant path text rather than removing useful coordinates.

## Experiment plan

1. Freeze the current binary, task, grader, model, effort, plugin, and strict guard.
2. Add deterministic tests and replay measurements for relative-path formatting and search normalization separately.
3. Benchmark each treatment independently before combining them.
4. Keep the same five-repetition matched schedule for continuity; retain failures and timeouts.
5. Track trusted correctness, time, reported cost, context categories, output tokens, model messages, tool bytes, and recovery calls.
6. Measure edit failures and post-write rereads when changing receipts.
7. Test combined winners on other multi-file edit tasks before selecting a default.

The target is lower end-to-end cost and time without lowering correctness or edit safety.
Five repetitions on one task can screen candidates. They cannot establish a general product advantage.
No new paid benchmark runs are started for this recommendation pass.

## Evidence

- Base comparison: .context/sonnet5-strict-results.md.
- Usage report: .context/sonnet5-strict-tool-analysis.md.
- Canonical database and queries: .context/sonnet5-analytics/.
- New measurements: 09_cost_inputs.*, 09_usage.json, 09_repeated_line_edits.json, 10_payloads.*, 10_payload_summary.json.
- Reproduction: 11_search_reproduction.jsonl.
- Skill domains: tilth-mcp-health.txt, tilth-error-forensics.txt, tilth-fix-recommendations.txt.
- Earlier matching finding: .hallouminate/wiki/model-tool-fumble-taxonomy.md:23-36.
- Locked search contract: .hallouminate/wiki/adr/tilth-search-v2-roadmap-002.md and -006.md.

## Below the Bar

Three alternatives.
