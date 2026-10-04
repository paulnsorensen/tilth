status: ok
next: age
artifact: .cheese/age/bench-result-substrate.md
cure applied review findings 1-5 plus nits on 8efe6bd in 49eb7eb; none deferred

### Applied

Commit 49eb7eb fixes the independent /age review of 8efe6bd. Every fix has a test that failed before the fix, or that a mutation check shows is load-bearing.

- **1 (blocking)**: a stream with no result event and a rejected rate_limit_event now classifies as quota. When a result event exists, the final result still decides. Evidence: `test_parse.py::test_rejected_rate_limit_event_without_a_result_is_quota` and `test_run_hardening.py::test_rejected_rate_limit_event_without_a_result_stops_run` both failed before the fix (`parse.py` `detect_quota_rejection`).
- **2**: `BASELINE_SLOT_FIELDS` drops `harness_digest` and adds `bare`, `strict_file_tools`, and `max_budget_usd`. `cell_identity` now records these flags on every row. Evidence: the harness-change refusal test, the variant tests, `test_stock_arms_with_different_modes_are_separate_slots`, and a mutation that removes `mode` (three tests fail).
- **3**: the strict `--settings` hook in the hashed template now uses `<python> <bash-guard>`. Evidence: `test_strict_key_ignores_interpreter_and_checkout_paths` failed before the fix.
- **4**: the guard test now edits the guard's content in place. A mutation that removes `bash_guard_sha256` makes it fail. New tests cover the scheduler's `fresh=True` probe and the cache bypass in `cli_version(fresh=True)`, and the matching mutations make them fail.
- **5**: a reporting error after the cell settles now sets `stop_reason`, and the run stops normally with a nonzero exit. `settle` no longer records. The success path records inside a guarded `report()`.
- **Nits**: the drift key now includes mode. Reused rows set `charged_usd` to 0.0. A failed CLI probe has its own stop reason. The tilth version is cached per mode. `tolerant_jsonl` moved to `benchmark/jsonl.py`. The `command` parameter is now typed `Sequence[str]`.

### Deferred

None.

### Checks

- `cd benchmark && python3 -m pytest tests -q`: 334 passed, 1 skipped. The baseline before this cure was 318 passed.
- `pyflakes` reports nothing on the touched files.
- `python3 scripts/verify.py` exited 0.

### Re-review

Residual risks:

- **certain**: stored rows written before this change have no `bare` field, so they never fill a slot. Their stock arms are re-run instead of being refused as drift.
- **certain**: no fresh-context taste test ran. This was a nested coder, so the orchestrator owns that review.

Next: `/age bench-result-substrate --scope benchmark/run.py --scope benchmark/baselines.py --scope benchmark/parse.py --scope benchmark/jsonl.py`
