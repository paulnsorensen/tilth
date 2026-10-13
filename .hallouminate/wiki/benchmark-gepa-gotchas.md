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

Each item is a gepa 0.1.4 behavior that silently breaks a naive integration. The gepa source lines come from the evolve spec (`e42d451:.cheese/specs/bench-evolution-loop.md:47,50,52-54,92`). That commit is reachable through `refs/pull/319/head`. The evolve lines match the layer 6 head of PR #319.

- gepa sends each dataset item to the evaluator only as the keyword `example`. It drops each keyword that the evaluator does not name (`gepa/optimize_anything.py:1002-1044`). So the evaluator signature is `evaluate(candidate, example)` (`Cascade.evaluate`, `benchmark/evolve/loop.py:323`).
- When `custom_candidate_proposer` is set, it owns every proposal, and gepa never calls `reflection_lm`. The default `reflection_lm` is `openai/gpt-5.1` through litellm, which needs an OpenAI API key. Evolve sets `reflection_lm=None` and registers one dispatcher (`optimize`, `benchmark/evolve/engine.py:109-118`).
- A proposer exception does not stop the search. gepa catches it, calls the proposer again, and then skips the proposal (`gepa/proposer/reflective_mutation/reflective_mutation.py:203-225`, `gepa/core/engine.py:846-849`). So evolve never stops by raising. It sets a shared stop state, and a `StopperProtocol` callback ends the run (`benchmark/evolve/engine.py:37-44`; reasons at `benchmark/evolve/calls.py:16`).
- A candidate that ties the best score on an instance joins that instance's Pareto front (`gepa/core/state.py:496-508`). Grader scores of 0 and 1 make ties routine, so the front can hold a candidate with one rollout. Evolve keeps its own accepted frontier and never reads the gepa front (`_admit`, `benchmark/evolve/loop.py:414-446`).
- gepa evaluates a child on its minibatch before the child gets a pool index. A child that fails minibatch acceptance never gets an index (`gepa/proposer/reflective_mutation/reflective_mutation.py:517,533-550`). So evolve gives each content id a first-seen sequence number and orders candidates only by it (`new_cascade`, `benchmark/evolve/loop.py:216`).

## External-task data

These items come from the external-tasks spec (`e42d451:.cheese/specs/bench-external-tasks.md:59,68,101`).

- The gin fixture at `d7776de7` (`benchmark/config.py:130`) already holds the gold change of `gin-gonic__gin-3741` (`logger.go:93-94`). An external cell that reads the local clones under `/tmp/tilth_bench/repos/` can see the fix. The contamination scan flags such a read as `foreign_repo_clone` (`benchmark/external/contamination.py:175`). Unverified: this is a spec claim. The fixture was not on disk for a check on 2026-10-13.
- A FeatureBench mask `patch` can also edit a FAIL_TO_PASS file. The row `pydata__xarray.97f3a746.test_backends_chunks.fa55f68a.lv1` removes an import line from `xarray/tests/test_backends_chunks.py`. The `test_patch` is a deleted-file diff against `base_commit`, so it does not apply after a mask hunk edits that file. The mask applies forward (`mask_patch_forward`, `benchmark/external/featurebench.py:118`).
- `repo_settings.docker_specs.run_args.cuda_visible_devices` is not a GPU signal, because CPU-only Pareto-12 rows set it. The curated CPU candidate list and an admission round trip on a CPU host keep GPU tasks out.

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

_Source: PR #312 session archive (closed unmerged; work lands as #313-#319); gepa and data gotchas from specs at `e42d451`; code verified at the layer 6 head of PR #319 · Updated: 2026-10-13_
