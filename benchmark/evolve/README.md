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
