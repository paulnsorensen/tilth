# tilth benchmark — cross-file edit series (haiku)

**Model under test:** haiku · **Repo:** gin @ `d7776de7d444935ea4385999711bd6331a98fecb` (Go 1.26) ·
**tilth:** v0.8.4 · **Date:** 2026-05-30 · **Branch/PR:** `paulnsorensen/run-benchmarks-v1` (#31)

## Thesis

Structural code navigation (tilth) pays off in **correctness and reliability** — not just efficiency —
**only when the bug's cause is hidden from both text search and git history**, on code the model can
still partly reach by domain prior. When either oracle (a tidy `git show <bug-commit>` diff, or a
strong prior about where the bug lives) is available, baseline grep/Read ties tilth on correctness and
the gap collapses to efficiency at best.

## Results matrix (pooled across all haiku runs, correctness first)

Bug classes: **compile** (build breaks), **runtime** (wrong values, builds fine), **logic** (one-token
flip, builds + routes fine, only param value wrong). `cpc` = cost-per-correct = total spend / #correct
(expected cost under geometric retry). Turns/cost are medians over **completed** cells.

| task | class | git? | mode | pass | turns | med $ | cpc $ | tilth_search |
|---|---|---|---|---|---|---|---|---|
| render_cascade | compile | yes | baseline | **6/6** | 46 | 0.20 | 0.20 | 0 |
| render_cascade | compile | yes | tilth | **5/6** | 23 | 0.17 | 0.25 | 13 |
| render_runtime | runtime | yes | baseline | **3/3** | 16 | 0.07 | 0.08 | 0 |
| render_runtime | runtime | yes | tilth | **3/3** | 9 | 0.06 | 0.06 | 3 |
| route_catchall | logic | yes | baseline | **6/6** | 33 | 0.23 | 0.31 | 0 |
| route_catchall | logic | yes | tilth | **6/6** | 34 | 0.28 | 0.28 | 38 |
| route_catchall_nogit | logic | **no** | baseline | **2/3** ⚠️ | 72 | 0.67 | 0.67 | 0 |
| route_catchall_nogit | logic | **no** | tilth | **3/3** | 22 | 0.14 | 0.13 | 14 |

⚠️ baseline's lost no-git cell is a **timeout** (`error:"timeout"`), not a wrong answer — see caveats.

## Headline — the git-oracle A/B (`route_catchall` vs `_nogit`)

Same one-token bug (`n.path[2:]→[1:]` in `tree.go` `getValue`, breaks `*wild` param values), same
prompt, same gate tests. The only difference: in `_nogit` the harness writes the edit **without
committing** and hides `.git`, so `git log/show/diff/blame` all fail.

- **git available (committed):** both modes **6/6**. Tracing transcripts: baseline used grep 1–2×
  then `git log → git show <bug-commit> → git diff` — git was the oracle, not grep. tilth searched
  genuinely (38 calls total) but **layered on top of** the same git shortcut → no correctness gain,
  slightly more turns.
- **git removed (no-git):** the split appears.
  - **baseline** *tried* git anyway (16/7/6 `git` mentions across reps → all hit `not a git
    repository`), then brute-forced via Bash (40–46 calls). Two reps converged at **69 & 75 turns**;
    the third **timed out**. cpc **$0.67**.
  - **tilth** navigated structurally (`tilth_search` 3/4/7, ~0 `git log/show`), verified with
    `go test`, converged at **15/22/25 turns**, **3/3**. cpc **$0.13**.

Removing the git oracle is what finally separated the modes — on the committed twin both were 6/6.
**≈5× cost-per-correct advantage for tilth, plus a reliability edge** (no timeouts vs baseline's 1/3).

## Honest caveats

- **Baseline's no-git "failure" is a timeout, not a wrong fix.** The agent ground so long without the
  oracle it hit the wall. The accurate framing: *tilth converges reliably & cheaply; baseline grinds
  ~2× the turns or fails to converge in time* — **not** "baseline picked a wrong answer."
- **render_cascade tilth 5/6 is a genuine miss** (29 turns, no error, 6 edits, gate stayed red) — tilth
  is not strictly dominant; on the compile cascade it lost one cell baseline didn't.
- **The domain prior is still live.** gin is famous; a capable model knows param math lives in
  `tree.go` and can teleport there from the failing test name. Only a *synthetic* repo + a *de-leaded*
  prompt removes this last shortcut. So the no-git result isolates the **git** oracle, not the prior.
- **Small n (3–6 reps/cell).** The no-git correctness split rests on a single timeout; it needs 5+ reps
  to firm up from "suggestive" to "stable."

## Tool-profile evidence (why the numbers move)

- baseline runs show **0 `tilth_search`** by construction; their work is grep + `git show` (git cells)
  or brute Bash (no-git cells).
- tilth runs show real symbol-walking: 3–38 `tilth_search` per cell, with `go test` for verification
  and **near-zero `git log/show/blame`** in the no-git cells (1/0/0) — i.e. tilth didn't need the oracle.

## Limitations & next steps

1. **Scale the no-git A/B to 5+ reps** — confirm the baseline drop is a stable split vs timeout noise.
2. **Kill the domain prior** — rebuild the navigation bug in the synthetic repo with a lean
   "these 3 tests fail, fix the regression" prompt. Synthetic + no-git + lean-prompt is the only config
   that removes *all* shortcuts and could split correctness on the merits of navigation alone.
3. **Up-model sweep (sonnet)** — does the efficiency/correctness gap hold or widen?

## Provenance (raw results, gitignored — recorded here for traceability)

| file | task(s) | cells |
|---|---|---|
| `benchmark_20260530_043404_haiku.jsonl` | render_cascade | base×3, tilth×3 |
| `benchmark_20260530_043511_haiku.jsonl` | render_cascade | base×3, tilth×3 |
| `benchmark_20260530_050443_haiku.jsonl` | render_runtime | base×3, tilth×3 |
| `benchmark_20260530_055105_haiku.jsonl` | route_catchall | base×3, tilth×3 |
| `benchmark_20260530_061813_haiku.jsonl` | route_catchall +_nogit | base×3+3, tilth×3+3 |
