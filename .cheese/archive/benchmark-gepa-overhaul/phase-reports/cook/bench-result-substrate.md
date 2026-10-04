status: ok
next: press
artifact: /root/.local/share/cheese/paulnsorensen-tilth/specs/bench-result-substrate.md
taste_test: deferred-to-orchestrator
durable_flags: benchmark result store + run-key + spend/auth/quota controls -> .hallouminate/wiki/benchmark-harness-gotchas.md
baseline: .cheese/cook/bench-result-substrate-baseline.yaml
cook added run keys, trajectory sidecars, a reusable result store, spend/auth/quota controls, and an informational power readout to the benchmark harness

## Cook Report — bench-result-substrate

Spec: /root/.local/share/cheese/paulnsorensen-tilth/specs/bench-result-substrate.md
(sha256 d03b8e64c55ce28bb6fc6e199df2c09297d54a42421bb655db475a27172cb17a at dispatch; user override "Override and cook now" recorded in the spec's gates_overridden).

### Contract
- Behaviour: every benchmark row carries a run key and key-input fields; claude/codex cells write an untruncated trajectory sidecar; a result store answers completed cells without model calls; paid runs require `--max-usd`, stop before crossing it, and count failed cells; baseline drift refuses without `--refreeze-baselines`; runners set `DISABLE_AUTOUPDATER=1`; Claude cells refuse `ANTHROPIC_API_KEY`; a usage-limit rejection marks `infra: quota` and stops; the analyze power readout is informational.
- Non-goals: external task adapters, judge, evolution loop; src/**, prompts/**, AGENTS.md, Cargo.toml, npm/package.json; cell-loop concurrency; per-cell `--max-budget-usd` semantics; version bump; recording a real OAuth stream.
- Quality gates: `cd benchmark && python3 -m pytest tests`; `python3 -m pytest benchmark/tests`; `python3 benchmark/check_stats.py`; pyflakes on touched files; `python3 scripts/verify.py` (just check).
- Shape check on build_runner_env, _run_single_in_repo, parse_stream_json, _power_readout:
  signature(s): unchanged public signatures; new public cell_identity, estimate_cell_cost, guard_claude_auth, write_trajectory, baselines.*
  callers: build_runner_env 1 (run.py); parse_stream_json 1 (run.py); _power_readout 1 (analyze.py) + check_stats.py; run.get_repo_path imported by check_task.py
  blast radius: benchmark harness only (no Rust, no prompts)
  slice: benchmark
  crust delta: new row fields, new run.py flags, new benchmark/baselines.py (all named by the spec)
  verdict: medium

### Files changed
- benchmark/baselines.py: new — digests, run_key, completed-row rule, store/lookup, baseline drift naming.
- benchmark/run.py: cell identity, sidecar, auth guard, DISABLE_AUTOUPDATER, quota stop, store reuse, spend ledger, drift refusal, new flags.
- benchmark/parse.py: cost_source (native vs pricing fallback), stream_native_cost, stream_native_success, detect_quota_rejection, extract_trajectory.
- benchmark/analyze.py: power readout labelled informational; "grow TASK pool" verdict dropped.
- benchmark/PHASE4_PLAN.md: superseded marker naming benchmark-gepa-overhaul.
- benchmark/README.md: documents store/spend/auth/quota; examples gain `--max-usd`.
- benchmark/tests/fixtures/streams/*.jsonl: synthetic native-cost, no-cost, and quota-rejection streams.
- benchmark/tests/conftest.py: hermetic version probes (autouse) and the `bench` run.main harness.
- benchmark/tests/test_baselines.py, test_analyze.py: new; test_parse.py, test_run_hardening.py: extended.
- benchmark/tests/test_run_hardening.py: existing main-driven tests gain `--max-usd 100` (paid runs now require it); the ambient-env allowlist test drops ANTHROPIC_API_KEY for the claude lane (now refused; covered by test_build_runner_env_refuses_api_key_for_claude) and expects DISABLE_AUTOUPDATER.
- benchmark/tests/test_gin_render_context.py: collateral repair: add repo root to sys.path so the spec gate (`cd benchmark && pytest tests`) collects it.

### Tests
- cd benchmark && python3 -m pytest tests: pass (257 passed, 1 skipped)
- python3 -m pytest benchmark/tests: pass
- python3 benchmark/check_stats.py: pass
- pyflakes on touched Python files: clean

### Risks
- Quota classifier matches the documented rate_limit_event (status rejected) and "You've hit your … limit" text, not a captured real rejection [speculating]; classification only fires when the cell did not succeed.
- env_fingerprint hashes toolchain versions plus the task repo's top-level lockfiles; "lockfile" is interpreted per task repo [speculating on intent].
- run_key also includes the task name (the spec's list omits it) so two tasks with identical content never share rows [certain].
- Task digest does not include task-module grading code (check_correctness overrides); a grading-code edit without a prompt/ground-truth/fixture change keeps the key [certain].
- Cost estimate mean is matched on task + arm + model id (spec says task and arm) [certain].
- `--cell-estimate-usd` defaults to `--max-budget-usd` (spec gives no default) [certain].
- git_sha/binary_sha256 are top-level only on tilth arms (also still under `variant`) [certain].
- CLAUDE.md benchmark examples (outside scope) lack `--max-usd` and will be refused as paid runs [certain].
- Plan approval: Cook's prepare step returned needs-planning; no literal plan-approval reply was obtainable from this sub-agent, so the single-curd plan ran under the user's relayed override [certain].

### Baseline
- benchmark/tests/test_gin_render_context.py collection: ModuleNotFoundError under `cd benchmark` — resolved by collateral repair.

### Self-eval
- [x] A failing test existed before production changes (RED captured for all six ACs).
- [x] Cook made tests pass without speculative behavior.
- [ ] Taste-test passed — fresh-context reviewer not dispatchable from this agent; inline self-check only (deferred-to-orchestrator).
- [x] Quality gates pass.

### Next step
- /press bench-result-substrate --auto
