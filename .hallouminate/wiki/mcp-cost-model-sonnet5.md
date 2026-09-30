# MCP cost model: workload-specific benchmark evidence

From the valid three-way sonnet5 re-run (`benchmark_20260808_030838_sonnet5.jsonl`,
243 rows, 0 errors, tilth attached 81/81 pinned-arm cells). Full data in
`.cheese/notes/tilth-sonnet5-cost-attribution.md`.

## Historical Sonnet result, August 2026

On gin/fastapi tasks, `<certain>` tilth arms cost **18-21% more per correct
answer** than no-tilth, with **no accuracy gain**:

| arm | correct | CPC | vs no-tilth |
|---|---|---|---|
| no_tilth | 79/81 (97.5%) | $0.1160 | — |
| upstream (0.9.0) | 78/81 | $0.1405 | +21% (paired 95% CI [+19%,+68%]) |
| fork (PR #196) | 78/81 | $0.1364 | +18% (CI [+15%,+46%]) |

Fork vs upstream cost delta is -$0.32, CI `[-1.40, +0.83]` — statistically
indistinguishable. The task corpus is at a 97.5% no-tilth ceiling, so there
was no accuracy headroom for tilth to fill on this corpus.

## Where the cost actually goes: the MCP prefix, not per-call output

`<certain>` (attribution script `tool_cost_attr.py` over stream logs
`benchmark/results/streams/20260808_030838/`): tilth's **per-call outputs are
cheaper than native**, not more expensive — `tilth_read` mean 2.9KB vs native
`Read` 5.1KB, roughly equal total result volume across arms.

The cost driver is a **fixed ~5,350-token prefix** (tool schemas + server
instructions) that gets cache-written at the start of every cell:

- First-turn prompt size: tilth arms ~13,463-13,465 tokens vs no-tilth
  ~8,113 tokens.
- Each benchmark cell is a fresh session, so this prefix is a fresh
  cache-write (1h rate) every cell, then a cache-read on every subsequent
  turn within the cell.
- Gross prefix cost ≈ $3.60 per arm-run; tilth's more-compact outputs claw
  back ~$2 of that; net cost delta ≈ +$1.80 (upstream) / +$1.48 (fork).
- Category-level deltas confirm this: almost the entire cost difference is
  in `cache_write`/`cache_read`, not `output` tokens.

## Mixed adoption is the worst posture

`<certain>` Tool adoption differs sharply between arms:

- **Upstream**: full commitment — 500 tilth calls / 128 native calls, zero
  `Grep`/`Read`/`Edit`/`Glob` calls, zero tilth→native fallbacks.
- **Fork (PR #196)**: mixed — 331 tilth / 265 native calls, 76/81 cells used
  tilth at all, and **28 tilth→native fallback transitions** (search→Read
  ×10, search→Grep ×6, grok→Grep ×4, read→Grep ×4, search→Glob ×3, diff→Read
  ×1).

Mixed adoption pays the full prefix cost *and* re-fetches content via native
tools when tilth fails or the model loses confidence in it — worse than
either full commitment or not attaching tilth at all.

## Wasted trips

`<certain>` 24 empty/error `tilth_search` results (16 upstream, 8 fork) — see
[Model-tool fumble taxonomy](model-tool-fumble-taxonomy.md) for the query-grammar
root cause. Also: `tilth_read` returned the full 29.1KB `tree.go` (no
smart-view benefit) 3×/arm on `gin_radix_tree`, and 3 redundant identical
`tilth_read` calls were observed.

## Prompt-surface delta investigation (resolved)

`<certain>` Measuring the raw MCP surface (`initialize` instructions +
`tools/list` schemas, `--mcp --edit`):

| build | total chars | instructions chars |
|---|---|---|
| upstream 0.9.0 | 14,981 | 2,187 |
| PR #196 | 13,779 | 1,810 |
| local fork 0.8.4 (stale binary) | 24,917 | 8,688 |

The 24,917-char fork reading was the stale-binary artifact — see
[Benchmark harness gotchas](benchmark-harness-gotchas.md). Real HEAD-vs-196 gap
(current HEAD instructions are 2,003 chars, PR #151's cap held) is in tool
descriptions/schemas: HEAD 18,294 chars total vs #196's 13,779 (write
4,302 vs 2,867; read 3,407 vs 1,939; search 2,981 vs 1,861; diff 2,093 vs
1,485). Part of this gap is fork-inherent (the whole-file-tag ops grammar
needs more schema text than upstream's line:hash anchors); part is
trimmable — see PR #171 below.

Conclusion: there was no "shortening PR #196 missed" — upstream 0.9.0
contains an instructions/schema slimming the 0.8.4 fork never synced.
Benchmark arms are unaffected by this finding (they run upstream/196
binaries, not local HEAD).

## Follow-up: surface-shrink work

PR #171 (`fix/mcp-surface-shrink`) cut HEAD's surface from 18,294 → 13,773
chars — below PR #196's 13,779 — while restoring items an adversarial review
caught missing (MD032 fix, N:-prefix guard, kind grammar, `#n` gloss,
`next_view` semantics, anti-patterns section). See
[Multi-agent workflow notes](multi-agent-workflow-notes.md) for the review process.

## Related

- `.cheese/notes/tilth-sonnet5-cost-attribution.md`
- [Benchmark harness gotchas](benchmark-harness-gotchas.md)
- [Model-tool fumble taxonomy](model-tool-fumble-taxonomy.md)
- PR #168 (harness fix), PR #171 (surface shrink), upstream PR #196

## Strict Sonnet cost comparison, September 2026

The MCP cost model is workload-specific: [Tool Efficiency Report: tilth versus WOZCODE](sources/tilth-versus-wozcode-2026-09.md) does not reproduce the older prefix explanation.[^sept-cost]
Both MCP arms pass 5/5; native passes 4/5 with one 600-second timeout.
The strict run uses Sonnet 5 high, five shuffled repetition blocks, seed 20260929, and a $10 per-cell cap.

| Arm | Mean agent seconds | Mean reported cost | Mean processed context |
|---|---:|---:|---:|
| Native | At least 519.69, timeout floor included | $2.2195, four completed cells only | 7,266,408, four completed cells only |
| Tilth | 351.27 | $1.5137 | 3,959,322 |
| WOZCODE | 321.38 | $1.2529 | 3,230,145 |

Across four completed matched triplets, tilth reduces time 28.77%, context 44.62%, and cost 30.93% versus native.
WOZCODE reduces those measures 31.78%, 51.31%, and 39.74%.
Across all five matched MCP repetitions, WOZCODE uses 18.42% less context and costs 17.22% less than tilth.
Its mean time is 8.51% lower. Do not replace the native timeout or treat missing usage as zero.

Sonnet costs are CLI-reported inference costs, not a full account-billing audit.
Agent duration excludes trusted grading.
Processed context sums input, cache creation, and cache reads; it is not unique source content.
One task, shared caches, restrictive guards, and product-specific hooks limit generalization.

## Response overhead is not causal attribution

The September response analysis finds a smaller initial context for tilth: 11,840 tokens versus WOZCODE's 13,600.[^sept-overhead]
Tilth emits 277 calls and 250 assistant messages; WOZCODE emits 293 calls and 233 messages.
Fewer calls therefore do not establish fewer model round trips.
MCP responses contain 632,676 tilth characters versus 457,725 WOZCODE characters.
Read plus search explain 59.23% of this character gap; write receipts explain 17.00%.
Repeated checkout prefixes contribute 51,944 read/write characters.
Offline relative-path substitution removes 8.21% of tilth MCP characters, not a measured token or dollar amount.
Long disposable paths magnify that opportunity.

The report proposes independent search, relative-path, counted-replacement, and receipt experiments.
It does not implement them or establish their causal effect.
Do not transfer the August fixed-prefix attribution to this September task.
For Luna's different model and hybrid harness, see [batching observations](tool-batching-behavior.md).

[^sept-cost]: https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/sonnet5-strict-results.md
[^sept-overhead]: https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode.md; https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/evidence/09_usage.json; https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/evidence/10_payload_summary.json

_Source: PR #278 at 2b17c3755589a4e89eccaf89c3309ea9591aaa82 · Updated: 2026-09-29 · Supersedes: no historical measurements; narrows general claims to their measured configurations_
