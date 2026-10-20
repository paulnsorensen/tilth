# Evolve

GEPA search over tilth commits. A candidate's score is grader correctness over its paid dev rollouts.

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

## Seed-hook replay floor

Every strict cell replays each Bash call through the seed hook. A cell that ran a call the seed hook
denies raises `HookLoosenedError`. A candidate therefore cannot loosen the hook to win cells.
