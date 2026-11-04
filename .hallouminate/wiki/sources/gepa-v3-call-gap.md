# Seed API-call gap: tilth versus built-in tools

Author: Codex gpt-6-astra diagnosis. Date: 2026-10-27.
Contribution: a turn-by-turn count of API calls and tool calls for the gepa-v3 seed (main `202027d`) against the built-in-tools baseline, with ranked fixes. [gepa-v3 evolve campaign state](../benchmark-gepa-v3-evolve.md) cites it.
Canonical location: this page. The original was `.cheese/research/gepa-v3-call-gap.md` in the run worktree, which the repository does not track. The trajectory paths in the evidence manifest are relative to that run checkout and are not committed.

## Finding

The gap comes from extra dependent turns, not more tool calls.
Across these 24 runs, baseline uses 356 tool calls and 196 API calls.
Tilth uses 334 tool calls and 308 API calls.
The largest avoidable paths are failed Go signature searches, incomplete search results, hook retries, and false unchanged-read replies.

Scope: main `202027dd974f6f2915340673f1d32410bb0ab762`.
I inspect three repetitions per arm for four task cells, including all five FastAPI chain turns.
I select seed rows by revision, not only timestamp.
I exclude later candidate rows and repeated baseline rows after L84.
No model, benchmark, evolve, test, or build command runs during this research.

## Ranked fixes

Savings below are planning estimates per cell across the **12 reviewed tilth cells**.
Each chain counts as one cell.
They are not measured treatment effects or estimates for the uninspected winning tasks.
The ranges overlap. Do not add them directly.
API turns containing other useful work do not count as fully removable.

### 1. Resolve signatures and return a useful enclosing unit

**Expected saving: about 1.0–1.7 API calls per reviewed cell.**
Most headroom is Caddy: about 3–6 calls per repetition.
Tokio has smaller scope-recovery opportunities.
Confidence: certain about the routing and output limits; speculating about saved calls.

Change:
- `src/mcp/tools/search_v2.rs#833-860`, `route_query`: recognize unambiguous Go declaration signatures before generic regex routing.
- `src/mcp/tools/search_v2.rs#1032-1060`, `strip_signature_prefix`: support receiver declarations, not only two whitespace tokens.
- `prompts/tools/search.md#1`: show a method-name query with a file glob. Explain regex metacharacters.
- `src/mcp/tools/search_v2.rs#1166-1180`, `unique_hit`: replace the fixed 60-line cut with a bounded complete construct when it fits.
- `src/search/mod.rs#687-704`, `format_raw_result_deferred`: consider bounded enclosing context for a sparse content hit, instead of expansion zero.

Preserve intentional regex searches.
Do not convert arbitrary regex failures to a different search without a diagnostic.
For a truncated construct, return its end line and an exact continuation read.
Do not return whole files by default.

Evidence:
- CT0 L1–3: `func (MatchExpression) UnmarshalCaddyfile` produces no match; a shortened query gives one line; a third call reads the method.
- CT0 L6, L9, L13: receiver-shaped queries for dispenser methods and `Quoted` also miss.
- CT1 L1–3 and L9–11 repeat this search-to-retry-to-read pattern.
- CT2 L7 misses; L8 succeeds with bare symbol names.
- CT0 L4 shows only the expression hit inside `parseMatcherDefinitions`. L5 and L10 read separate parts of that function.
- CT1 L5–7 and CT2 L4–6 repeat the fragmented function reads.
- CB1 L1 supplies the full target method through Grep context in one API turn.
- TT0 L5–7 and TT2 L6–8 recover the tail of a 60-line symbol preview.

### 2. Make broad search complete enough and make test hits editable

**Expected saving: about 0.8–1.2 API calls per reviewed cell.**
FastAPI offers about 3–5 calls per chain.
Confidence: certain about both implementation defects; speculating about agent response.

Change:
- `src/search/content.rs#13-33,119-122`: make the hit cap budget-aware, with a bounded continuation mechanism.
- `src/search/mod.rs#636-652`: stop forcing the small search path regardless of the requested output budget.
- `src/search/mod.rs#1755-1771`, test facet in `format_search_result_seen`: emit file tags and record the exact displayed test lines.
- Keep `prompts/tools/search.md#1`'s edit-ready search promise only when every source-bearing output branch satisfies it.

The current test facet bypasses `format_matches`.
It prints trimmed one-line tests without tags or seen-line entries.
At five or fewer hits, the ordinary formatter can provide tags.
Thus splitting a query changes both coverage and editability.
Preserve original source whitespace and record only displayed lines.
Do not weaken the tagged-edit guard.

The content backend returns at most ten matches in this path.
A budget of 30,000 does not remove that cap.
Prefer enough source-bearing matches within the host-safe budget.
For large results, provide stable continuation and distinct-file coverage rather than only “Narrow with scope.”

Evidence:
- FT2 turn5 L2: budget 20,000 returns ten hits from 45 found.
- FT2 turn5 L18: budget 30,000 returns ten of fifteen test hits, without tags.
- FT2 turn5 L20: another partitioned search is needed; compact OAuth test hits still lack tags.
- FT2 turn5 L21–23: a 15-file edit fails, then a read batch obtains tags, then the edit retries.
- FT0 turn5 L15–20 and FT1 turn5 L20–25 show the same narrowing/tag-recovery pattern.
- FT1 turn5 L24 then spills the recovery read to a 60.8 KB persisted result.
- FB0 turn5 L41 gives 80 Grep content hits; L42–56 edits the affected tests in one API turn.
- CT0 L4 also truncates to ten of thirteen hits, despite a small rendered response.

### 3. Explain the strict Bash boundary before the first blocked call

**Expected saving: about 0.6–1.0 API calls per reviewed cell.**
The observed excess is 18 denials, or 1.5 per cell, before discounting overlaps and behavior uncertainty.
Confidence: certain about the denials; speculating about prompt effectiveness.

Change:
- `prompts/mcp.md#2`: include `wc`, `awk`, `git grep`, redirects, heredocs, and inline interpreters in a short operation map.
- `src/hook/hints.rs#155-199`: give the same map in denial hints.
- For a verification script, explain the permitted route: create it through the write tool, then execute the file through Bash.
- Keep the hook rules unchanged.

The phrase “Git review/history” does not authorize `git grep`.
The phrase “tests, builds, and non-file operations” does not explain the inline-code restriction.
The interpreter hint currently says “Do not script it,” although trajectories successfully create and run test scripts through the tools.
Use a cell-local unique scratch path, not a shared fixed `/tmp` name.

Evidence:
- Tilth has 38 hook denials; baseline has 20.
- There are 32 tilth denial-only API turns and 15 baseline denial-only turns.
- CT1 L4, L8, L16; CT2 L3, L9, L14, L18, L21: search/read/write fallbacks hit the hook.
- MT2 L15–18: heredoc block, inline-Python block, tool-created script, then successful execution.
- FT0 turn5 L1–4: grep, wc, then git-grep denial escalation.
- FT2 turn5 L1, L3, L17, L28, L33: five separate denial turns.
- TT0 L2: grep fallback costs another turn.

Do not attribute every denial to tilth.
Both arms use strict hooks, with different replacement-tool hints.
Some blocked searches follow incomplete tilth output, so this fix overlaps fix 2.

### 4. Record only visible read ranges, and bound serialized output

**Expected saving: about 0.6–0.9 API calls per reviewed cell.**
There are eight unchanged-stub/retry pairs across Metaflow, Tokio, and FastAPI.
Some pairs share a turn with useful work.
Confidence: certain about the source defect and observed retries; speculating about total savings.

Change:
- `src/mcp/tools/read.rs#413-436`: record successful displayed ranges after rendering, not the requested span before rendering.
- `src/mcp/tools/read.rs#638-668`: errors must return no seen-line provenance, not `SeenSpec::Whole`.
- `src/mcp/tools/read.rs#217-239,275-285,552-595`: replace a per-file clipping boolean with accurate visible-range provenance.
- `src/session.rs#157-166`: do not clear the history of hidden ranges after one small successful read.
- `src/mcp/path_suffix.rs#72-79`: distinguish a code declaration prefix from a Markdown heading, or return an exact corrected symbol call.
- `prompts/tools/read.md#3`: keep the explicit resend escape until the implementation is corrected.

A missing heading currently returns `SeenSpec::Whole`.
The caller then records that whole-file span, even though the response contains only an error.
For a large full read, the code records the whole file before checking output size.
One later small read clears `read_clipped`, but the whole-file seen span remains.
An unseen range can then return an unchanged stub.

Cap the final serialized MCP response below the host visibility limit.
Account for escaping and multi-file wrappers, not only source bytes or estimated tokens.
For a batch, mark only source lines actually emitted.
Do not recommend “never use full”: the current implementation requires it to escape these false stubs.

Evidence:
- Every MT repetition L2 returns a 51.6 KB persisted-output preview.
- MT0 L6–7 and L17–18, MT1 L5–6 and L10–11, MT2 L6–7: five stub/resend pairs.
- MT0 L15 returns a heading error immediately before one of those pairs.
- TT2 L3: `#fn recv_ref` becomes a heading error. L4–5 and L7–8 then return stub/resend pairs.
- FT1 turn5 L4 spills a 57.4 KB batch. L6 reads a small part of api_key.py.
- FT1 turn5 L11–12 then incorrectly stubs and resends api_key.py's unseen tail.

### 5. Put the existing-file write contract before the operation list

**Expected saving: about 0.3–0.6 API calls per reviewed cell.**
This is lower-confidence prompt work.
Confidence: certain about failed operations; speculating about prevention.

Change:
- `prompts/tools/write.md#1`: lead with “create_file is only for absent paths.”
- Show whole-file replacement of an existing file as a tagged `replace`, not `create_file`.
- State that an edit should cover the complete affected local construct, including obsolete locals and comments.
- `prompts/mcp.md#6-14`: retain batching and receipt reuse, but show one concrete multi-file ready-edit batch.
- Require the containing delimiter context before an insertion, not only before an AST rewrite.

Evidence:
- FT0 turn5 L10–11 and FT2 turn5 L7–8 try to create an already-read base.py, then replace it.
- MT1 L18–20 omits a known tag and retries.
- TT0 L8–12 removes a guard but leaves its local and comment, then fails an exact-text cleanup and rereads.
- TT2 L9–11 also needs a cleanup read and edit.
- CT1 L22 inserts a method inside a grouped Go type declaration; L24–26 compile, read, and repair it.

Do not add automatic overwrite or infer missing tags.
The write receipt already permits reuse of its new tag.
The seed prompt does not require a read after every write.
A blanket removal of read-after-write instructions is therefore not a seed fix.
Batching independent ready files has additional potential, but both arms also serialize some file edits.
Do not count fewer tool invocations as API savings without checking `api_turn`.

## Task comparison and concrete turn losses

API counts use `api_calls` where present and `per_turn_context_tokens` length otherwise.
The trajectory `api_turn` is zero-based and can contain several tools.
L numbers below are one-based physical JSONL lines.

| Task | Baseline API calls, reps 0/1/2 | Tilth API calls, reps 0/1/2 | Mean excess | Correct baseline / tilth |
|---|---|---|---|---|
| Caddy | 6 / 7 / 11 | 27 / 26 / 29 | 19.33 | 2/3 / 3/3 |
| Tokio | 5 / 7 / 7 | 13 / 6 / 11 | 3.67 | 3/3 / 3/3 |
| Metaflow | 18 / 13 / 11 | 24 / 20 / 18 | 6.67 | 3/3 / 3/3 |
| FastAPI chain | 38 / 39 / 34 | 43 / 42 / 49 | 7.67 | 2/3 / 2/3 |

### Caddy first

Baseline starts with a Grep that includes the full method.
The successful baseline repetitions inspect the expression construction and token newline logic, then copy the original token.
The failed baseline repetition applies a cheaper line-only fix.
Its six API calls must not become the target for a correctness-preserving optimization.

CT0:
- L1–17, API turns 0–14: failed signatures, one-line hits, split function reads, and helper discovery.
- L18–19: blocked fixture redirect, then another search.
- L20–23: tool creation, an incorrect adapter API in the generated test, then repair and execution.
- L24–26: blocked sed, then fixture update and another test.
- L27–28: the import-snippet fixture finally reproduces the failure.
- L29–32: correct token-copy fix and tests.

CT1:
- L1–21, API turns 0–20: the same lookup loop, three hook denials, and sliced token-definition reads.
- L22–27: an invalid method insertion and private-field access require repairs before tests pass.

CT2:
- L2 demonstrates that a bare symbol query can return the complete target method immediately.
- L4–17: split expression reads, failed receiver queries, an over-narrow helper search, and denied shell fallbacks.
- L18–23: fixture creation and modification each incur a hook denial.
- L24–27: a fixed /tmp path already exists; a new path is needed.
- L30: raw control characters make one write call invalid.
- L31–32: the import fixture finally exposes the bug.
- L33–34: the agent applies the token-copy fix and verifies it.

Conclusion: search and read granularity dominate the avoidable early turns.
Later reproduction work is partly valuable, not pure tool overhead.

### Tokio

TB0 L1–2 batches two context-rich Greps in API turn 0.
One later read and one edit/test turn complete the task.
TT1 needs six API calls, close to baseline, with no tool errors.
This is a useful successful tilth control.

TT0 loses turns to a grep denial, a partial recv_ref preview, split range reads, and a failed cleanup edit.
Its final cargo output also spills into a persisted result, followed by a narrower command.
TT2 loses turns to a heading parse error, two false unchanged replies, and a second cleanup edit.
The eight-API-call spread between TT0 and TT1 is not a fixed MCP overhead.

### Metaflow

All baseline repetitions obtain the 1,568-line source through one Read.
All tilth repetitions begin with a full read that the host hides behind a persisted-output preview.
They then recover source through slices and false-stub retries.
This is not merely “the agent reads too much.”
The requested source is not reliably delivered.

All tilth repetitions also pay four or five Bash denials.
Baseline pays three or four.
Further tool friction includes MT1's missing tag and existing /tmp script.
Do not attribute package installation attempts and unrelated local test failures entirely to the MCP implementation.

### FastAPI chain

`benchmark/chains.py#63-68` defines four read tasks followed by one security migration.
The mean excess is concentrated in the final turn.

| Chain member | Baseline API calls, reps 0/1/2 | Tilth API calls, reps 0/1/2 |
|---|---|---|
| dependency resolution | 4 / 3 / 3 | 3 / 3 / 3 |
| request validation | 4 / 3 / 2 | 3 / 4 / 2 |
| Depends internals | 3 / 3 / 3 | 3 / 3 / 3 |
| grok Depends | 2 / 3 / 3 | 4 / 4 / 4 |
| security edit | 25 / 27 / 23 | 30 / 28 / 37 |

Baseline uses 71–92 tools per chain; tilth uses 42–49.
Baseline batches many Edit calls in one model response.
Tilth's fewer tools do not produce fewer dependent turns.

All three tilth edit turns hit search coverage and test-tag recovery problems.
FT0 and FT2 also misuse create_file.
FT1 has two oversized read results.
FT2 L12–14 repairs its generated OAuth edit.
FT2 L25–30 searches for a working Python/test environment.
L33–35 handles a blocked heredoc and an existing shared /tmp script.
L39–40 discovers and installs the missing argon2 test dependency.

## Harness and grading artifacts

1. **FastAPI chain failures are phrase-check failures, not failed edits.**
   Rows L30 and L64 fail only the request-validation subtask.
   Both arms' security edits pass trusted and candidate tests in all three repetitions.
   The check at `benchmark/tasks/fastapi_tasks.py#59` accepts four literal phrase alternatives.
   FB2 turn2's final answer says “returns the async closure app(request).”
   FT0 turn2 says “returns the inner coroutine app(request) -> Response.”
   Both describe the missing concept but fail the literal phrase check.
   The full-stream result records are FB2 turn2.jsonl L6 and FT0 turn2.jsonl L8.
   Treat this as strong evidence of a grading false negative.
   Correcting it changes the denominator for both arms; it does not remove the API-call gap.

2. **Caddy correctness differs.**
   Baseline passes two repetitions; tilth passes three.
   Cost per correct is total cost divided by successful cells, not mean successful-run cost.
   More careful import-chain diagnosis partly explains the tilth increase.

3. **Shared scratch state contaminates execution.**
   CT2 L24 fails to create /tmp/t/Caddyfile.
   L25 nevertheless runs that old file: its :80/name-a output differs from the requested :8080 fixture.
   MT1 L21 and FT2 turn5 L34 also find existing fixed scratch paths.
   Isolate scratch paths per cell without deleting another run's files.

4. **The candidate test environment is not consistently ready.**
   FT2 turn5 L25 reports no python; L27 reports no pytest.
   Later calls discover dependency and version gaps.
   FT0 turn5 L26–32 likewise iterates through dependencies, then reaches 381 passing tests.
   These are harness/environment costs, not search latency.
   Some generated tests also contain genuine agent mistakes; for example, importing HTTPBase from the wrong module.

5. **Contamination flags need event-level interpretation.**
   MB0 and MT1 contain package-source flags.
   Their flagged pip-download commands share a Bash command with a blocked content read.
   The denial is not evidence that downloaded source entered context.
   Do not silently drop them as confirmed contamination.

6. **Use API usage records, not num_turns.**
   MB1 reports num_turns=2 but has thirteen API usage entries.
   Chain summaries count strict denials inconsistently with earlier chain members.
   FT1 has four trajectory denials, including turn2 L3, while its aggregate field reports three.
   Count the observed hook prefix and retain the chain member identifier.

7. **Host output persistence is an interaction defect.**
   It is not solely a larger-token problem.
   The host hides source, then tilth's provenance can claim the source is already visible.

## Scope and handoff

The causal source findings are certain within the inspected revision and trajectories.
Expected savings remain speculative until separately measured.
No tests or benchmark run validates the proposed changes.
Do not change grading, hooks, source, or a running evolve process during this research.

Recommended next step: implement source-level regression tests for fixes 1, 2, and 4 in a separate authorized task.
Check real host serialization for the clipping fix.
Keep the exact seed, task digest, and strict hook fixed for a later paid A/B evaluation.
Do not use equal call counts alone as proof of equal correctness.

Research routing: local trajectories and code through Tilth; repository wiki for the strict-hook contract.
No public-source lookup is needed for this pinned local diagnosis.
agent_resolution: inline research sub-agent; no child agents or paid model calls.
The only requested durable artifact is this report. No separate wiki or checkpoint file is written.

## Evidence manifest

All trajectory links are relative to the run checkout.
B means baseline; T means tilth_forced; the final digit is the repetition.
C, T, M, and F mean Caddy, Tokio, Metaflow, and FastAPI.
FastAPI entries name turn1; replace .turn1. with the cited .turnN.
For a full stream, remove .trajectory from the filename.

- **MB0** (rows.jsonl L1): `benchmark/results/streams/plan/a79b8d308e5cd997_Netflix__metaflow.b390a8d4.test_stub_generator.7bf08c98.lv1_baseline_rep0_20261026T052152110676.trajectory.jsonl`
- **MB1** (rows.jsonl L2): `benchmark/results/streams/plan/1ed7c2dc3ba815b2_Netflix__metaflow.b390a8d4.test_stub_generator.7bf08c98.lv1_baseline_rep1_20261026T052542448620.trajectory.jsonl`
- **MB2** (rows.jsonl L3): `benchmark/results/streams/plan/ff6ffeb69f0ad2f4_Netflix__metaflow.b390a8d4.test_stub_generator.7bf08c98.lv1_baseline_rep2_20261026T053520171900.trajectory.jsonl`
- **CB0** (rows.jsonl L4): `benchmark/results/streams/plan/bf7d5fd36be5e0cd_caddyserver__caddy-6288_baseline_rep0_20261026T053656340548.trajectory.jsonl`
- **CB1** (rows.jsonl L5): `benchmark/results/streams/plan/7f08c95167950872_caddyserver__caddy-6288_baseline_rep1_20261026T053746358565.trajectory.jsonl`
- **CB2** (rows.jsonl L6): `benchmark/results/streams/plan/d229c48a7f1e31b8_caddyserver__caddy-6288_baseline_rep2_20261026T053840893183.trajectory.jsonl`
- **TB0** (rows.jsonl L16): `benchmark/results/streams/plan/5aba0a06582c454f_tokio-rs__tokio-4867_baseline_rep0_20261026T000343173562.trajectory.jsonl`
- **TB1** (rows.jsonl L17): `benchmark/results/streams/plan/22f7679f84e753ba_tokio-rs__tokio-4867_baseline_rep1_20261026T002419642355.trajectory.jsonl`
- **TB2** (rows.jsonl L18): `benchmark/results/streams/plan/a9eef1f0b1ae00f1_tokio-rs__tokio-4867_baseline_rep2_20261026T072300568194.trajectory.jsonl`
- **FB0** (rows.jsonl L28): `benchmark/results/streams/plan/65b476050bacf5c9_chain_fastapi-edit-5_baseline_rep0_20261026T160242504619.turn1.trajectory.jsonl`
- **FB1** (rows.jsonl L29): `benchmark/results/streams/plan/f2a8d955af22eed6_chain_fastapi-edit-5_baseline_rep1_20261026T160642430507.turn1.trajectory.jsonl`
- **FB2** (rows.jsonl L30): `benchmark/results/streams/plan/61f7332527ebf8e9_chain_fastapi-edit-5_baseline_rep2_20261026T161136122525.turn1.trajectory.jsonl`
- **MT0** (rows.jsonl L55): `benchmark/results/streams/plan/1480d977a99756c1_Netflix__metaflow.b390a8d4.test_stub_generator.7bf08c98.lv1_tilth_forced_rep0_20261026T165056361805.trajectory.jsonl`
- **CT0** (rows.jsonl L56): `benchmark/results/streams/plan/863cf8fe1fe0857f_caddyserver__caddy-6288_tilth_forced_rep0_20261026T165639590972.trajectory.jsonl`
- **TT0** (rows.jsonl L60): `benchmark/results/streams/plan/ec166f72c6a7df05_tokio-rs__tokio-4867_tilth_forced_rep0_20261026T170645763960.trajectory.jsonl`
- **FT0** (rows.jsonl L64): `benchmark/results/streams/plan/bcb83ca48fdb6973_chain_fastapi-edit-5_tilth_forced_rep0_20261026T171413208183.turn1.trajectory.jsonl`
- **MT1** (rows.jsonl L65): `benchmark/results/streams/plan/9e6cdfa4f3d16870_Netflix__metaflow.b390a8d4.test_stub_generator.7bf08c98.lv1_tilth_forced_rep1_20261026T171922949688.trajectory.jsonl`
- **MT2** (rows.jsonl L66): `benchmark/results/streams/plan/3b7068ba201b6fc9_Netflix__metaflow.b390a8d4.test_stub_generator.7bf08c98.lv1_tilth_forced_rep2_20261026T172528153411.trajectory.jsonl`
- **CT1** (rows.jsonl L67): `benchmark/results/streams/plan/106ca86a7adff931_caddyserver__caddy-6288_tilth_forced_rep1_20261026T172726610937.trajectory.jsonl`
- **CT2** (rows.jsonl L68): `benchmark/results/streams/plan/183973802fb2dc2a_caddyserver__caddy-6288_tilth_forced_rep2_20261026T172851990601.trajectory.jsonl`
- **TT1** (rows.jsonl L75): `benchmark/results/streams/plan/bddef0af414fa7d5_tokio-rs__tokio-4867_tilth_forced_rep1_20261026T174632936331.trajectory.jsonl`
- **TT2** (rows.jsonl L76): `benchmark/results/streams/plan/1f1ed4e773425702_tokio-rs__tokio-4867_tilth_forced_rep2_20261026T174728912621.trajectory.jsonl`
- **FT1** (rows.jsonl L83): `benchmark/results/streams/plan/48ecee37f2f83829_chain_fastapi-edit-5_tilth_forced_rep1_20261026T180146781792.turn1.trajectory.jsonl`
- **FT2** (rows.jsonl L84): `benchmark/results/streams/plan/bf832f817ca1c2c9_chain_fastapi-edit-5_tilth_forced_rep2_20261026T180612917609.turn1.trajectory.jsonl`

_Source: Codex gpt-6-astra diagnosis of the gepa-v3 seed trajectories (main `202027d`), ingested from the run worktree report · Updated: 2026-10-29_
