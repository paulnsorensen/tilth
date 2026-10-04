status: ok
next: age
artifact: /tmp/claude-0/-home-user-tilth/8d35f234-3a3c-4d10-9272-9383e74c8185/scratchpad/findings.md
applied 4 follow-up findings on benchmark/external (tamper rule, pytest infra restore, grading venv, package digest in run key)

### Applied
- F1 tampered rule: `patches.hunk_removals` + `preflight._tamper` try single-hunk removals largest-first (bound `$TILTH_BENCH_TAMPER_MAX_HUNKS`, default 8); admitted when any removal fails; `tampered_hunk` + detail name the hunk; `VERDICT_VERSION=2` and bound mismatch recompute cached verdicts. Tests: test_tampered_check_admits_when_another_hunk_is_exercised, test_tampered_check_is_bounded_by_max_hunks, rejects_non_discriminating[tampered_resolved] (redundant two-hunk fix), stale_schema params, test_patch_helpers_split_reverse_and_remove_hunks.
- F2 pytest infra: `ExternalTask.restore_test_config` resets conftest.py / pytest.ini / .pytest.ini / pytest.toml / tox.ini / setup.cfg / [tool.pytest pyproject.toml to base_commit (python only). Test: test_agent_test_infrastructure_does_not_change_grade (7 cases).
- F3 grading venv: `ExternalTask.grade_venv` builds once per (instance, env fingerprint) under `$TILTH_BENCH_DATA/envs/` from the prepared tree; `grading_env` puts checkout (+src) on PYTHONPATH. Test: test_agent_venv_cannot_change_grade.
- F4 run key: `identity_inputs()["external_package"] = package_digest()`. Test: test_external_helper_edit_changes_only_external_run_key (mutation-checked).
### Deferred
- none
### Checks
- benchmark pytest: 463 passed, 1 skipped; pyflakes clean; scripts/verify.py exit 0.
- Real FB preflight (scratch data): 5/7 admitted; pydantic + xarray gold_unresolved for env reasons that predate this change.
### Re-review
- /age bench-external-followups --scope benchmark/external --scope benchmark/tests --scope benchmark/README.md
