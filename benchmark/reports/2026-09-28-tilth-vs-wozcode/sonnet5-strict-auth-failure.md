# Invalid strict schedule: Claude authentication

Results: benchmark/results/benchmark_20260928_201559_sonnet5.jsonl.
The schedule has 15 rows: one passing tilth cell and 14 Claude authentication errors.
The baseline fails after inference starts; later cells fail before meaningful task work.
The provider reports HTTP 401 and a revoked OAuth access token.
Do not count these as task failures or use this schedule for an arm comparison.
All raw streams remain available. No rows are deleted.

The launch wrapper copied one access token from the current macOS Claude login into the child environment.
It did not follow credential refreshes during the schedule.
The current keychain credential later has a new validity window ending 2026-09-29 11:25:06 UTC.
This is an authentication setup failure, not evidence about file tools.

The lone passing tilth cell is diagnostic only: 399.437 agent seconds, 3684202 processed input tokens, 40445 output tokens, and $1.5781832 native cost.
Its tool mix is 3 list, 9 read, 7 search, 8 write, 2 diff, and 21 Bash calls.
Seven Bash requests are denied. It uses no native file tools.

## Retry

The fresh schedule requires at least four hours of credential validity before starting.
It launches with 7.95 hours remaining and aborts on any new authentication error.
It keeps the same task, model, high effort, strict tool policy, five repetitions, 600-second timeout, $10 cap, and shuffle seed.
Results: benchmark/results/benchmark_20260928_202801_sonnet5.jsonl.
Streams: benchmark/results/streams/20260928_202801/.
Log: .context/sonnet5-strict-auth-retry-run.log.

For future unattended benchmarks, prefer a dedicated `claude setup-token` credential over a copied interactive access token.
Official guidance: https://code.claude.com/docs/en/authentication#generate-a-long-lived-token.
