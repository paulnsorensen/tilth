# Benchmark GEPA overhaul — session archive

Everything this effort produced outside the source tree, kept here because the cloud container that produced it is ephemeral.
The implementation itself lives in `benchmark/`, `src/mcp/tools/definitions.rs`, and `prompts/tools/`; the accepted specs also live in `.cheese/specs/`.

Reading anything under `.cheese/` from a benchmark cell counts as contamination (`harness_notes`), because review notes quote local-task ground-truth identifiers.

## Layout

| Path | What it holds |
| --- | --- |
| `corpus/` | The easy-cheese durable corpus for this project (`~/.local/share/cheese/paulnsorensen-tilth/`). |
| `corpus/specs/` | Every spec revision: current `*.md`, `*.ledger.json`, every fresh-context fork-coherence verdict (`*.verdict-rN.json`), and pre-fold drafts (`*.rN*.md.bak`). |
| `corpus/research/` | The subscription-auth and native-harness research behind decisions F-1 and F-6. |
| `corpus/handoffs/`, `corpus/work/` | Wheypoint handoff records and per-curd work records. |
| `phase-reports/` | Full cook, press, age, and cure reports for c1 and c5, plus `.cheese/notes/benchmark-gepa-overhaul.md`. |
| `evidence/phase-outcomes.md` | Every curd's phase outcomes (commits, tests, press and age findings, seam deviations, risks), including the c0, c2, c3, and c4 reports whose full bodies were lost with their worktrees. |
| `evidence/` | Model-free admission results: preflight logs before and after the tampered-rule fix, cached admission verdicts (counts only), the COSPA Pareto-12 config, and the c2 follow-up finding list. |

To resume with easy-cheese, copy the corpus back: `cp -r corpus/. ~/.local/share/cheese/paulnsorensen-tilth/`, then `/cheese --continue benchmark-gepa-overhaul`.

## Curds

| Curd | Spec | State |
| --- | --- | --- |
| c0 | `bench-prompt-files` | Landed: `tilth_search`/`tilth_write` descriptions in `prompts/tools/*.md`, byte-identical `tools/list`. |
| c1 | `bench-result-substrate` | Landed with two independent reviews and three cure rounds: run keys, untruncated sidecars, result store, drift refusal, `--max-usd`, auth guard, quota stop. |
| c2 | `bench-external-tasks` | Landed plus follow-up: native FeatureBench and SWE-bench Multilingual, single-hunk tampered rule, isolated grading venv, test-infra restore, contamination scan. |
| c3 | `bench-panel-manifest` | Landed: `gepa-v1` panel, stratified split, split lock, stamping, idempotent registration. |
| c4 | `bench-model-judge` | Landed: pinned `claude-sonnet-5` judge, categorical labels, stripped-record critiques, calibration gate. Calibration labels are the maintainer's to write. |
| c5 | `bench-evolution-loop` | Landed: GEPA `optimize_anything` loop over tilth commits, content-hash candidates, stop callbacks, finish reserve, accepted frontier, draft-PR finish. All paid calls stubbed in tests. |

Each spec exhausted its three fork-coherence rounds on narrowing findings, all folded in, and was accepted with a recorded override under the maintainer's policy (see each spec's `gates_overridden`).
The Mold override gap is tracked in paulnsorensen/easy-cheese#745.

## Admission on the cloud host

The real, model-free preflight against the pinned datasets (FeatureBench `v1.1`, SWE-bench Multilingual `846e647b9f33c0b51b739d005d13d85493c9af09`) admitted 5 of 7 FeatureBench Lite Level 1 candidates (sphinx, metaflow, seaborn ×2, sympy), plus `gin-gonic__gin-3741` and `sharkdp__bat-2650`.
pydantic (gold 4/6) and xarray (pytest-mypy-plugins option mismatch) were refused as `gold_unresolved`.

## Deliberately not archived

- Dataset rows, parquet files, and `$TILTH_BENCH_DATA`: they hold gold patches and held-out tests, which must never enter the checkout. Regenerate with `python3 benchmark/external/preflight.py --panel benchmark/panels/gepa-v1.json`, which fetches the pinned rows.
- Scratch copies of source files and build caches.

## Open follow-ups

- `bench-oauth-fixture`: record a scrubbed real `claude -p` OAuth stream on the maintainer's machine and commit it as a parser fixture.
- `bench-gpu-host`, `bench-heavy-python-envs`, `bench-hgm-parent-selection`, `fix-tilth-deps-route`: see the umbrella spec's Deferred follow-ups.
- c1 deferred nits: pre-runner errors are charged the estimate, timeouts are undercharged, per-cell work repeats, and the result store is local-only.
- c5 deferred: `run_plan` duplicates `run.main`'s cell loop; prompt size is unbounded; reflection gets no build-failure feedback.
- c2 residual: a symlinked test directory could let config restore write through the link.
- Before the first paid run: hand-label `benchmark/judge/calibration.json`, run preflight on the paid host, and expect one `--refreeze-baselines` if an older store exists.
