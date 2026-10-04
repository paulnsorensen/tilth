status: ok-with-concerns: quota classifier unverified against a real stream; CLAUDE.md examples lack --max-usd
next: age
artifact: .cheese/cook/bench-result-substrate.md
durable_flags: benchmark result store + run-key + spend/auth/quota controls -> .hallouminate/wiki/benchmark-harness-gotchas.md
baseline: .cheese/cook/bench-result-substrate-baseline.yaml
press attacked spend, reuse, resume, quota, sidecar, and row-field contracts; all green on attempt 1

## Press Report — bench-result-substrate

Readiness: follow-up recommended (GREEN on attempt 1; out-of-contract concerns recorded for Age).

## Attempts

| # | Outcome | Router action | Candidate | Route | Telemetry |
| --- | --- | --- | --- | --- | --- |
| 1 | green | Dispatch("/age") | .cheese/press/candidates/bench-result-substrate.attempt-1.json | .cheese/press/bench-result-substrate.attempt-1.route.json | .cheese/press/bench-result-substrate.attempt-1.telemetry.json |

## Evidence

- Attack identity: bench-result-substrate/press-1
- Test file: benchmark/tests/test_result_substrate_press.py (10 tests, all green)
- Test digest: sha256:f8368bcba9f27140ba11163203165ffab8b9828c868bfc386327f5160be517d6
- Attacks: reused rows excluded from spend; exact-ceiling admission; timeout row estimate + re-run; zero native cost on a failed cell charges 0 (not the estimate); quota stop then resume runs only unfinished cells; API key does not block a fully reused run; fresh row reproduces its own run_key; task content change invalidates reuse; codex cell sidecar untruncated with cost_source pricing; OpenCode row trajectory_path null with all row fields.
- No production paths changed during Press.

## Review follow-ups

- Quota classifier is built from documented shapes only; confirm against a real rejected `claude -p` stream (follow-up bench-oauth-fixture).
- CLAUDE.md benchmark examples (outside the cooked scope) lack `--max-usd`; they now hit the paid-run refusal.
- Taste-test was an inline self-check only (no fresh-context reviewer available to this agent); Age should apply full fresh scrutiny.
