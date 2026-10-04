status: ok
next: age
artifact: independent /age review of c3/c4/c5 (pasted findings)
Applied B1, S1-S6, N1, N3-N7 in benchmark/**. N2 and N8 are deferred.

### Applied

- B1 (blocking): `evolve/rust.py` `blank_byte_lock` + `evolve/materialize.py` `_line_changes`. Only the three byte-lock literal spans are blanked in seed and candidate. The rest of each line, and the rest of the test module, must be byte-identical. Tests: `test_byte_lock_line_code_edits_are_refused[return|let-underscore|or-true]` and `test_byte_lock_count_and_lead_literal_edits_are_rewritten`. All three probes failed (were committed) before the fix.
- S1: `run.CandidateBuildFailed` is raised by `run_plan` for any build failure (RuntimeError, OSError, or SubprocessError). The cascade records it as stage `build`, scores 0, and keeps the tail. Tests: `test_failed_candidate_build_is_a_cascade_stage` (real gepa, run completes, finish logged), `test_build_failure_side_info_names_stage`, and `test_run_plan_reports_any_build_failure_as_candidate_build_failed`.
- S2: preflight calls `run.check_baseline_drift` on the test-split baseline cells before any paid call, unless `--refreeze-baselines` is set. Finish turns BaselineDrift, a stop, a build failure, or a push refusal into a `finish: incomplete|refused` line with no PR, and `run()` returns 1. Tests: `test_test_split_baseline_drift_refuses_before_paid_calls` and `test_finish_baseline_drift_is_incomplete_not_traceback`.
- S3: `Materializer._refuse_harness_reach` checks the lines that the cumulative src diff adds. It rejects an `include*!` whose path is outside src/ or prompts/ (resolved with realpath), a path built with concat!/env!, and a path that is not a literal. It also rejects harness terms: `benchmark`, `.cheese`, `tilth_bench`, `TILTH_BENCH_DATA`, the panel path and basename, and the data dir. Tests: `test_rejects_include_outside_src_and_prompts[7]`, `test_includes_inside_src_and_prompts_are_kept`, and `test_rejects_harness_terms_in_src[8]`.
- S4: any cfg/cfg_attr attribute whose masked predicate contains the token `test` opens a guarded span, and the attribute line is part of that span. An inner `#![cfg(..test..)]` guards the whole file. Tests: `test_any_cfg_predicate_naming_test_marks_test_code` and `test_test_cfg_attributes_and_items_are_refused[4]` (one case uses the src/util.rs:96 shape).
- S5: the real-gepa stop tests now run with max_metric_calls=2000 and assert that no evaluator call happens after the stop is set. To make that hold, the dispatcher returns `{}` once the stop is set, so gepa evaluates no child.
- S6: `test_rerun_dominated_candidate_not_accepted`.
- N1: finish logs the reason of the stop that hit it, so it logs `finish: incomplete (ceiling)` even after a plateau stop. Test: `test_finish_ceiling_after_plateau_logs_ceiling`.
- N3: the dispatcher adds `{stage, tail}` to every component's records for an apply, just check, or build failure. Test: `test_dispatcher_passes_failure_tail_to_every_component`.
- N4: `run_plan` re-probes the CLI version before each paid cell and raises `PlanStopped("cli-version")`. `cli-version` was added to STOP_REASONS. Tests: `test_run_plan_reprobes_cli_before_each_paid_cell[2]` and `test_cli_change_mid_search_stops_the_run`.
- N5: Glob patterns are normpath'd against the export (and against `path`) before the check. Test: `test_proposer_glob_climbing_after_a_wildcard_is_rejected[3]`.
- N6: push uses `--force-with-lease=refs/heads/<branch>:`, which fails when the branch exists. A GitError becomes a logged refusal and `run()` exits 1. Test: `test_existing_winner_branch_is_a_logged_refusal`.
- N7: `test_reused_row_is_restamped_for_this_panel` and `test_dev_delta_never_buys_baseline_cells`.

### Deferred

- N2: the reserve over-counts baseline cells. It stays because it is conservative and only makes the reserve larger.
- N8: Panel can be constructed directly. This is a convention; the loaders are the documented entry point.

### Checks

- `cd benchmark && python3 -m pytest tests -q`: 768 passed, 1 skipped (baseline 726 passed, 1 skipped).
- pyflakes is clean on every touched .py file.
- `python3 scripts/verify.py`: exit 0.
- Mutation checks were run in a scratch copy: removing `stop_callbacks` fails both real-gepa stop tests, `if False:` on the re-run dominance check fails S6, `store_only=False` in `_dev_delta` fails N7b, and putting the stamp first in the reused-row merge fails N7a. Every other fix was red before it was applied.

### Re-review

/age bench-evolution-hardening --scope benchmark/evolve --scope benchmark/run.py --scope benchmark/tests
