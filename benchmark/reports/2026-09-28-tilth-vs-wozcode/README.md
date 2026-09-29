# Full benchmark report archive

This directory publishes the complete reports behind the [tilth versus WOZCODE recommendation](../2026-09-28-tilth-vs-wozcode.md).
The twelve supporting Markdown files preserve their original contents, including caveats and excluded attempts.
Their historical `.context/` citations identify original local paths. The tables below map the published reports and evidence.
Plans describe the state when written. Final results supersede interim progress statements.

## Current comparison

- [Full recommendation report](../2026-09-28-tilth-vs-wozcode.md)
- [Strict Sonnet results](sonnet5-strict-results.md)
- [Strict tool-usage analysis](sonnet5-strict-tool-analysis.md)
- [Strict benchmark contract](sonnet5-strict-plan.md)
- [Per-cell result summary](evidence/strict-cell-metrics.json)

## Complete report inventory

| Original local report | Published full report |
|---|---|
| `.context/luna56-curated-selection.md` | [luna56-curated-selection.md](luna56-curated-selection.md) |
| `.context/luna56-fixed-results.md` | [luna56-fixed-results.md](luna56-fixed-results.md) |
| `.context/luna56-render-context-invalid-attempt.md` | [luna56-render-context-invalid-attempt.md](luna56-render-context-invalid-attempt.md) |
| `.context/luna56-render-context-plan.md` | [luna56-render-context-plan.md](luna56-render-context-plan.md) |
| `.context/luna56-render-context-results.md` | [luna56-render-context-results.md](luna56-render-context-results.md) |
| `.context/read-budget-benchmark-results.md` | [read-budget-benchmark-results.md](read-budget-benchmark-results.md) |
| `.context/sonnet5-hybrid-stopped-results.md` | [sonnet5-hybrid-stopped-results.md](sonnet5-hybrid-stopped-results.md) |
| `.context/sonnet5-strict-auth-failure.md` | [sonnet5-strict-auth-failure.md](sonnet5-strict-auth-failure.md) |
| `.context/sonnet5-strict-plan.md` | [sonnet5-strict-plan.md](sonnet5-strict-plan.md) |
| `.context/sonnet5-strict-results.md` | [sonnet5-strict-results.md](sonnet5-strict-results.md) |
| `.context/sonnet5-strict-tool-analysis.md` | [sonnet5-strict-tool-analysis.md](sonnet5-strict-tool-analysis.md) |
| `.context/sonnet5-three-way-plan.md` | [sonnet5-three-way-plan.md](sonnet5-three-way-plan.md) |

## Analysis evidence

Original directory: `.context/sonnet5-analytics/`.
The following SQL, outputs, summary data, and reproduction records are preserved in full.
The wide text tables retain their original spacing and embedded source excerpts.

- [01_coverage.sql](evidence/01_coverage.sql)
- [01_coverage.txt](evidence/01_coverage.txt)
- [02_tools.sql](evidence/02_tools.sql)
- [02_tools.txt](evidence/02_tools.txt)
- [03_arguments.sql](evidence/03_arguments.sql)
- [03_arguments.txt](evidence/03_arguments.txt)
- [04_batches.sql](evidence/04_batches.sql)
- [04_batches.txt](evidence/04_batches.txt)
- [05_reads.sql](evidence/05_reads.sql)
- [05_reads.txt](evidence/05_reads.txt)
- [06_response_sizes.sql](evidence/06_response_sizes.sql)
- [06_response_sizes.txt](evidence/06_response_sizes.txt)
- [07_errors.sql](evidence/07_errors.sql)
- [07_errors.txt](evidence/07_errors.txt)
- [08_shapes.sql](evidence/08_shapes.sql)
- [09_cost_inputs.sql](evidence/09_cost_inputs.sql)
- [09_cost_inputs.txt](evidence/09_cost_inputs.txt)
- [09_repeated_line_edits.json](evidence/09_repeated_line_edits.json)
- [09_usage.json](evidence/09_usage.json)
- [10_payload_summary.json](evidence/10_payload_summary.json)
- [10_payloads.sql](evidence/10_payloads.sql)
- [11_search_reproduction.jsonl](evidence/11_search_reproduction.jsonl)
- [tilth-error-forensics.txt](evidence/tilth-error-forensics.txt)
- [tilth-fix-recommendations.txt](evidence/tilth-fix-recommendations.txt)
- [tilth-mcp-health.txt](evidence/tilth-mcp-health.txt)

[SHA-256 manifest](evidence/manifest.json) records the source and published hashes for each copied artifact.
Thirteen evidence files gain a final newline. All report copies are byte-identical to their originals.
The [cell summary](evidence/strict-cell-metrics.json) includes all 15 scored cells.
Missing timeout cost and token values remain null, not zero.

## Existing repository references

- [Search failure taxonomy](../../../.hallouminate/wiki/model-tool-fumble-taxonomy.md)
- [MCP cost analysis](../../../.hallouminate/wiki/mcp-cost-model-sonnet5.md)
- [Batching history](../../../.hallouminate/wiki/tool-batching-behavior.md)
- [Harness history](../../../.hallouminate/wiki/benchmark-harness-gotchas.md)
- [Deterministic search contract](../../../.hallouminate/wiki/adr/tilth-search-v2-roadmap-002.md)
- [Search continuation contract](../../../.hallouminate/wiki/adr/tilth-search-v2-roadmap-006.md)

## Local-only artifacts

This is a report archive, not a complete session-log or environment snapshot.
The DuckDB database, full session streams, raw payload extracts `08_shapes.json` and `10_payloads.json`, and runtime logs remain local.
The SQL and published report outputs document their analyses but do not recreate missing raw sessions.
Authentication files, plugin installations, virtual environments, and compiled binaries are not published.
Historical references to those artifacts are provenance records, not claims that GitHub hosts them.
No credentials or private-key material belong in this archive.

## Publication validation

Verify every copied artifact against its original before committing.
Check JSON syntax, Markdown link targets, report completeness, and credential-like strings.
This publication changes documentation and evidence only. It does not implement the proposed optimizations.
