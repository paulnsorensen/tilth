# FeatureBench integration

Main already runs FeatureBench Lite Level 1 natively. The harness replays tests on the host with no container runtime and no upstream CLI.

**Status: implemented in `benchmark/external` (PR #315). This page records no paid run.**
Decisions, task picks, and panel membership live in the [ADR: benchmark GEPA overhaul](adr/benchmark-gepa-overhaul.md).
This page does not restate them.
No gate stands in front of task growth, and every paid run needs `--max-usd`.

## What runs

The runner grades FeatureBench Lite Level 1 instances on a local checkout.
It prepares a masked tree, builds a private `uv` venv, and replays the FAIL_TO_PASS and PASS_TO_PASS tests itself.
The code is in `benchmark/external/featurebench.py`, at `1cbebb313dccce0166d8cfc82856dbe5a7c737a5`.

- **Level 1 only.** Admission refuses a Level 2 ID as `level2` and an unknown suffix as `unclassified` (`benchmark/external/preflight.py:205`).
- **Candidate list.** The COSPA Pareto-12 IDs that end in `.lv1`, minus the five heavy-environment repositories (`featurebench.py:22-40,57-60`).
- **Mask.** The row `patch` applies forward to `base_commit` and removes the feature. Prepare applies only the hunks for files outside FAIL_TO_PASS, then `test_patch`. The gold patch is the reverse of those hunks (`featurebench.py:1-8,125-133`).
- **Grading.** Grading restores every FAIL_TO_PASS and PASS_TO_PASS file from `base_commit`. It runs pytest with `junit_family=xunit1` and aggregates results per file. A file passes when pytest collects at least one of its tests and all of them pass (`featurebench.py:99-113,159-183`).
- **Report integrity.** The JUnit report is a file that pytest writes outside the checkout. Test output that the agent prints cannot add a result (`featurebench.py:63-70`).
- **Selection.** `run.py --tasks <instance_id>` resolves an admitted external task. `--tasks all` stays local-only.

## Pinned data revision

The dataset is the Hugging Face repository `LiberCoders/FeatureBench`, file `data/lite-00000-of-00001.parquet`.
The harness pins commit `76b4a4566e04f4bcc13c35125d4f301791efa736`, which the tag `v1.1` pointed to on 2026-10-13 (`benchmark/external/data.py:16`).
A pinned sha256 checks the parquet file, and a download that does not match is refused (`data.py:25-28`).
A mutable tag never decides the revision. Loading a task cached at another revision raises (`featurebench.py:186-188`).

Dataset rows hold gold patches and held-out tests, so they stay in `$TILTH_BENCH_DATA`, outside the checkout.
The default is `~/.local/share/tilth-bench/external` (`data.py:30,49-51`).

## Native admission round trip

A task enters a panel only after a round trip on the host that will run it.
The round trip is model-free (`preflight.py:154-191`).

1. Prepare the masked tree and build the grading venv. Failure gives `prepare_failed` or `env_build_failed`.
2. Grade the gold patch. It must resolve; otherwise the verdict is `gold_unresolved`.
3. Grade the empty patch. It must not resolve; otherwise the verdict is `empty_resolved`.
4. Grade the gold patch minus one hunk at a time, largest hunk first, up to `$TILTH_BENCH_TAMPER_MAX_HUNKS` hunks (default 8). Some removal must fail the held-out tests; otherwise the verdict is `tampered_resolved`. With one source hunk, the variant is that hunk minus its added lines. When that hunk only adds lines, the verdict records `tampered: n/a` and is admitted.

Verdicts cache under the data directory.
The cache key joins the host environment fingerprint and a digest of the grading code, so a toolchain or grader change recomputes the verdict (`preflight.py:76-78`).
To regenerate admission for the committed panel, run `python3 benchmark/external/preflight.py --panel benchmark/panels/gepa-v1.json`.
The ADR records the earlier cloud-host admission result and its two refusals.

## Protection and limits

Cells get no filesystem or network isolation.
A contamination scan of tool-call inputs replaces isolation, as the ADR records.
Grading reads the JUnit report, not stdout.
The [invariants page](benchmark-gepa-invariants.md) holds the guards and their code citations.
The [gotchas page](benchmark-gepa-gotchas.md) records the data traps, including a FAIL_TO_PASS file that the mask edit touches.

Not covered:

- GPU tasks and Level 2 tasks.
- Heavy-environment repositories (pandas, astropy, transformers, Lightning, mlflow) until a native recipe exists.
- A leaderboard-comparable score. A subset result keeps its subset name and never reads as the full Lite score.

## Reporting rules

- Keep every planned attempt in the denominator, including infrastructure failures, timeouts, and absent patches.
- Report fully resolved tasks apart from test-pass fractions.
- Do not report best-of-attempt summaries as pass@1.
- Keep credentials out of run metadata and reports.
- Record the dataset revision, the Tilth binary identity, and the resolved model ID with each row.

A 12-task panel tests plumbing and large effects.
It does not give confirmatory proof.
See [cost planning](benchmark-cost-planning.md) and [statistical limits](benchmark-selection-and-rigor.md#what-a-small-panel-can-tell-us).
Historical attachment and billing failures are in [harness gotchas](benchmark-harness-gotchas.md).

## Rejected alternatives

These routes are rejected non-goals. Do not build them or propose them again.

- **Harbor.** Its FeatureBench verifier shares the agent container, which leaves the gold patch readable.
- **Docker.** Any container runtime step is an error in this harness.
- **`fb infer` and `fb eval`.** The upstream CLI needs an API key for Codex and has no MCP hook.

The reasons and the maintainer decision are in the [ADR: benchmark GEPA overhaul](adr/benchmark-gepa-overhaul.md).
The [source register](sources/benchmark-research.md#featurebench-harnesses) keeps the upstream documentation links as research evidence only.

_Source: `benchmark/external` at 1cbebb313dccce0166d8cfc82856dbe5a7c737a5; ADR-001, ADR-006, ADR-007 · Updated: 2026-10-15 · Supersedes: the 2026-09-29 external-runner proposal._