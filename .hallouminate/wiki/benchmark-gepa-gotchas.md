# Benchmark GEPA overhaul gotchas

The October 2026 GEPA overhaul hit three groups of silent failures: harness flags and grading, gepa 0.1.4 integration, and external-task data.
Each item below names the guard that fixes it. The guards are on [Benchmark GEPA loop invariants and guards](benchmark-gepa-invariants.md).
PR #312 is the all-in-one provenance PR. It closes unmerged, and the work lands as the stacked PRs #313-#319.
General harness failures from the August 2026 investigation are on [Benchmark harness gotchas](benchmark-harness-gotchas.md).

## Harness and grading

- The `bare` variant flag means `--setting-sources ""`, not `claude --bare`. The real `--bare` ignores `CLAUDE_CODE_OAUTH_TOKEN`, so it breaks subscription runs. The flag is set in `_runner_command` (`benchmark/run.py`, near lines 937-943).
- A quota rejection can arrive with no `result` event. Without a result event, a rejected `rate_limit_event` must classify as quota. Otherwise the run treats it as an ordinary error (`detect_quota_rejection`, `benchmark/parse.py:648`).
- pytest-pretty hides the pytest summary line. A gold patch once scored 0 until the grader passed `-p no:pretty`. That flag keeps the `-rA` summary in the log. Since #315, grading reads JUnit XML and not the log (`benchmark/external/featurebench.py:171-180`).
- FeatureBench environment builds fail unless install order and `set -e` match the upstream setup script.
- Stored rows written before the `bare` field existed never fill a baseline slot. Their stock arms re-run instead of raising drift.
- A benchmark cell that reads the checkout's `.cheese/` tree is contaminated (`harness_notes`). Review notes and specs quote local-task ground-truth identifiers.
- Phase-agent self-reviews missed blockers that independent mutation-tested reviews found. See [Multi-agent workflow notes](multi-agent-workflow-notes.md).

## gepa 0.1.4 integration

Each item is a gepa 0.1.4 behavior that silently breaks a naive integration. The gepa source lines come from the evolve spec (`9e84659:.cheese/specs/bench-evolution-loop.md:47,50,52-54,92`). That commit is the head of closed PR #312, reachable through `refs/pull/312/head`. The evolve lines match the layer 6 head of PR #319 after the PR #318 cure.

- gepa sends each dataset item to the evaluator only as the keyword `example`. It drops each keyword that the evaluator does not name (`gepa/optimize_anything.py:1002-1044`). So the evaluator signature is `evaluate(candidate, example)` (`Evolution.evaluate`, `benchmark/evolve/loop.py:325`).
- When `custom_candidate_proposer` is set, it owns every proposal, and gepa never calls `reflection_lm`. The default `reflection_lm` is `openai/gpt-5.1` through litellm, which needs an OpenAI API key. Evolve sets `reflection_lm=None` and registers one dispatcher (`optimize`, `benchmark/evolve/engine.py:109-118`).
- A proposer exception does not stop the search. gepa catches it, calls the proposer again, and then skips the proposal (`gepa/proposer/reflective_mutation/reflective_mutation.py:203-225`, `gepa/core/engine.py:846-849`). So evolve never stops by raising. It sets a shared stop state, and a `StopperProtocol` callback ends the run (`benchmark/evolve/engine.py:37-44`; reasons at `benchmark/evolve/calls.py:16`).
- A candidate that ties the best score on an instance joins that instance's Pareto front (`gepa/core/state.py:496-508`). Grader scores of 0 and 1 make ties routine, so the front can hold a candidate with one rollout. Evolve keeps its own accepted frontier and never reads the gepa front (`_admit`, `benchmark/evolve/loop.py:421-453`).
- gepa evaluates a child on its minibatch before the child gets a pool index. A child that fails minibatch acceptance never gets an index (`gepa/proposer/reflective_mutation/reflective_mutation.py:517,533-550`). So evolve gives each content id a first-seen sequence number and orders candidates only by it (`new_cascade`, `benchmark/evolve/loop.py:215`).

## External-task data

These items come from the external-tasks spec (`9e84659:.cheese/specs/bench-external-tasks.md:59,68,101`).

- The gin fixture at `d7776de7` (`benchmark/config.py:130`) already holds the gold change of `gin-gonic__gin-3741` (`logger.go:93-94`). An external cell that reads the local clones under `/tmp/tilth_bench/repos/` can see the fix. The contamination scan flags such a read as `foreign_repo_clone` (`benchmark/external/contamination.py:175`). Unverified: this is a spec claim. The fixture was not on disk for a check on 2026-10-13.
- A FeatureBench mask `patch` can also edit a FAIL_TO_PASS file. The row `pydata__xarray.97f3a746.test_backends_chunks.fa55f68a.lv1` removes an import line from `xarray/tests/test_backends_chunks.py`. The `test_patch` is a deleted-file diff against `base_commit`, so it does not apply after a mask hunk edits that file. The mask applies forward (`mask_patch_forward`, `benchmark/external/featurebench.py:118`).
- `repo_settings.docker_specs.run_args.cuda_visible_devices` is not a GPU signal, because CPU-only Pareto-12 rows set it. The curated CPU candidate list and an admission round trip on a CPU host keep GPU tasks out.


- A grade must not copy a whole host cache (`isolate_caches`, `benchmark/external/proc.py:71-97`). Before October 2026, each Rust or Go grade copied the host Cargo registry or Go module cache with `cp -a --reflink=auto`, which is a full copy on ext4. On crabbot the Go copy was 8.5 GB: one `gin-gonic__gin-3741` grade spent 802 s in cache prep and held 8.7 GB of scratch. Now a Go grade starts with an empty `GOMODCACHE` and a `GOPROXY` of `file://<host GOMODCACHE>/cache/download`, then the inherited proxy. Go extracts only the modules that it uses and checks them against `go.sum`. A missing file is a 404, so the comma falls through to the next proxy. A Rust grade copies the registry index, `git/`, `config.toml`, and only the `.crate` archives that its `Cargo.lock` pins. Cargo extracts their sources again. The same Go grade now takes 0.0 s of cache prep and 377 MB of scratch.
- Tests that grade fixture projects still point `CARGO_HOME` and `GOMODCACHE` at empty directories (`empty_dependency_caches`, `benchmark/tests/external_support.py:67`), because the Rust index copy still costs time. Do not make it autouse: `test_evolve` runs a real `cargo check --locked` on tilth, which needs the real registry.
- `check_correctness` turns SIGTERM into `SystemExit(143)` (`unwind_on_sigterm`, `benchmark/external/proc.py`), so a terminated grade removes its `tilth-external-grade-*` scratch and kills its test process group. SIGKILL still leaks the scratch. A slow `rmtree` of a multi-GB copied cache was a likely kill window: one leaked dir kept only `checkout-caches/gomodcache`.

## gepa-v3 evolve rounds

The gepa-v3 rounds (October 2026, runs gepa-v3-r1 to r6) hit these failures; each names its fix or workaround. The campaign state is on [gepa-v3 evolve campaign state](benchmark-gepa-v3-evolve.md).

- A Claude Code auto-update (2.1.310 to 2.1.311) changed `cli_version` in every run key, so every stored cell drifted. The run re-bought baselines and its finish reserve doubled to $78. Fix: the CLI pin in `benchmark/claude-cli.version` (#411).
- Hint text in `src/hook/hints.rs` is part of the hook source key, so a prompt-only hook-hint edit (#403) drifted every strict baseline cell (about $28 to re-buy).
- A chain's digest once hashed all of `chains.py`, so editing one chain drifted every chain's baselines (#400 fixed it).
- Changing a panel's split after rows exist trips the split lock; register the changed split under a new panel name (gepa-v3-long-2, #401).
- `setsid nohup wrapper.sh &` returns the wrapper's pid, not the evolve python's. `pgrep -f '<pattern>'` also matches the shell that runs the check. Find the evolve process by `/proc/<pid>/exe`, or watch the run's `log.txt`.
- The candidates share one cargo target dir. A newer dep-info file from another worktree let cargo reuse the seed's binary and run the wrong tests. The integrity stage and `default_just_check` now touch `*.rs` sources before each cargo call (#412).
- A new seed commit changes `git_sha` in every tilth cell's key, so the finish reserve is about $40 even when baselines are reused. Size `--max-usd` above reserve plus seed rescore plus candidates.
- The evolve `src_patch` proposer has no Bash, so it cannot see clippy or rustfmt output; a one-lint failure used up a directive until repair (#408) and auto-format (#410).
- bat's pty integration test hangs the agent's full `cargo test`; abandoned `grep | head` pipes then leak until killed.
- A fastapi local task had no agent venv, so the agent fought missing pytest and argon2 until the 600 s timeout, and one incomplete seed cell stopped the run (#407).
- Text rules cannot guard test integrity. Reviews broke them with a macro on the attribute line, `fn r#name`, `#[cfg(any())]` above the test module, and fn-body and macro wrapping. Use the behavioral integrity stage (#412).

## Symptoms and first checks

Use this list to route a failure to the right section above.

- A subscription run fails to authenticate. Check that no variant passes the real `--bare` flag.
- A run ends as an ordinary error after a usage limit. Check for a rejected `rate_limit_event` with no `result` event.
- A gold patch scores 0 in a Python task. Check the grading report. Grading must read JUnit XML, not the pytest log.
- A baseline slot re-buys after a harness change. Check whether the stored row predates the `bare` field.
- A GEPA run raises in the proposer and keeps going. Expect this. Stop it through the shared stop state, not an exception.
- An external cell solves a gin task at once. Check the `foreign_repo_clone` hit in the contamination scan.
- A FeatureBench task fails to apply `test_patch`. Check whether the mask `patch` edits a FAIL_TO_PASS file.
- A GPU task passes admission on a CPU host. Do not trust `cuda_visible_devices`; use the curated CPU list.
- Rows from older stores never match. Expect one `--refreeze-baselines`.
- Every stored cell drifts at once. Check `cli_version` against `benchmark/claude-cli.version` first, then the hook source key.
- An evolve run stops at its ceiling with no candidate scored. Check whether the finish reserve plus the seed rescore exceeds `--max-usd`.
- A wait loop reports the evolve run exited while it still runs. Check the python pid, not the wrapper pid or a `pgrep -f` self-match.

_Source: PR #312 session archive (closed unmerged; work lands as #313-#319); gepa and data gotchas from specs at `9e84659` (`refs/pull/312/head`); code verified at the layer 6 head of PR #319 after the PR #318 cure; gepa-v3 section from runs gepa-v3-r1 to r6 and PRs #396-#412 · Updated: 2026-10-29_
