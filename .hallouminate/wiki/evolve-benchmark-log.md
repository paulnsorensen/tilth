# Evolve benchmark log

The evolve benchmark log is `benchmark/log/evolve.jsonl`: one JSON entry per evolve run past preflight and the baseline purchase, with a light and a dark SVG chart next to it (`evolve.svg`, `evolve-dark.svg`). The top `README.md` embeds both charts. The owner made three decisions about it in November 2026 (PR #471). The full contract is in `benchmark/evolve/README.md` § Benchmark log.

## Decision 1: evolve runs Sonnet only

- `cli.py --model` accepts only the Claude names in `run.MODELS` whose model is a Sonnet (`benchlog.sonnet_models`). The default is `sonnet`. Any other model is a usage error (exit 2).
- Reason: scores from different agent models do not compare, so the log and its chart hold Sonnet runs only.
- A band of the chart is one panel and one model (`model_id`, else `model`). `sonnet` can resolve to a new model id over time, and the chart then starts a new band.
- Before this decision, haiku was the default. A tools-sonnet-r1 launch without `--model` ran haiku by accident ([gepa-v3 evolve campaign state](benchmark-gepa-v3-evolve.md)).

## Decision 2: the run builds its entry from memory

- `Evolution.benchmark_entry` (`benchmark/evolve/loop.py`) builds the entry from the run's own state: settings, panel, seed and frontier cascades, spend totals, arms, and a result record that `finish()` fills.
- Every `finish()` return path sets an explicit outcome kind: `pr_opened`, `no_improvement`, `incomplete`, `refused`, `failed`, or `pr_failed`.
- The run writes the entry to `<run_dir>/benchmark-entry.json`.
- `tilth_version` is the version that the seed build reports, with its `+<sha>` suffix. Each candidate build embeds its own commit, so the run's rows hold several versions.
- The text round trip is gone: the `run:`, `frontier:`, and `arms:` log lines, the log-parsing regexes, `entry_from_run_dir`, and the `import` subcommand. The `finish:` and `spend:` lines did not change.
- Reason: parsing `log.txt` duplicated state the run already holds, and a reworded log line silently gave a wrong or `null` outcome.

### Gotcha: the first 12 entries came from the removed importer

- The 12 entries from 2026-10-18 to 2026-11-06 were imported from old run directories (`log.txt` plus `rows.jsonl`).
- A stored row names its candidate commit in `git_sha`. The backfill took the seed and best arms only from exact evidence: the `run seed:` line, a finalist's `refs/evolve/<run-id>/<cid>` ref, the one non-seed commit with test-split rows, or the pushed `origin/evolve/<run-id>-winner` branch.
- Runs that gained tilth arms: tools-sonnet-r1 (dev and test), preamble-r2 (dev and test), preamble-r1 (dev), sonnet-r8b (dev and test), sonnet-r7b-finish (test). sonnet-r1 to r6 and sonnet-r8 keep `null` seed and best arms.
- Check: the backfilled test pass rates equal each run's logged `finish: test-split means`, and the tools-sonnet-r1 seed rows give the logged seed dev mean 0.905.

## Decision 3: each run opens its own log PR

- Without `--benchmark-log`, a run fetches `origin/<base-branch>`, adds a temporary detached worktree there, appends its entry, redraws both charts with the running code, commits, pushes `benchmark-log/<run-id>`, and opens a ready PR (`benchmark/evolve/logpr.py`). The run checkout does not change. The tool never merges.
- A publish fault writes one `benchmark log: <step> failed (...)` line, keeps `benchmark-entry.json`, removes the worktree, and does not change the exit code.
- `python3 benchmark/evolve/benchlog.py publish RUN_DIR` publishes the entry again. `--benchmark-log PATH` keeps a local log and opens no PR.
- Reason: `main` has the ruleset "main: PR + CI", so a run cannot push the log to `main`. Each run delivers its own entry as a PR instead of a manual commit after the run.

### Gotcha: two open log PRs conflict

- Each log PR appends a line to the same file, and GitHub does not apply `merge=union`. The second PR conflicts after the first one lands.
- Fix: `benchlog.py publish RUN_DIR --replace`. It rebuilds the commit on the new base and overwrites the run's own branch with `--force-with-lease`. An open PR from that branch stays.
- Without `--replace`, an existing `benchmark-log/<run-id>` branch is an error that names the flag.

### Gotcha: a resumed run adds a second entry

- A resumed run publishes a second entry for its run id, and the log stays append-only. The chart draws only the last entry of each run id (`benchlog.latest`).

## Tests

- `benchmark/tests/test_evolve_benchlog.py`: entry fields per outcome kind, chart layout, and chart bytes.
- `benchmark/tests/test_evolve_benchlog_publish.py`: real git against a local bare `origin` and a `gh` recorder. No test reaches the network.
- `benchmark/tests/conftest.py` gives every run an offline log client, and a session fixture fails when a test changes the committed `benchmark/log/`.

_Source: PR #471 review (findings 1, 3, 4, 6, 7, 8, 9, 13–17, 23, 31–35) and the owner decisions of 2026-11-22 · Updated: 2026-11-22_
