# Tool-call batching: Sonnet 5 essentially never batches unprompted

Findings from the sonnet5 investigation on whether models batch multiple
tool calls per turn, and what shipped in response. Source:
`.cheese/notes/tilth-pr196-sonnet5-audit.md`.

## The finding

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

## Luna 5.6 edit diagnostic, 2026-09-28

An instructed Codex run demonstrates batching but no performance gain on two Gin edit tasks.
Three repetitions per arm produce twelve correct cells: baseline 6/6 and tilth 6/6.[^luna-edit]
All six tilth cells use successful batched MCP reads and writes; none reads host skills.
Eleven of fourteen reads and six of seven writes contain multiple items.
Mean agent time rises from 80.52 seconds to 101.75 seconds with tilth.
Processed context rises from 1,335,878 to 2,731,343 tokens.
This small, fixed-order diagnostic does not isolate the causal effect of batching alone.
Six diff calls fail because Git metadata is intentionally hidden; two search continuation calls also fail.
Do not generalize the older unprompted Sonnet finding to this explicitly instructed Luna run.

[^luna-edit]: benchmark/results/benchmark_20260928_131346_luna56.jsonl; benchmark/results/streams/20260928_131346/; .context/luna56-fixed-results.md

## Larger Luna migration, 2026-09-28

A context-aware renderer migration changes 20 reference files across 90 line-diff edit sites.
The corrected six-cell run uses Luna 5.6 at xhigh, with three repetitions per arm.
Both arms pass all three cells.[^luna-large]

Tilth uses 116 tool calls versus 150 for baseline, a 22.67% reduction.
Mean agent time rises from 431.49 to 448.65 seconds, a 3.98% increase.
Processed context rises from 5,977,630 to 8,940,423 tokens, a 49.56% increase.
All three matched tilth runs take longer than baseline.

Seventeen of 25 MCP reads contain multiple paths; the maximum is 20 paths.
Twenty of 23 write attempts apply a section, including eight multi-file calls; the largest applies 14 files.
Three write calls fail outright; two others partly apply and reject another section.
The tilth runs use no native edit fallback. Baseline also uses multi-file native patches.
Tool batching works, but this diagnostic does not show a speed benefit.

One earlier setup attempt is invalid because the grader omits a binding-package caller.
The corrected run adds that caller and uses a writable prewarmed temporary Go cache.
Its fixed arm order, shared cache, and one-task scope limit generalization.
Do not attribute the smaller slowdown versus the tiny tasks to edit size alone.[^luna-large]

[^luna-large]: benchmark/results/benchmark_20260928_154153_luna56.jsonl; benchmark/results/streams/20260928_154153/; .context/luna56-render-context-results.md; .context/luna56-render-context-invalid-attempt.md

### Trace diagnosis

Six of 25 tilth read calls contain truncation notices, followed by reads for missing sections.
The benchmarked reader divides its output budget equally and does not redistribute unused shares before truncation.
For example, 20 paths with a 30,000-token budget receive 1,500 tokens each.
Small files can leave budget unused while large files lose required content.
Batch file processing already uses Rayon parallel iteration; serial file reads are not the demonstrated bottleneck.[^luna-budget]

Baseline also overlaps native reads, with up to 19 command events in flight.
Fewer tool calls therefore do not establish fewer serial waits.
Tilth uncached input rises 37.24%, cached input rises 50.29%, and generated output rises 3.38%.
The processed-context increase is not a measurement of additional unique source content.
Raw streams lack per-call timestamps, so they cannot assign the 17.17-second mean gap to individual causes.
Redistributing unused read budget is a concrete fix candidate, not a proven explanation for the entire slowdown.[^luna-large]

[^luna-budget]: src/mcp/tools/read.rs:149-163,231-240 at 18b7534eecde023cb4a70e5d13d7de29178073de; src/budget.rs:34-45; benchmark/results/streams/20260928_154153/

### Read shapes and allocation follow-up

The 25 tilth reads contain 156 path requests: 78 numeric ranges, 63 plain paths, and 15 symbols.
Twenty-three calls contain a numeric range. Twenty-four calls use automatic mode; one uses full mode.
Plain paths can return automatic outlines rather than complete files.
These counts describe this Luna task, not agents generally.[^luna-large]

The local allocator change redistributes unused shares and returns complete batches when their framed response fits.
See [read budget accounting](read-budget-accounting.md#batch-allocation-2026-09-28).
A six-cell rerun uses the patched release binary, with SHA-256 beginning `67385086492d`.
Native baseline passes 3/3; patched tilth passes 2/3 and times out after 600 seconds on the third cell.
The timeout remains included, so this run does not show an overall speed benefit.[^patched-luna]

Read-budget truncation falls from 6/25 calls in the earlier run to 0/24 calls in the patched run.
The timeout trace contains four failed write calls and one partial write, including escaped-text mismatches and unread-line rejections.
Its candidate checks pass before timeout, but no completed turn or trusted grade exists.
Do not classify it as correct or treat its missing token usage as zero.[^patched-luna]

[^patched-luna]: .context/read-budget-benchmark-results.md; .context/read-budget-benchmark-inputs.sha256; benchmark/results/benchmark_20260928_173414_luna56.jsonl; benchmark/results/streams/20260928_173414/

## Strict Sonnet 5 migration, 2026-09-28

A five-repetition, three-arm run uses Sonnet 5 high on the same Gin renderer migration.[^strict-sonnet]
Both MCP arms restrict file work to their own tools. All arms share a Go-only Bash guard.
Tilth and WOZCODE pass 5/5. Native passes 4/5 and times out once after 600 seconds.
On the four completed matched repetitions, tilth reduces processed context 44.62% and reported cost 30.93% versus native.
WOZCODE reduces those measures 51.31% and 39.74%.
The timeout remains in correctness totals; its missing usage is not zero.

Tilth batches 26/48 reads, 31/38 searches, and 15/38 writes.
Its 104 write sections all report applied. WOZCODE uses 12 multi-file edit calls, covering 110 file targets across calls.
Separate multi-call assistant messages occur 23 times for tilth and 54 times for WOZCODE.
These results supersede any blanket claim that instructed Sonnet never batches.
They do not isolate the effect of earlier prompt or schema changes.

Tilth reads use 92 plain paths and 57 numeric ranges, with no symbol suffixes.
Tilth returns 632,676 MCP response characters; WOZCODE returns 457,725.
WOZCODE returns compact edit summaries and uses 20 replace_all operations.
Tilth write receipts include tags and excerpts; all 38 contain omitted-line notices.
No inspected read response contains truncation or next_view markers.
This distinguishes abbreviated write receipts from truncated reads.

Both arms have no successful native file-tool fallback. WOZCODE makes eight failed short-name Search attempts.
Native incurs eight Glob timeouts. All arms incur guard denials.
The strict guard, one-task scope, and product-specific hooks limit generalization.
Do not attribute the full performance gap to batching or response size alone.
The benchmark-only session-analytics database joins all 1,335 calls to results across 15 sessions.
Its permission_denials table misses this hook wording; full response inspection finds the denials.

[^strict-sonnet]: benchmark/results/benchmark_20260928_202801_sonnet5.jsonl; .context/sonnet5-strict-results.md; .context/sonnet5-strict-tool-analysis.md; .context/sonnet5-analytics/

## Related

- `.cheese/notes/tilth-pr196-sonnet5-audit.md`
- [MCP cost model: why tilth costs more per correct answer](mcp-cost-model-sonnet5.md)
- [Model-tool fumble taxonomy](model-tool-fumble-taxonomy.md)
- [Multi-agent workflow notes](multi-agent-workflow-notes.md)
