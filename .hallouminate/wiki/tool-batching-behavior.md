# Tool-call batching

Tool-call batching combines several read paths, search queries, or file edits in one tool request.
Historical Sonnet traces rarely batch; instructed Luna and strict Sonnet migration runs demonstrate effective multi-file batching.
Batching and separate multi-call messages do not establish parallel execution or faster completion.
The August evidence is `.cheese/notes/tilth-pr196-sonnet5-audit.md`; September measurements and limits appear below.

## Historical unprompted finding, August 2026

`<certain>` Across all three benchmark arms, Sonnet 5 batches **1-2%** of the
time it plausibly could, and emits **zero parallel tool-use turns**:

- `tilth_read` multi-item calls: 1-2% (3/123 fork, 3/202 upstream)
- `tilth_write` multi-file calls: 7-14%
- `tilth_search` always singular — upstream's schema has no query array at
  all; the fork's `queries: [...]` is batch-capable but never actually
  batched
- Native `Read`: 0%
- **0 of 1,781 tool-bearing turns** across all three arms emitted more than
  one tool call

`<certain>` Likely cause: the MCP tool descriptions' examples all show
single-item calls, which teaches singleton usage even where the schema
supports arrays. `<certain>` The benchmark corpus itself doesn't currently
exercise scenarios that reward batching, so this measures baseline model
behavior more than task pressure.

## Countermeasures shipped (pending validation)

Three PRs, built by codex workers in worktrees under
`/home/paul/Dev/tilth-wt/`, each adversarially reviewed by opus-tier agents
before merge (see [Multi-agent workflow notes](multi-agent-workflow-notes.md)):

- **PR #171** (`fix/mcp-surface-shrink`, base `main`): rewrote tool-call
  examples to be batch-first 2-item examples throughout, alongside the
  surface-shrink work (18,294 → 13,773 chars). Also added a control-char
  escape rule with a byte-lock test (see fumble class 3 in
  [Model-tool fumble taxonomy](model-tool-fumble-taxonomy.md)).
- **PR #170** (`feat/jit-batch-nudge`, base `main`): a session-state
  just-in-time nudge that prompts the model toward batching mid-session.
- **PR #169** (`feat/benchmark-batch-metric`, base
  `benchmark/sonnet5-failure-taxonomy`, stacked on #168): adds a
  `batch_sizes` field per result row and a per-arm Batching table to
  `analyze.py`'s reports, so batch rate is now a measured metric, not a
  one-off script output.

## Validation status

`<speculative>` Not yet validated — the plan is a HEAD-vs-upstream A/B run on
haiku (run #12 in the working notes) after all three PRs merge, which will
measure both #171's example-compression effect and #170's nudge effect,
using #169's new batch-rate metric. This has not run as of 2026-08-08.

## Related

- `.cheese/notes/tilth-pr196-sonnet5-audit.md`
- [MCP cost model: why tilth costs more per correct answer](mcp-cost-model-sonnet5.md)
- [Model-tool fumble taxonomy](model-tool-fumble-taxonomy.md)
- [Multi-agent workflow notes](multi-agent-workflow-notes.md)

## Instructed Luna batching, September 2026

The Luna results in [Tool Efficiency Report: tilth versus WOZCODE](sources/tilth-versus-wozcode-2026-09.md) show effective batching without an overall speed benefit.[^sept-luna]
These Codex runs use Luna 5.6 xhigh, fixed baseline-first order, and three repetitions per arm.

| Task set | Correct, native / tilth | Mean seconds, native / tilth | Processed context, native / tilth |
|---|---|---|---|
| Two small Gin repairs | 6/6 / 6/6 | 80.52 / 101.75 | 1,335,878 / 2,731,343 |
| Larger renderer migration | 3/3 / 3/3 | 431.49 / 448.65 | 5,977,630 / 8,940,423 |

Small-task estimates total $0.12577 native versus $0.24644 tilth; larger-task estimates total $0.46619 versus $0.63264.
These are harness estimates, not verified billing.
The small-task tilth arm batches 11/14 successful reads and 6/7 writes.
Six diff calls fail against intentionally hidden Git metadata; two continuation calls also fail.
These failures remain measured.

The larger tilth arm uses 116 calls versus 150 native calls, but all three matched runs take longer.
It batches 17/25 reads, reaching 20 paths.
Twenty of 23 write attempts apply at least one section; eight successful calls span multiple files, reaching 14 files.
Three writes fail outright and two partly apply. Completion of an MCP event does not prove every section applied.
No tilth trace uses native edit fallback; native also batches patches.
The larger reads request 156 paths: 78 ranges, 63 plain paths, and 15 symbols.
Twenty-four reads use automatic mode and one uses full mode.
Six reads truncate; see [read-budget follow-up](read-budget-accounting.md#patched-luna-read-budget-benchmark-september-2026).
Raw events lack per-call timestamps. Fixed order, shared caches, and different task setups prevent causal batching or size claims.

## Strict Sonnet batching, September 2026

The strict Sonnet migration demonstrates instructed batching and multi-call messages, unlike the historical unprompted traces.[^sept-batch]
These measurements do not isolate the effect of the earlier prompt or schema changes.

| Operation | Calls | Multi-item calls | Items | Largest batch |
|---|---:|---:|---:|---:|
| Tilth read | 48 | 26 | 149 paths | 16 |
| Tilth search | 38 | 31 | 97 queries | 6 |
| Tilth write | 38 | 15 | 104 file sections | 13 |
| WOZCODE Search | 113 | 30 | 198 patterns | 13 |
| WOZCODE Edit | 45 | 34 | 242 replacements | 30 |

All 104 tilth file sections apply. WOZCODE makes 12 multi-file edits covering 110 file targets across calls.
Tilth emits 300 text swaps and 123 line replacements; WOZCODE emits 222 single replacements and 20 `replace_all` operations.
These operation units are not equivalent.
Tilth emits 23 multi-call messages; WOZCODE emits 54. Neither count proves concurrent server execution.
Tilth reads request 92 plain paths and 57 ranges, with no symbol suffixes or custom budgets.
Forty-three reads omit mode and five request full mode.
All 38 tilth write receipts abbreviate source, but no inspected read shows truncation markers.
Six tilth paths and 27 WOZCODE patterns repeat within sessions; these are not automatic waste estimates.

The analytics database joins all 1,335 calls across 15 sessions.
Its 500-character result summaries cannot measure complete responses; the analysis uses full raw entries.
Guard denials total 38 tilth, 42 WOZCODE, and 76 native calls.
The derived permission-denials table misses this hook wording; zero there does not mean no denials.
Native also incurs eight Glob timeouts; WOZCODE makes eight unavailable short-name Search attempts.
Neither MCP arm successfully falls back to native file tools.
Keep these recovery costs in the results; do not infer an exact server latency from trace emission times.

[^sept-luna]: https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/luna56-fixed-results.md; https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/luna56-render-context-results.md
[^sept-batch]: https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/sonnet5-strict-tool-analysis.md

_Source: PR #278 at 2b17c3755589a4e89eccaf89c3309ea9591aaa82 · Updated: 2026-09-29 · Supersedes: no historical measurements; narrows general claims to their measured configurations_
