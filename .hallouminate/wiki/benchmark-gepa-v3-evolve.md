# gepa-v3 evolve campaign state

The gepa-v3 evolve campaign (October 2026) brought tilth to cost-per-correct parity with the built-in-tools baseline, on a long-session panel.
Main at `b2f4412` is within noise of baseline on the dev split, and 2-6% cheaper per correct answer on the held-out test split.
The evolve loop is now hardened end to end, but its dev split is too noisy to rank single candidates that differ by a few percent.
Guards added during the campaign are on [Benchmark GEPA loop invariants and guards](benchmark-gepa-invariants.md); failures it hit are on [Benchmark GEPA overhaul gotchas](benchmark-gepa-gotchas.md).

## Panel: gepa-v3-long-2, superseded by gepa-v3-long-3

The gepa-v3 panel exists because real coding sessions run about 93 API calls, while the old gepa-v2 tasks ran 5-6, so carry cost never showed.
`benchmark/panels/gepa-v3-long-2.json` holds 18 members, 10 dev and 8 test, scored with sonnet at 3 reps.

- **Long single tasks:** metaflow (FeatureBench), caddy-6288, axum-1119, vuejs-11739, and the four local edit tasks (gin, express, fastapi, ripgrep).
- **Chains:** `chain:gin-edit-5`, `chain:fastapi-edit-5`, `chain:rg-edit-7` and `chain:express-edit-6` run several tasks in one resumed `claude -p` session (`benchmark/chains.py`). A chain is correct only when every sub-task is correct.
- **Short controls:** tokio-4867, ripgrep-2209, caddy-6350, terraform-35611, axios-5085 and coreutils-6682, so evolve cannot win long tasks while losing short ones.
- **Dropped:** bat-1892, because bat's pty integration test hangs the agent's full `cargo test`, and the `gin_context_next` and `grok_context_next` chain sub-tasks, because chain answers omit their literal `HandlersChain` grading string.
- The panel loader refuses any task that reaches both dev and test, directly or as a chain sub-task (`_check_no_shared_task`, `benchmark/panels.py`).

`benchmark/panels/gepa-v3-long-3.json` is long-2 with two chains swapped. It keeps the seed, the split (10 dev, 8 test), and all six short controls.
`chain:rg-edit-7` becomes `chain:rg-edit-6` (no `grok_rg_matcher`), and `chain:fastapi-edit-5` becomes `chain:fastapi-edit-4` (no `fastapi_request_validation`).
Do not edit long-2: stored runs carry its name and split digest. Use long-3 for new runs.

## Panel health (October 2026)

`benchmark/panel_health.py` builds the per-task table from result rows; re-run it after each run.
The numbers below count every paid row of gepa-v3-r1 to r6, gepa-v3-baselines and preamble-r1 (sonnet, CLI 2.1.310), skipping reused rows.

| Task | Split | Baseline | Tilth | Failure class | Max wall / timeout | Verdict |
|---|---|---|---|---|---|---|
| `chain:rg-edit-7` | test | 5/13 | 9/20 | `grok_rg_matcher` sub-task in all 19 failures | 400 s turn / 600 | Replaced by `rg-edit-6` |
| `chain:fastapi-edit-5` | dev | 14/16 | 65/71 | `fastapi_request_validation` sub-task in all 8 failures | 348 s turn / 600 | Replaced by `fastapi-edit-4` |
| tokio-4867 | dev | 11/11 | 60/77 | F2P, tilth arm only | 74 s / 1800 | Kept |
| ripgrep-2209 | test | 6/8 | 17/20 | F2P `regression::r2208`, both arms | 78 s / 1800 | Kept, watch |
| `rg_edit_flow_migration` | test | 10/10 | 19/20 | One `$1` budget cap | 307 s / 600 | Kept, timeout 900 s |
| `fastapi_edit_security_raise_on_missing` | dev | 16/16 | 77/78 | One timeout, before the #407 venv fix | 241 s / 600 | Kept, timeout 900 s |
| vuejs-11739 | test | 10/10 | 18/20 | F2P | 824 s / 1200 | Kept |
| metaflow | dev | 13/13 | 77/78 | One contaminated row | 732 s / 1200 | Kept |

The other ten members pass at least 92% of cells in both arms and use at most 40% of their timeout.

- **Grading false negatives.** `grok_rg_matcher` requires the literal `impl Matcher for RegexMatcher`; chain answers write "`Matcher` impl (line 409)". `fastapi_request_validation` requires an `async def app` form; chain answers write "an async closure, `app(request)`".
- Without these sub-tasks, rg-edit-7 passes 13/13 and 20/20, and fastapi-edit-5 passes 15/16 and 71/71.
- The fix drops the sub-tasks from new chains, as for `gin_context_next`. A grader edit would change the task digests and move the stored long-2 chain keys.
- **tokio-4867** fails only in the tilth arm. The failures are quick (8 turns) alternative fixes in `recv_ref` that the hidden test rejects; the sampled passing runs fix `new_receiver`. The grade builds in 9 s, so it is not flaky.
- **ripgrep-2209** fails in both arms (25% and 15%). The grade compiles in 6 s, then `r2208` fails on a different `replace_all` fix, so the failures are real wrong answers. It stays as a short control, but it adds noise to the test split.
- **Background-task wake-ups.** When an agent backgrounds a test run, each notification starts a new one-turn result in the same stream, and the row keeps only the last. Seven slow rows report 1-3 turns for 15-37 real turns.
- vuejs rep 0 of preamble-r1 is one of them: 37 turns for $0.43, then 3 wake-ups, for 824 s of wall time. Cost stays correct because the last result event is cumulative; `num_turns` and `duration_ms` do not.

## Cell timeouts and budget

- `timeout_s` and the per-cell `--max-budget-usd` are both run-key inputs (`cell_identity`, `benchmark/run.py`). `_task_source_files` also hashes the source file of each task.
- `rg_edit_flow_migration` and `fastapi_edit_security_raise_on_missing` now set `timeout_s = 900`. That keeps 2x headroom over the slowest stored turn (400 s and 348 s) for a loaded host.
- Only metaflow (732 s) and vuejs (824 s, a wake-up row) used more than 40% of a timeout among the other tasks, both against 1200 s. Their timeouts are per language in `benchmark/external/task.py`; a change would move every Python or JavaScript external key, so they stay.
- Re-buy for a long-3 run: the baselines of the two edit tasks and of the two new chains. Stored baseline means at `$3` are $1.04 (rg edit), $0.48 (fastapi edit), about $2.63 (rg-edit-6) and about $1.10 (fastapi-edit-4).
- That is about $5.25 per repetition, or $15.75 at 3 reps; the timeout changes are $1.52 of each repetition. The other 14 members keep their keys.
- Long-2 also loses the stored keys of those two tasks and of its `chain:rg-edit-7` and `chain:fastapi-edit-5` cells.
- The budget cannot vary per task: `--cell-budget-usd` sets one budget for every cell, and a chain gets that budget times its size.
- At `$1`, `rg_edit_flow_migration` sits at the cap in both arms (baseline mean $0.94, max $0.99; one tilth cell capped after 57 turns). At `$3`, r1-r6 never hit a cap; the costliest single cell was $1.09 and the costliest chain $4.09 of $21.
- Run long-3 with `--cell-budget-usd 3`. That matches r1-r6, so their stored `$3` baselines (the `long-pool-screen` result store) answer the 14 unchanged members.

## Cost per correct: where tilth stands

Cost per correct is total spend divided by correct cells; below 1.0 means tilth is cheaper than baseline.
The evolve score `p * base / c` is stricter: its parity line is the baseline pass rate, so on the test split (baseline 21/24) parity sits near 0.875, not 1.0.

| Run | Seed | Dev cost per correct vs baseline | Test cost per correct vs baseline |
|---|---|---|---|
| r1 | `202027d` (old seed) | +9% | not run |
| r2 | not accepted | no score | no score |
| r3 | `eab2279` + #409 candidate | +8% | -2% |
| r4 | `25e48dd` | +2% | not run |
| r5 | `58d4a75` | +4% | not run |
| r6 | `b2f4412` | +11% | -6% (22 of 24 cells) |

r2 has no score: its seed was not accepted, because a fastapi cell timed out before the venv fix.
The same tilth code (content id `4c56a06`) scored dev means of 0.952, 0.945 and 0.876 in r4, r5 and r6.
So the dev split moves about 8 points between runs of identical code; trust the test split and paired intervals, not single dev means.

## Why tilth cost more: trajectory diagnosis

The cost gap is driven by extra dependent turns, not by more tool calls.
A turn-by-turn diagnosis by Codex `gpt-6-astra` over the seed's trajectories counted 308 API calls for tilth against 196 for baseline. Tilth made 334 tool calls and baseline made 356.
The report is in the corpus at [Seed API-call gap: tilth versus built-in tools](sources/gepa-v3-call-gap.md).

- Go receiver-signature searches missed, and unique hits stopped at 60 lines, so caddy-6288 took 27 calls against baseline's 8. Fixed by #404.
- The 10-hit search cap and untagged test hits forced narrowing and re-reads before edits. Fixed by #404.
- Strict Bash hook denials cost turns (38 for tilth against 20 for baseline). Fixed by #403 with an operation map in `prompts/mcp.md` and the denial hints.
- A read error or a host-clipped read marked unseen lines as seen, so later reads got false "unchanged" stubs. Fixed by #405 (displayed-lines-only records and a 40 KB response cap).
- `create_file` on existing files. Fixed by #403 in `prompts/tools/write.md`.
- Harness artifacts: a fastapi phrase-check false negative (#402), a fastapi agent without pytest or argon2 that timed out (#407), and shared fixed `/tmp` scratch paths (not fixed).

After the fixes, tilth makes fewer calls than baseline on 7 of 10 dev tasks; for example, the fastapi chain fell from 44 to 31 calls.

## What evolve found

Across r1-r6, evolve's best winner was a prompt edit, not a source patch.

- r3 found a two-call multi-file edit pattern for `prompts/mcp.md` (dev 0.977 against 0.932, test 0.958 against 0.833). Its paired test interval included 0, so it shipped by hand as #409.
- Source-patch directives mostly failed on clippy, rustfmt, or pinned tests until #408, #410 and #412 gave the proposer repair, auto-format, and test-update paths.
- The r6 `budget.rs` numbered-line cut led the dev split (0.973) but cost 14% more per correct than baseline on test, so it was noise.
- A hypothesis that a change cuts one tool's calls was often met while the score fell, so per-tool metrics do not predict cost per correct.

## Spend

Evolve spend totals about $487: r1 $99, baseline buy $32, r2 $45, r3 $161, r4 $12, r5 $48, r6 $89.
The finish reserve is about $40 whenever the seed commit changes, because `git_sha` is in every tilth cell's run key.

## Ideas to push cost per correct lower

These ideas cut dependent turns, the measured cause of tilth's extra cost.
They are untested; the call savings are the deep-diagnosis estimates for the main seed (`4c56a06`), and the ranges overlap, so do not add them.
The recommended order is ideas 1, 2 and 4 by hand, then one higher-rep test-split measurement, rather than more evolve rounds.

1. Explicit search modes (literal, regex, symbol), so auto-resolution stops narrowing or changing the agent's intent. This is the largest remaining cause, about 0.45 calls per cell.
2. Edit from search: let `tilth_write` edit lines that a search hit displayed, now that #404 tags test hits, to skip a read turn.
   Measured on the gepa-v3 streams: after #404, 85% of search-read-write sequences read because the write touched lines the search did not display (imports, the rest of a multi-line statement, other hunks); only 8% re-read lines the search had shown.
   A usage hit now prints its enclosing syntax node of at most 4 lines (`CONSTRUCT_CONTEXT_MAX_LINES`, `src/search/mod.rs`). An offline replay made 12% of sequences read-free for 7% more search output; 12 lines gave 14% for 32% more.
   Import edits remain the largest unserved group.
3. Structural or exact-text edits instead of line-range edits, to cut repair loops such as a leftover brace after a partial replace (about 0.35 calls per cell).
4. Coalesce adjacent read sections and return complete enclosing constructs, to cut fragmented reads (about 0.30 calls per cell).
5. Prevent malformed `tilth_write` JSON: the seed had 5 rejected write calls against none for baseline. Smaller structural payloads or a schema-first example would help (about 0.17 calls per cell).
6. Batch independent discovery: baseline searches two entry points in its first turn, while tilth spends two turns (about 0.13 calls per cell).
7. Smaller tool schemas and instructions, since they ride every API call; [MCP cost model](mcp-cost-model-sonnet5.md) found the fixed MCP prefix, not per-call output, is where cost went. This is unmeasured on this panel.
- A per-cell `TMPDIR` for scratch isolation, which does not change run keys.
- Measurement: more reps or more test tasks, so evolve can see 5% effects; about $100 or more per comparison.

_Source: gepa-v3 evolve session, runs gepa-v3-r1 through gepa-v3-r6 and PRs #396-#412; PR #413 review fixes (r2 note, call-count labels, call-gap source copy); panel-health pass over r1-r6, gepa-v3-baselines and preamble-r1 with `benchmark/panel_health.py` · Updated: 2026-10-31_
