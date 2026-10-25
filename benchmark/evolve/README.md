# Evolve

GEPA search over tilth commits. A candidate scores each task as `p * base / c` (cost per correct).

## Score and finish rule

`p` is the candidate pass rate on the task. `c` is the candidate mean row cost (`total_cost_usd`, else
`charged_usd`). `base` is the frozen baseline-arm mean `total_cost_usd` for the task. A score of 1.0 means
baseline parity. A task never solved scores 0. The ratio has no cap. Only an absent or null `total_cost_usd`
falls back to `charged_usd`. A negative, non-finite, or malformed cost, or a solved task with a mean cost of 0,
stops the run.

`buy_baselines` buys the cheap, dev and test baselines before any candidate cell. A task with no completed
baseline row, or a baseline cost that is not a number above 0, refuses the run (exit 2).

Finish opens a PR only if both hold: the lower bound of the paired test-split bootstrap interval of the
per-task score delta (winner minus seed) is above 0, and the winner's aggregate test pass rate is at least
the seed's.

Paid-tier side info for reflection carries `cost_pairs` for the task: the candidate's rows and the frozen
baseline rows, each cut to cost, turns, tokens, tool calls, tool sequence, and batch sizes. Reflection can
so see why a candidate costs more than stock Claude. The pairs carry no trajectory and no grader output.
The dispatcher sends reflection and the `src/` proposer one `scores` record per candidate (task scores, pass
rates, mean costs, baseline costs) and one `cost_pairs` record per task. Both prompts state the score
(`candidate.OBJECTIVE`) and ask for lower cost without lost correctness.

## Diagnosis records

After a candidate's paid dev tier, `attribution.summarize` runs once per content id on the candidate's dev
rows and the frozen baseline dev rows. It never reads test-split rows or grader text. A failure is logged and
skipped. The side info for the candidate carries a compact `attribution` view under its own key (never under
`scores`), at most about 4k characters: coverage, the leading causes with evidence, `call_shapes` and `errors`.
The dispatcher sends it, with a `source_map` record for the leading causes, to reflection and the proposer.

`diagnosis.WeightedSelector` is the gepa `module_selector`. It weights each component by the parent's
`dominant` hints (the hinted component takes about 44% of the picks, against 20% for a uniform choice), picks
`src_patch` within the first 2 proposals, and leaves no component out for more than 10 proposals in a row. The
bound is `MAX_UNSELECTED`. Each pick goes to `log.txt` with its evidence and reason. A reflection or proposer call
receives a `replay` record only from its own candidate, never from another one.

Reflection and the proposer end a reply with one `<hypothesis>` block (`metric`, `scope`, `direction`,
`tolerance`, `invariants`). `hypotheses.jsonl` in the run directory holds it, keyed by parent and child content
id, and then the outcome (`met`, `unmet`, `unmeasurable`) after the child's paid dev tier. Later calls get the
last outcomes as a `hypothesis_feedback` record. A hypothesis is soft: it never rejects a candidate.

`--replay-diagnostic N` (default 0, off) replays the first N seed dev cells (by task, then repetition) against
the seed binary and the candidate binary when the candidate changes `src_patch`. The `replay.compare` result goes
to `log.txt` and to later calls as a `replay` record. It reuses the loop's build directory, writes no result
rows, and never rejects a candidate.

## Strict file tools (default)

Every cell runs with Bash guarded by the tilth hook (`tilth hook`). The tilth arm runs the candidate
binary's own hook with `--hint tilth`. The baseline runs the seed's hook with `--hint native`.
Every cell uses `hook_sha` = the seed sha, so baseline run keys depend on the seed hook source.

Pass `--no-strict-file-tools` to run cells without the hook. Stored non-strict baselines are not
reused by a strict run, and the reverse. The first strict run buys a full strict baseline.

## Seed-hook preflight

Before any paid call, a strict run builds the seed hook and replays every row of the frozen corpus
(`benchmark/hook_corpus.json`) through it in both hint modes. A `must_deny` row that the hook allows,
or a `must_allow` row that it denies, refuses the run (exit 2). The log names the failing rows.

## Hook gate

A candidate may edit `src/hook/**`. After its build and before its first cell, the cascade replays the same
corpus through the candidate binary in both hint modes. Any wrong verdict fails the candidate at stage
`hook gate` with score 0 and runs no cells. The failing commands go to reflection in the stage tail.
A candidate that cannot allow `go test ./...` or deny `cat src/lib.rs` with the right hint text
also fails at `hook gate`, through the harness probe. The corpus file itself stays outside the
candidate allowlist (`src/`, `prompts/`).

## Rejected-candidate cleanup

Each built candidate keeps a copy of its binary in `results/candidates/<sha>/` (about 39 MiB). Evolve
deletes that directory when the candidate is definitively rejected: it fails at `apply`, `just check`,
`build`, `hook gate`, or `cheap tier`, is dominated after one rollout or after the re-runs, or is evicted from
the frontier by a dominating candidate. It keeps the directory when a rollout did not complete, because a
resumed run reuses stored rows keyed on the binary digest and a rebuild may differ byte for byte. It never
deletes the seed's directory (the seed binary is the hook binary for every cell) or the shared
`candidates/target` build cache. A deletion error is logged, not raised.

## Seed-hook replay floor

Every strict cell replays each Bash call through the seed hook. A cell that ran a call the seed hook
denies raises `HookLoosenedError`. A candidate therefore cannot loosen the hook to win cells.
