# Tool Efficiency Report: tilth versus WOZCODE

Paul Sorensen's repository benchmark report, dated 2026-09-28 and ingested 2026-09-29, identifies four candidates for controlled efficiency tests.
Canonical source: [Tool Efficiency Report: tilth versus WOZCODE](https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode.md).
The source commit is `2b17c3755589a4e89eccaf89c3309ea9591aaa82`.
PR #278 now holds curated wiki knowledge. The benchmark implementation has a separate review unit.
Original reports and evidence remain available at this immutable commit, not the changing PR branch.

## Findings and interpretation

The strict Sonnet comparison gives tilth and WOZCODE five correct cells each.
Native passes four cells and times out once after 600 seconds.
WOZCODE uses 18.42% less processed context and costs 17.22% less than tilth across five matched MCP repetitions.
These observations describe one synthetic migration, not a general product ranking.[^results]

The report distinguishes response characters, processed context, output tokens, model messages, and reported inference cost.
Processed context includes repeated input and cache traffic; it does not count unique source tokens.
Missing timeout usage remains unknown, not zero.
Luna estimates are harness estimates, whereas Sonnet costs come from CLI reports.

## Four proposed treatments

The efficiency report prioritizes these independent experiments, not an approved implementation contract.[^report]

1. Recognize narrow declaration-shaped searches or return a labeled, bounded literal alternative.
   Preserve receiver and glob scope, genuine regex behavior, completeness, diagnostics, and the existing continuation contract.
2. Render in-checkout paths relative to explicit `cwd`.
   Offline substitution removes 51,944 characters, or 8.21% of tilth MCP text; this is not a measured token saving.
   Preserve canonical snapshot keys, external absolute paths, tags, and seen-line checks.
3. Add explicitly requested, count-checked repeated exact replacement.
   Preserve ordinary unique-match semantics.
   Reject zero matches, count mismatch, overlaps, unseen targets, and conflicting drift without partial writes to that section.
   Test Unicode, multiple operations, stale snapshots, large inputs, and matcher complexity.
4. Make receipts compact while retaining status, tags, useful changed ranges, detailed errors, and normalization warnings.
   A status/tag-only replay shrinks 45,925 characters to 8,333, but does not prove a safe default.
   Hidden receipt lines do not authorize later edits. Measure subsequent reads and recovery failures.

Schema shrinking is not the first treatment: tilth starts with less context than WOZCODE in this run.
File reads already use Rayon; the reports establish no server-execution bottleneck.
Do not lower read budgets or remove line coordinates merely to reduce response characters.
Freeze binary, task, grader, model, effort, plugin, and guard for each treatment.
Retain failures and timeouts. Test successful treatments on additional tasks before changing defaults.

## Curated topic routes

The efficiency report's evidence extends existing wiki topics rather than replacing their historical results:

- [Harness validity and excluded attempts](../benchmark-harness-gotchas.md)
- [Cost interpretation and response overhead](../mcp-cost-model-sonnet5.md)
- [Batching observations](../tool-batching-behavior.md)
- [Verified signature-search misses](../model-tool-fumble-taxonomy.md)
- [Read-budget symptom versus outcome](../read-budget-accounting.md)

## Immutable report inventory

The archived plans describe earlier states. Final corrected results supersede their progress statements.[^archive]

- [Selection: Luna edit-only batching](https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/luna56-curated-selection.md)
- [Corrected Luna small-task results](https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/luna56-fixed-results.md)
- [Luna larger-task plan](https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/luna56-render-context-plan.md)
- [Invalid larger-task attempt](https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/luna56-render-context-invalid-attempt.md)
- [Corrected larger-task results](https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/luna56-render-context-results.md)
- [Patched read-budget results](https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/read-budget-benchmark-results.md)
- [Sonnet three-way plan](https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/sonnet5-three-way-plan.md)
- [Stopped asymmetric Sonnet run](https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/sonnet5-hybrid-stopped-results.md)
- [Strict Sonnet plan](https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/sonnet5-strict-plan.md)
- [Authentication-invalid strict run](https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/sonnet5-strict-auth-failure.md)
- [Final strict results](https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/sonnet5-strict-results.md)
- [Strict tool-usage analysis](https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/sonnet5-strict-tool-analysis.md)

## Evidence coverage and limits

The immutable archive includes the recommendation, twelve supporting reports, SQL, query outputs, summary JSON, a hash manifest, and search reproduction.[^archive]
[All 15 scored cells](https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/evidence/strict-cell-metrics.json) preserve null timeout metrics.
The [SHA-256 manifest](https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/evidence/manifest.json) records copied artifact identities.
Thirteen evidence files gain a final newline; report copies preserve their original bytes.
The archive excludes the session database, full raw streams, raw payload extracts, runtime logs, credentials, plugins, environments, and binaries.
SQL and published outputs do not reconstruct those missing raw sessions.
Historical local paths identify provenance only. They do not promise downloadable files.
This ingest starts no paid inference and implements none of the proposed optimizations.

[^report]: https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode.md
[^results]: https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/sonnet5-strict-results.md
[^archive]: https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/README.md

_Source: PR #278 at 2b17c3755589a4e89eccaf89c3309ea9591aaa82 · Updated: 2026-09-29 · Supersedes: mutable branch links for this report set_
