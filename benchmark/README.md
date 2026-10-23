# tilth Benchmark

Automated evaluation of tilth's impact on AI agent code navigation.

## Codex headless benchmark

Use `--runner codex` to exclude Claude and OpenCode model calls.
It defaults to `gpt5` (`gpt-5.6-sol`) and the `baseline,tilth` arms.
`--models all` selects only Codex models.
The runner rejects other providers and `tilth_forced` before any model call.
Codex cannot enforce the forced arm's tool restrictions.

Install and authenticate Codex CLI 0.154.0 (`codex login`).
The [Codex non-interactive guide](https://learn.chatgpt.com/docs/non-interactive-mode) describes headless execution.
The runner explicitly disables discovered host and project skills through Codex skill configuration.
Install Python 3.12+ and a tilth binary.
For pinned repository tasks, run `python benchmark/fixtures/setup_repos.py`.
For synthetic tasks, run `python benchmark/fixtures/setup.py`.
Set `TILTH_BIN` to select a specific binary.
The runner preserves saved Codex authentication and `CODEX_HOME`.
It ignores user config and rules and disables skill search, plugins, hooks, apps, and multi-agent support.
It preserves `HOME`, `CODEX_HOME`, and saved authentication.
It sets the disposable project to untrusted and omits project instructions.
Both arms get the same task and guidance to batch independent reads.
The tilth arm also gets explicit tilth-first guidance for source work. Native tools remain available.
Each tilth arm configures only its selected MCP binary and requires at least one successful tilth MCP call.
The Codex tilth arm approves only `tilth_write` through invocation-only tool configuration.
Benchmark tasks target disposable repository copies. Native shell commands retain the `workspace-write` sandbox.
This MCP approval does not confine tilth access to those copies or restrict absolute paths.
This setting does not change global approvals, other MCP tools, or the untrusted project policy.
A host-skill access attempt or tilth arm without successful use invalidates the cell and stops the schedule.

Run a bounded synthetic check without Claude calls:

```bash
python benchmark/run.py --runner codex --tasks find_definition \
  --models gpt5 --modes baseline,tilth --reps 1 --max-cells 2 --max-usd 2
```

Run the curated, edit-only Luna 5.6 comparison at xhigh after setup and authentication:

```bash
python benchmark/run.py --runner codex --models luna56 \
  --reasoning-effort xhigh \
  --tasks gin_edit_render_cascade,gin_edit_render_runtime \
  --modes baseline,tilth --reps 3 --max-cells 12 --max-usd 40
```

This schedule has two tasks and 12 cells. Both tasks require cross-file repairs and use test-based grading.
The selection targets batched file access: a shared-helper contract repair and three independent renderer repairs.
It excludes read-only tasks, single-file repairs, and the invalid Express multi-mutation task.
Select tasks before inspecting results. This small Gin-only suite does not measure general coding performance.
Check raw tool calls and `batch_sizes` for actual batching; one successful MCP call does not demonstrate batching.
The cell cap limits calls; `--max-usd` limits spending. The runner records the selected effort in each result row.

The output path appears in the run summary under `benchmark/results/`. Raw Codex JSONL and adjacent `.stderr` files appear in the matching `benchmark/results/streams/` directory, including failed cells. `--max-cells`, `--max-usd`, and the per-cell 600-second timeout bound the run size. Codex has no per-cell dollar cap; its cost is an estimate from token usage, which the run-wide `--max-usd` ledger counts.

## Result store, spend ceiling, and subscription guards

Every row carries a `run_key` that hashes the model, agent CLI version, effort, timeout, arm, repetition, task, a harness digest (system prompt, tool allowlist, strict-file-tools, bare, per-cell `--max-budget-usd`, MCP config shape, the runner command template with workspace, binary, and prompt paths normalized, and in strict mode the hook source key: the git tree hash of `src/hook` plus the SHA-256 of `Cargo.lock` at the hook commit), a task digest (prompt, ground truth, test command, mutations, fixture files, repo commit), an environment fingerprint (the versions of the task repo language's toolchains, with `uv` for Python, plus the repo's lockfiles, or `package.json` and `node_modules/.package-lock.json` when it has none), and, for tilth arms, the candidate `git_sha` and `binary_sha256`.
Rows also record `harness_digest`, `task_digest`, `env_fingerprint`, `cli_version`, `timeout_s`, `cost_source`, `charged_usd`, `reused`, and `trajectory_path`; a Woz Code cell also records `plugin_sha256`, a hash of the plugin tree that joins the run key.
For Claude and Codex cells, `trajectory_path` names a sidecar beside the raw stream with every tool call's full input and output; OpenCode cells record `null`.
A completed Claude row also records `resolved_model`, the model ID that the stream reports. Other rows record `null`. This field is not part of the run key.

The Claude model names `haiku`, `sonnet`, and `opus` are bare claude CLI aliases (`--model sonnet`). The installed CLI version selects the model, and the run key includes that version.
The names `haiku45`, `sonnet46`, `sonnet5`, and `opus5` select pinned model IDs. The run key hashes the model ID, so a pinned name reuses the stored rows of its ID.
Rows stored under a pinned ID do not answer a bare alias. A bare alias buys those cells again.
`--models all` skips the pinned names (`PINNED_MODELS` in `config.py`), so it does not run one model twice. Name a pinned model to run it.
`paired.py` and `analyze.py` label a row with its `resolved_model`. A bare-alias row without one takes the one ID that its alias resolved to in the same file, else it keeps the alias.
The Codex names are `gpt5`, `luna56`, and `o3`. The OpenCode name is `gpt5mini`.
When a stream reports no native cost, `pricing.yaml` prices the model ID that the stream reports. The `aliases` table is the fallback for a stream that reports no ID.

`run.py` appends every row to `benchmark/results/result_store.jsonl`.
A cell whose key matches a completed row (no `error`, no `infra`, no timeout) is answered from the store without a model call and written to the run's output with `reused: true`, this run's schedule and variant metadata, and the stored outcome.
A torn store line is skipped with a warning.
Error, timeout, and `infra: quota` rows are re-run. Re-running an interrupted command resumes it.

A run with any cell the store cannot answer is paid and requires `--max-usd`.
Spend sums native `total_cost_usd` when the stream reports it, else the `pricing.yaml` cost (`cost_source: native | pricing`).
Failed cells add their native cost, else their pre-run estimate (`cost_source: estimate`). Every paid row records the ledger amount as `charged_usd`; an estimate is never written as `total_cost_usd`, so analysis does not count it as cost.
The run stops before a cell whose estimate would cross the ceiling.
The estimate is the mean stored cost for that task, arm, and model, else this run's largest cell cost, else `--cell-estimate-usd` (default: `--max-budget-usd`).

A paid run that would re-run a stock-arm cell (an arm with no MCP server or plugin, such as `baseline` or `no_tilth`) whose stored row for the same task, model, arm, effort, timeout, repetition, and harness digest was recorded under other key inputs, such as a new CLI version or environment fingerprint, refuses to start and names the changed inputs. Pass `--refreeze-baselines` to re-run them.
The run key of a Claude cell includes a per-cell budget placeholder, so rows stored before this change need `--refreeze-baselines` to be reused.
Runner subprocesses set `DISABLE_AUTOUPDATER=1`, and the run stops before a paid cell when the agent CLI version changed since planning.
A Claude run refuses to start when `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_PROFILE`, a `CLAUDE_CODE_USE_*` provider flag, or a federation variable is set, because each outranks `CLAUDE_CODE_OAUTH_TOKEN`.
It also refuses when a settings file that a cell loads sets one of these keys under `env` or sets `apiKeyHelper`; no runner receives any `ANTHROPIC_*` variable.
Before the first paid cell, a Claude run also needs `CLAUDE_CODE_OAUTH_TOKEN` and a passing `claude auth status` preflight that reports `oauth_token` for each distinct cell environment.
A live Claude call can report an init `apiKeySource` other than `none`, report no `apiKeySource`, or send an assistant event before its init event.
The run then records the cell as `infra: auth` and stops.
Every paid run charges a persistent budget file at `$TILTH_BENCH_DATA/budgets/<budget_id>.jsonl`.
`--budget-id NAME` names that budget; the default is a digest of the planned run keys.
One run at a time holds a budget: a second run with the same budget id exits with status 75 before it changes the budget or the output file.
The lock is `$TILTH_BENCH_DATA/budgets/runs/<budget_id>.lock`, and the kernel releases it when the run exits.
`--fresh-budget` appends a reset marker, so only later charges count against `--max-usd`.
Each charge record carries an `at` timestamp (ISO 8601, UTC).
An unreadable budget line that appears during a run stops the run before the next cell.
The paid row is stored first, and the stop reason names the budget file.
A usage-limit rejection (a terminal result with usage-limit text or a rate-limit subtype) records the cell as `infra: quota` and stops the run with exit status 1, as does the spend ceiling.

## External tasks: FeatureBench and SWE-bench Multilingual

`benchmark/external/` runs FeatureBench Lite Level 1 and SWE-bench Multilingual instances natively, with no Docker.
FeatureBench comes from HF `LiberCoders/FeatureBench` (the commit that tag `v1.1` names, pinned in `external/data.py` with a parquet sha256).
SWE-bench Multilingual comes from HF `SWE-bench/SWE-bench_Multilingual` (revision `846e647b9f33c0b51b739d005d13d85493c9af09`).
The harness keeps dataset rows, bare upstream mirrors, gold patches, held-out tests, and admission verdicts only in `$TILTH_BENCH_DATA`.
The default is `~/.local/share/tilth-bench/external`, outside this checkout and every agent workdir.

```bash
pip install -r benchmark/requirements.txt   # includes pyarrow, which the preflight fetch needs
# Fetch uncached rows, then admit the FeatureBench candidates and the Go and Rust picks
python3 benchmark/external/preflight.py
# Admit exactly a panel's FeatureBench and SWE-bench Multilingual members
python3 benchmark/external/preflight.py --panel path/to/panel.json
```

Admission prepares each instance on this host and grades its gold, empty, and tampered patches.
An instance is admitted only when its gold resolves, its empty patch does not, and its tampered check fails.
The tampered check removes one gold hunk at a time, largest first.
It stops at `$TILTH_BENCH_TAMPER_MAX_HUNKS` hunks (default 8).
The check passes when any single removal fails the held-out tests, and the verdict names that hunk.
The harness caches verdicts by instance ID, data revision, and environment fingerprint.
Delete a verdict file under `$TILTH_BENCH_DATA/verdicts/` to retry it.
The harness recomputes a verdict from an older admission version or another tamper bound.
`run.py --tasks <instance_id>` runs an admitted instance.
Its workdir is the `base_commit` tree as the single commit of a fresh repository.
FeatureBench workdirs also apply the mask and delete the FAIL_TO_PASS tests.
The instance gets a `uv` venv for Python, or the host Go or Rust toolchain.
Grading copies the agent's changes onto a clean prepared tree.
It restores the held-out tests and every pytest configuration file from `base_commit`: `conftest.py`, `pytest.ini`, `tox.ini`, `setup.cfg`, and a `pyproject.toml` with `[tool.pytest`.
Then it runs FAIL_TO_PASS and PASS_TO_PASS natively.
Python tests run in a grading venv, never in the agent's `.venv`.
The harness builds that venv once per instance and environment fingerprint under `$TILTH_BENCH_DATA/envs/`.
Rows add `pass_rate`, `f2p_passed`, `f2p_total`, `p2p_passed`, and `p2p_total`.
`--tasks all` stays the local registry.

Every row records `contaminated` and `contamination_hits`. These inputs count as a hit:

- A tool input that reads the `benchmark/` tree or the harness data directory.
- A tool input that fetches the task's upstream repository or package. A `git clone`, `curl`, or `go get` command counts. A bare `github.com` import path or issue link does not.
- In an external cell, a tool input that reads a clone under `/tmp/tilth_bench/repos/`.

A row with no trajectory sidecar is `contaminated: true` with an `unscanned` hit. `correct` stays the grader verdict.

## Pre-registered panels

A panel (`benchmark/panels/<name>.json`) is a fixed task set. The cheap tier holds the three local hard tasks: `rg_search_dispatch`, `rg_trait_implementors`, and `gin_servehttp_flow`.
The panel also holds `gin_edit_render_context`, at least one FeatureBench Lite Level 1 instance, and one Go and one Rust SWE-bench Multilingual slot member. A slot member is the primary or its fallback: `gin-gonic__gin-3741` or `prometheus__prometheus-14861`, and `sharkdp__bat-2650` or `tokio-rs__tokio-6724`. A fallback names `fallback_for` and `fallback_reason`.
`panels.stratify` splits the other members into dev and test. Within each source family, members sort by language and `sha256("<split_seed>:<id>")`. The split then alternates dev, test.
A panel file may carry a top-level `"version": 2` (an integer; absent means version 1). A version 2 panel is a screened pool with no cheap tier: its members are only `dev` or `test`, a local member is any non-external task in `tasks.TASKS`, and a SWE-bench Multilingual member is a `GEPA_V2_CANDIDATES` or slot id with no `fallback_for` or `fallback_reason`. It needs at least one dev and one test member, and `panels.stratify` still fixes the split. `Panel.cheap` is empty, and `evolve` sends a candidate straight to the dev tier.

```bash
python3 benchmark/run.py --panel benchmark/panels/gepa-v1.json --panel-split dev \
  --models sonnet5 --modes baseline,tilth --reps 1 --max-usd 20
```

`panels.load_panel` is the only loader. It refuses these faults: an unknown key or family, a missing required member, and a hand-picked split. It also refuses a declared language that differs from the repo language or from `external.language_of` on the dataset row. It also refuses an external member that `external.preflight.admit` does not admit on this host.
`--panel` accepts only Claude and Codex models: other runners write no trajectory sidecar, so every panel row would be `contaminated`.
Every row of a panel run carries `panel_name`, `panel_split_digest`, and `panel_split` (not run-key inputs).
The first completed stored row for a panel name locks its split: a later split change is refused; register it under a new panel name.
`analyze.py` counts a `contaminated` panel row as incorrect, keeps it in its denominators, and tallies it per task and arm.
`benchmark/tests/fixtures/panels/gepa-v1-rows/` holds redacted rows (`instance_id`, `repo`, `base_commit`) for the committed panel's language check; full rows stay in `$TILTH_BENCH_DATA`.

## Applicability judge

`benchmark/judge/` labels each task `strong`, `weak`, or `none` for structural-tool applicability.
It also critiques stripped rollouts for reflection.
It uses the claude CLI alias `opus` (`--model opus`) through an isolated, tool-less `claude -p` on `CLAUDE_CODE_OAUTH_TOKEN`.
The alias follows the newest opus release, and cache keys and calibration records store only `opus`.
Evolve reflection and the proposer use the claude CLI alias `sonnet`.
The call has no tools, MCP servers, settings, or hooks, and the prompt goes on stdin.
A critique sees only the task prompt, allowlisted row fields, and the verbatim trajectory sidecar.
It never sees `correctness_reason`, ground truth, gold patches, or held-out tests.
Labels and critiques are cached under the gitignored `benchmark/results/judge/`.
They never enter result rows or scores.

```bash
python3 benchmark/judge/cli.py label --tasks rg_search_dispatch,gin_edit_render_context --max-usd 2 --cell-estimate-usd 0.05
python3 benchmark/judge/cli.py calibrate --max-usd 5 --cell-estimate-usd 0.05
python3 benchmark/judge/cli.py sheet --count 24
```

`sheet` makes no model call and needs no auth.
It writes `labeling-sheet.md` and `calibration-draft.json` to `.cheese/judge` (or `--out DIR`) from the stored clean rollouts, with no judge label shown, so you can fill `calibration.json` by hand.

Both spend flags are required whenever a call is uncached.
Judge calls share the `--max-usd` ceiling and pass its remaining headroom as `--max-budget-usd`.
The first usage-limit rejection stops every later judge call.
The flags accept finite positive numbers only.
`--budget-id NAME` keeps the judge spend in `$TILTH_BENCH_DATA/budgets/NAME.jsonl`; without it, no budget file is written.
Before the first uncached call, the judge runs one `claude auth status` check and refuses unless `CLAUDE_CODE_OAUTH_TOKEN` is set and the method is `oauth_token`.
A judge call whose init event reports an API key raises an error, keeps its cost on the ledger, and discards its output.
The maintainer hand-labels `benchmark/judge/calibration.json`.
Its shape is `{"tasks": {name: label}, "trajectories": [{"run_key", "verdict"}]}`, with verdict `apt`, `missed`, or `misapplied`.
It lists every task an analyzed run holds, plus at least 20 clean, completed rollouts with trajectories.
The judge is calibrated when unweighted Cohen's kappa reaches 0.6 on both labels and verdicts.
A stored agreement goes stale when the model, a prompt, the calibration file, the threshold, or the minimum trajectory count changes.
Until the judge is calibrated, critiques are withheld.
Until then, `analyze.py` prints `## Applicability (uncalibrated)` with no label table.
An external instance needs only a cached dataset row: the judge does not run admission.

## Evolution loop

`benchmark/evolve/` runs a GEPA (`gepa==0.1.4`) search whose candidates are tilth commits.
A candidate holds the four `prompts/` files that `src/` embeds, plus a cumulative `src/**` patch on `--seed-sha`.
Each candidate becomes one local commit on the seed (ref `evolve/<run-id>/<content-id>`).
The commit passes `just check`, then the hook gate (strict runs), then a cheap tier, then the panel's dev split.
That `just check` sets `TILTH_EVOLVE_CANDIDATE=1`, so the Rust tests skip their phrase pins on `prompts/` text; CI does not set it, so the winner PR must re-pin them before it lands.
The MCP host truncates the instructions field and each tool description past 2048 UTF-8 bytes. Reflection is told this cap and the current size, and a candidate prompt file over it is refused before its worktree and `just check`.
The run log names the stage at which each candidate failed (apply, `just check`, build, hook gate, or cheap tier), with the output tail.
Candidates run in the `tilth_forced` arm (tilth MCP only, no built-in tools) and are compared with the `baseline` arm, so `--model` accepts only Claude models.
The score is grader correctness only, and contaminated rollouts count as incorrect.
Reflection and the `src/` proposer are isolated `claude -p` calls.
They see only the judge's stripped records, never grader text or test-split rollouts.
The loop buys baseline cells once and answers them from the result store afterwards.
A candidate is accepted only after `--reruns` more rollouts.
The finish step picks the accepted candidate with the best dev mean (the seed wins a tie), then scores only that candidate and the seed once on the test split.
The candidate wins only if its test mean beats the seed's, and its test delta is the one reported.

```bash
python3 benchmark/evolve/cli.py --panel benchmark/panels/gepa-v1.json --seed-sha "$(git rev-parse HEAD)" \
  --base-branch main --max-usd 200 --max-metric-calls 60 --cell-estimate-usd 0.5
```

`--max-usd` is required.
It bounds every cell, judge, reflection, and proposer call.
`--budget-id NAME` keeps the spend in `$TILTH_BENCH_DATA/budgets/NAME.jsonl`, so a rerun starts from the recorded spend.
Without the flag, the evolve ledger lives in the process only.
`--seed-sha` must be an ancestor of `origin/<--base-branch>`, the branch that the winner PR targets.
When that remote-tracking ref does not exist, the run checks the local branch.
`--cell-budget-usd` sets the Claude `--max-budget-usd` of every cell in the run: seed, candidate, baseline, and finish cells (default: `1.0`, the `run.py` default).
The value must be a positive finite number; the run refuses any other value before a paid call.
The per-cell budget is part of each cell's run key, so a run at one budget does not reuse rows bought at another budget, and it buys its baselines again.
A stronger model, such as `sonnet`, costs more per token, so give it a larger budget than `haiku`: at `$1` many external cells stop with `error_max_budget_usd`.
`--cell-estimate-usd` defaults to `--cell-budget-usd`, so the finish reserve and a cell with no stored cost are estimated at the full per-cell budget.
Evolve runs strict by default (`--no-strict-file-tools` opts out); see [evolve/README.md](evolve/README.md) for the seed-hook preflight and the hook gate.
A panel row from `run.py --panel` has the same key only when that run passed `--bare` and the same `--max-budget-usd`, passed `--strict-file-tools` with its hook commit equal to the evolve `--seed-sha`, and did not pass `--reasoning-effort`.
The evolve run refuses before any paid call unless `CLAUDE_CODE_OAUTH_TOKEN` is set and `claude auth status` reports `oauth_token`.
A call whose init event shows an API key stops the run with the `auth` reason, and its cost stays on the ledger.
The finish step holds back the test-split cost of two finalists, the dev winner and the seed.
A non-seed winner is pushed as `evolve/<run-id>-winner` and opened as one draft PR; nothing is merged.
Run logs, delta reports, and candidate worktrees live under the gitignored `benchmark/results/evolve/<run-id>/`.

## Larger Luna edit task

`gin_edit_render_context` migrates Gin's renderer API to accept request context.
The reference changes 20 files across 90 edit sites: 165 added lines and 72 removed lines.
It includes cancellation checks, request-context dispatch, a private SSE adapter, and caller/test migration.
The grader injects held-out tests after inference and restores pinned regression assertions in a separate copy.
It also validates the candidate's migrated tests.
It runs full render and binding suites, relevant root tests, and all-package compilation under default and `nomsgpack` builds.
Unrelated network integration tests are outside the grade because the pinned Unix-socket test has a timing failure.

Validate the reference and negative controls before running inference:

```bash
mise exec go@1.24.7 -- python3 benchmark/tasks/gin_render_context_fixtures/preflight.py
```

Set `GOCACHE` to a writable temporary directory outside the candidate repository before running either arm.
The runner forwards it without changing the sandbox. Prewarm dependencies before measuring agent time.
Run three repetitions per arm, with no Claude calls:

```bash
TILTH_BIN="$PWD/target/release/tilth" mise exec go@1.24.7 -- \
  python benchmark/run.py --runner codex --models luna56 \
  --reasoning-effort xhigh --tasks gin_edit_render_context \
  --modes baseline,tilth --reps 3 --max-cells 6 --max-usd 20
```

This forward task retains Git metadata. Reference tests and solution files stay outside the candidate workspace.
See `benchmark/tasks/gin_render_context_fixtures/evidence.md` for reference size and grader checks.

## Results — v0.5.0

| Model | Tasks | Runs | Baseline $/correct | tilth $/correct | Change | Baseline acc | tilth acc |
|---|---|---|---|---|---|---|---|
| Sonnet 4.6 | 26 | 86 | $0.26 | $0.15 | **-44%** | 84% | 94% |
| Opus 4.6 | 26 | 25 | $0.22 | $0.14 | **-39%** | 91% | 92% |
| Haiku 4.5 | 26 | 49 | $0.12 | $0.08 | **-38%** | 54% | 73% |
| **Average** | | **160** | **$0.20** | **$0.12** | **-40%** | **76%** | **86%** |

### Why "cost per correct answer"?

Raw cost comparison treats a wrong answer as a cheap success. It isn't — you paid for a response you can't use and still need the answer. The real question is: **how much do you expect to spend before you get a correct answer?**

This is a geometric retry model. If accuracy is `p`, you need `1/p` attempts on average before one succeeds. The expected cost is:

```
expected_cost = cost_per_attempt × (1 / accuracy)
```

**Cost per correct answer** (`total_spend / correct_answers`) computes this exactly. It's mathematically equivalent to `avg_cost / accuracy_rate` — not an arbitrary penalty, but the expected cost under retry.

## Sonnet 4.6 (86 runs)

| | Baseline | tilth | Change |
|---|---|---|---|
| **Cost per correct answer** | **$0.26** | **$0.15** | **-44%** |
| Accuracy | 84% | 94% | +10pp |
| Avg cost per task | $0.23 | $0.14 | -40% |
| Avg turns | 9.0 | 6.2 | -31% |

v0.5.0 MCP instruction overhaul and scope fallback deliver -44% cost per correct answer with +10pp accuracy gain. Turn count drops 31% as models use tilth tools directly instead of falling back to built-in Grep/Read/Glob.

### Per-task results

```
Task                                       Base    Tilth   Delta  B✓  T✓  Winner
─────────────────────────────────────────────────────────────────────────────────
express_app_render                          inf   $0.17     ↓∞  0/1 2/2  TILTH (acc)
fastapi_depends_function                  $0.34   $0.08   -78%  1/1 2/2  TILTH ($)
rg_trait_implementors                     $0.29   $0.07   -75%  1/1 2/2  TILTH ($)
rg_lineiter_usage                         $0.30   $0.10   -68%  1/1 2/2  TILTH ($)
fastapi_depends_internals                 $0.31   $0.12   -60%  1/1 2/2  TILTH ($)
fastapi_depends_processing                $0.51   $0.21   -59%  1/1 2/2  TILTH ($)
find_definition                           $0.10   $0.05   -50%  1/1 2/2  TILTH ($)
gin_client_ip                             $0.38   $0.19   -50%  1/1 2/2  TILTH ($)
read_large_file                           $0.12   $0.07   -40%  1/1 2/2  TILTH ($)
express_res_send                          $0.15   $0.10   -36%  1/1 2/2  TILTH ($)
rg_walker_parallel                        $0.28   $0.18   -36%  1/1 2/2  TILTH ($)
edit_task                                 $0.09   $0.06   -30%  1/1 2/2  TILTH ($)
rg_flag_definition                        $0.11   $0.08   -26%  1/1 2/2  TILTH ($)
rg_search_dispatch                        $0.56   $0.42   -26%  1/1 2/2  TILTH ($)
codebase_navigation                       $0.18   $0.14   -25%  1/1 2/2  TILTH ($)
gin_middleware_chain                       $0.49   $0.39   -22%  1/1 2/2  TILTH ($)
fastapi_request_validation                $0.26   $0.21   -19%  1/1 2/2  TILTH ($)
fastapi_dependency_resolution             $0.45   $0.40   -12%  1/1 2/2  TILTH ($)
express_json_send                         $0.26   $0.23   -11%  1/1 2/2  TILTH ($)
─────────────────────────────────────────────────────────────────────────────────
gin_servehttp_flow                        $0.37   $0.34    -8%  1/1 2/2  ~tie
rg_lineiter_definition                    $0.11   $0.11    -3%  1/1 2/2  ~tie
express_render_chain                      $0.26   $0.25    -2%  1/1 2/2  ~tie
markdown_section                          $0.06   $0.06    +1%  1/1 2/2  ~tie
gin_radix_tree                            $0.14   $0.15    +9%  1/1 2/2  ~tie
─────────────────────────────────────────────────────────────────────────────────
express_app_init                          $0.15   $0.17   +14%  1/1 2/2  BASE ($)
gin_context_next                          $0.05   $0.10   +82%  1/1 2/2  BASE ($)
─────────────────────────────────────────────────────────────────────────────────
W19 T5 L2
```

Costs are $/correct (avg_cost / accuracy). Winner: accuracy difference > 15pp first, then >=10% cost difference.

### By language

| Repo | Language | $/correct (B → T) | Accuracy (B → T) |
|---|---|---|---|
| FastAPI | Python | $0.38 → $0.20 (-46%) | 100% → 100% |
| ripgrep | Rust | $0.28 → $0.16 (-42%) | 100% → 100% |
| Synthetic | Multi | $0.11 → $0.08 (-31%) | 100% → 100% |
| Express | JS | $0.24 → $0.18 (-23%) | 80% → 100% |
| Gin | Go | $0.29 → $0.23 (-19%) | 100% → 100% |

Rust sees the largest improvement (-42%) thanks to the read threshold bump — previously outlined files like ripgrep's `sink.rs` now return full content, eliminating multi-read spirals. All languages improve. `express_app_render` — previously unsolved by Sonnet — is solved in both tilth runs. `rg_search_dispatch` — previously intermittent — now succeeds 2/2.

## Opus 4.6 (25 runs)

| | Baseline | tilth | Change |
|---|---|---|---|
| **Cost per correct answer** | **$0.22** | **$0.14** | **-39%** |
| Accuracy | 91% | 92% | +1pp |
| Avg cost per task | $0.20 | $0.13 | -35% |
| Avg turns | 9.8 | 6.2 | -37% |

v0.5.0 delivers -39% cost per correct answer with 37% fewer turns. Opus already had high accuracy (91%); tilth maintains this while cutting costs significantly.

```
Task                                       Base    Tilth   Delta  B✓  T✓  Winner
─────────────────────────────────────────────────────────────────────────────────
fastapi_depends_internals                 $0.20   $0.09   -56%  1/1 1/1  TILTH ($)
rg_trait_implementors                     $0.16   $0.08   -50%  1/1 1/1  TILTH ($)
codebase_navigation                       $0.21   $0.11   -48%  1/1 1/1  TILTH ($)
fastapi_depends_function                  $0.11   $0.07   -41%  1/1 1/1  TILTH ($)
fastapi_depends_processing                $0.35   $0.22   -39%  1/1 1/1  TILTH ($)
find_definition                           $0.08   $0.05   -37%  1/1 1/1  TILTH ($)
rg_search_dispatch                        $0.66   $0.42   -37%  1/1 1/1  TILTH ($)
fastapi_dependency_resolution             $0.41   $0.26   -35%  1/1 1/1  TILTH ($)
gin_servehttp_flow                        $0.33   $0.22   -32%  1/1 1/1  TILTH ($)
gin_middleware_chain                      $0.33   $0.28   -16%  1/1 1/1  TILTH ($)
edit_task                                 $0.07   $0.06   -15%  1/1 1/1  TILTH ($)
markdown_section                          $0.06   $0.05   -15%  1/1 1/1  TILTH ($)
─────────────────────────────────────────────────────────────────────────────────
fastapi_request_validation                $0.19   $0.17    -8%  1/1 1/1  ~tie
express_json_send                         $0.23   $0.21    -8%  1/1 1/1  ~tie
rg_flag_definition                        $0.07   $0.06    -6%  1/1 1/1  ~tie
express_render_chain                      $0.26   $0.26    -0%  1/1 1/1  ~tie
read_large_file                             inf     inf    ---  0/1 0/1  ~tie
rg_lineiter_definition                    $0.06   $0.07    +3%  1/1 1/1  ~tie
gin_radix_tree                            $0.15   $0.15    +4%  1/1 1/1  ~tie
gin_client_ip                             $0.17   $0.18    +6%  1/1 1/1  ~tie
express_app_init                          $0.18   $0.20   +10%  1/1 1/1  ~tie
─────────────────────────────────────────────────────────────────────────────────
rg_walker_parallel                        $0.19   $0.22   +13%  1/1 1/1  BASE ($)
gin_context_next                          $0.05   $0.06   +20%  1/1 1/1  BASE ($)
express_app_render                        $0.14   $0.17   +25%  1/1 1/1  BASE ($)
rg_lineiter_usage                         $0.09   $0.12   +40%  1/1 1/1  BASE ($)
express_res_send                          $0.09   $0.12   +41%  1/1 1/1  BASE ($)
─────────────────────────────────────────────────────────────────────────────────
W12 T9 L5
```

## Haiku 4.5 (49 runs)

| | Baseline | tilth | Change |
|---|---|---|---|
| **Cost per correct answer** | **$0.12** | **$0.08** | **-38%** |
| Accuracy | 54% | 73% | +19pp |
| Avg cost per task | $0.10 | $0.06 | -40% |
| Avg turns | 10.4 | 9.7 | -7% |

v0.5.0 improves Haiku accuracy by 19pp and reduces cost per correct answer by 38%. The scope fallback fix prevents Haiku from passing invalid directory paths that caused 0-result searches and subsequent fallback to built-in tools.

## Cross-model analysis

### Tool adoption by model (tilth mode)

| Model | tilth_search/run | tilth_read/run | tilth_list/run | Host tools/run | Adoption rate |
|---|---|---|---|---|---|
| Haiku 4.5 | ~1.5 | ~4.0 | ~0.5 | ~1.0 | ~85% |
| Sonnet 4.6 | ~2.5 | ~3.0 | ~0.5 | ~0.2 | ~95% |
| Opus 4.6 | ~2.5 | ~3.0 | ~0.3 | ~0.1 | ~98% |

v0.5.0 MCP instructions with top-weighted DO NOT rules reduced host tool usage to near-zero on Sonnet and Opus. Haiku adoption improved significantly through scope fallback and instruction positioning.

### Where tilth wins

**fastapi_depends_function (-78% $/correct on Sonnet):** tilth's search results surface the function with full context and callees. Baseline takes 3x more tool calls to assemble the same picture.

**rg_trait_implementors (-75% Sonnet, -50% Opus):** Structural search finds all trait implementations efficiently. Baseline needs multiple grep/read cycles.

**fastapi_depends_internals (-60% Sonnet, -56% Opus):** tilth's callee footer resolves the dependency chain in a single search. Consistent wins across models.

**Rust overall (-42% $/correct on Sonnet):** The read threshold bump from ~3500 to ~6000 tokens eliminated multi-read spirals on mid-sized Rust files. `rg_search_dispatch` flipped from +90% regression to -26% win.

### Where tilth loses

**gin_context_next (+82% Sonnet, +20% Opus):** Simple struct field lookup. Baseline reads one file cheaply; tilth's search overhead isn't worthwhile on trivial tasks.

**express_app_render (+25% Opus):** Deep render chain tracing. Sonnet tilth solves this (2/2) where baseline can't, but Opus baseline is already efficient here.

**express_res_send (+41% Opus):** Short call chain where baseline's single Read is faster than tilth's search + read pattern.

## Methodology

Each run invokes `claude -p` (Claude Code headless mode) with a code navigation question.

**Three modes:**

- **Baseline** — Claude Code built-in tools: Read, Edit, Grep, Glob, Bash
- **tilth** — Built-in tools + tilth MCP server (hybrid mode)
- **tilth_forced** — tilth MCP only; no built-in tools
- **wozcode** — Built-in tools + Woz Code plugin and its explicit MCP server; requires `--wozcode-plugin-dir`

All selected modes use the same system prompt, model, and per-cell budget cap (default $1.00). The agent explores the codebase and returns a natural-language answer. Correctness is checked against ground-truth strings that must appear in the response.

**Grading semantics**: correctness is a case-insensitive, backtick-stripped substring match — every `required_strings` entry for a task must appear somewhere in the graded text (`benchmark/tasks/base.py`'s `check_correctness`). The graded text is the agent's text output *accumulated across every assistant turn in the run*, not just the final one — a short wrap-up turn at the end of a run adds to, rather than replaces, the substantive answer from an earlier turn (`benchmark/parse.py`'s `parse_stream_json`).

**Repos (pinned commits):**

| Repo | Language | Description |
|---|---|---|
| [Express](https://github.com/expressjs/express) | JavaScript | HTTP framework |
| [FastAPI](https://github.com/tiangolo/fastapi) | Python | Async web framework |
| [Gin](https://github.com/gin-gonic/gin) | Go | HTTP framework |
| [ripgrep](https://github.com/BurntSushi/ripgrep) | Rust | Line-oriented search |

**Difficulty tiers (7 tasks each, Sonnet only):**

- **Easy** — Single-file lookups, finding definitions, tracing short paths
- **Medium** — Cross-file tracing, understanding data flow, 2-3 hop chains
- **Hard** — Deep call chains, multi-file architecture, complex dispatch

### Running benchmarks

**Prerequisites:**

- Python 3.12+
- At least one supported agent CLI (`claude`, `codex`, or `opencode`) installed and authenticated
- Rust/Cargo and Git (for building exact tilth revisions and cloning benchmark repos)
- Analysis stats deps: `pip install -r benchmark/requirements.txt` (scipy, numpy, and pyarrow). Running the tests also needs pytest: `pip install -r benchmark/requirements-dev.txt`.

**Setup:**

```bash
# Clone repos at pinned commits (~100MB total)
python benchmark/fixtures/setup_repos.py

# Resolve mutable branch names to immutable SHAs, then build both tilth arms.
# The candidate is named "fork" in result data even when both repos are jahala/tilth.
python benchmark/prepare_experiment.py \
  --reference-repository https://github.com/jahala/tilth \
  --reference-ref main \
  --variant-repository https://github.com/jahala/tilth \
  --variant-ref <candidate-branch> \
  --output /tmp/tilth-benchmark/experiment.json
python benchmark/build_variants.py /tmp/tilth-benchmark/experiment.json

# Install the analysis stats deps used by analyze.py / paired.py / stats.py
pip install -r benchmark/requirements.txt
```

**Run:**

```bash
# Primary three-way experiment: no tilth, pinned upstream, pinned fork.
# Arm order is randomized inside each task/model/repetition block.
python benchmark/run.py \
  --experiment /tmp/tilth-benchmark/experiment.json \
  --tasks all --repos ripgrep,fastapi,gin,express \
  --models sonnet --reps 3 --max-usd 150

# Multiple model lanes use the same pinned variants and arm-order seed
python benchmark/run.py \
  --experiment /tmp/tilth-benchmark/experiment.json \
  --tasks all --models sonnet,opus --reps 3 --max-usd 400

# Routine-safe bounded run: 2 tasks × 1 model × 1 repetition × 3 arms
python benchmark/run.py \
  --experiment /tmp/tilth-benchmark/experiment.json \
  --tasks find_definition,markdown_section \
  --models haiku --reps 1 --max-cells 6 --max-usd 3

# Legacy local A/B remains available for development checks
python benchmark/run.py \
  --tasks fastapi_depends_processing,gin_middleware_chain \
  --models sonnet --reps 1 --modes baseline,tilth --max-usd 5

# Local three-way Sonnet 5 comparison: one Gin renderer task, five matched repetitions.
# Set CLAUDE_CODE_OAUTH_TOKEN outside this command. Use a dedicated CLAUDE_CONFIG_DIR;
# only its wozcode/auth.json is copied into each temporary Woz cell.
TILTH_BIN=/absolute/path/to/target/release/tilth \
CLAUDE_CONFIG_DIR=/absolute/path/to/isolated/config \
python benchmark/run.py \
  --tasks gin_edit_render_context --models sonnet5 --reasoning-effort high \
  --modes baseline,tilth,wozcode --wozcode-plugin-dir /absolute/path/to/wozcode \
  --reps 5 --max-cells 15 --max-budget-usd 10 --max-usd 150 --arm-order-seed 20260929 \
  --strict-file-tools

# Diagnostic only: remove built-in search tools and force the local tilth MCP
python benchmark/run.py --tasks all --models haiku --reps 1 --modes tilth_forced --max-usd 20
```

`--strict-file-tools` gives the native arm Read, Edit, Write, Grep, Glob, and Bash. Both MCP arms get Bash and their own MCP tools, with no native file tools. All three arms guard Bash with `tilth hook` (exec form, `onFailure: block`, so a missing binary denies). A tilth arm runs its own binary with `hook --hint tilth`; the baseline runs the binary built at the hook commit with `hook --hint native`. The hook commit is the checkout's `HEAD` (the seed hook), even with `--candidate-sha`; `run_plan(strict_file_tools=True, hook_sha=...)` takes it explicitly. A strict run needs Claude CLI >= 2.1.295 (exec-form hooks and `onFailure: block`) and refuses an older one. The hook denies shell file reads, searches, and writes; see `src/hook`. Before any paid strict cell, the run probes every distinct hook binary (each tilth arm's binary with `--hint tilth`, the hook-commit build with `--hint native`) with one allow event (`go test ./...`, exit 0) and one deny event (`cat src/lib.rs`, exit 2, with the matching hint text). A binary that fails the probe, such as one built before `tilth hook` existed, refuses the run before any spend (`parser.error` in `run.py`, `HookProbeFailed` from `run_plan`). The wozcode arm has no native file tools, but its denials use `--hint native` and name them; read its denial text with that in mind. After each cell, the audit replays every Bash call through the hook binary built at the hook commit. A call that ran and that hook denies fails the cell as `hook_loosened`, whichever arm made it, so the seed hook is the floor for a candidate hook. This controls tool use, not code run by tests or MCP servers; it is not a security sandbox. Raw streams retain denied attempts, and result rows record `strict_file_tools`, `hook_source_key`, and the count of Bash calls that the hook-commit hook denies (a call that only a candidate hook denied is not counted).

**Analyze:**

```bash
# Reject error rows, missing/extra schedule blocks, incomplete matched blocks,
# and variant metadata drift before analysis. run.py records cell failures but
# intentionally completes the schedule.
python benchmark/check_experiment.py \
  benchmark/results/benchmark_<timestamp>_<model>.jsonl \
  /tmp/tilth-benchmark/experiment.json \
  --tasks <task-a,task-b> --models <model-a,model-b> --reps <repetitions>

# Summarize a run: descriptive Wilson CIs and cost metrics, plus task-clustered
# paired accuracy bootstrap CIs. The primary comparison is fork vs upstream;
# fork vs no_tilth and upstream vs no_tilth are guardrails.
python benchmark/analyze.py benchmark/results/benchmark_<timestamp>_<model>.jsonl

# Focus one task-clustered pairwise comparison
python benchmark/paired.py benchmark/results/benchmark_<timestamp>_<model>.jsonl \
  --experiment-mode fork --baseline-mode upstream

# Offline re-grade under grader alternation (recovers historical false negatives)
python benchmark/regrade.py benchmark/results/benchmark_<timestamp>_<model>.jsonl
```

Results are written to `benchmark/results/benchmark_<timestamp>_<model>.jsonl`. Each row records the task/model/repetition cell, arm order and seed, tool identities, budget, effort, cost, tokens, correctness, and wall duration. Variant rows also record the tilth binary and Woz plugin identity. Raw Claude streams remain under `benchmark/results/streams/`. A Claude error or missing success result produces a failure row, even when its text matches the grader. Every cell runs in a fresh disposable copy or Git worktree. When `CLAUDE_CONFIG_DIR` names an explicit auth seed, each Claude cell gets a fresh config directory. Woz Code requires this seed. Legacy runs without a seed keep Claude's normal OAuth configuration.

### Task definitions

Tasks are in `benchmark/tasks/`. Each specifies `repo`, `prompt`, `ground_truth` (correctness strings), and `difficulty`.

### Contributing benchmarks

We welcome benchmark contributions — more data makes the results more reliable.

**Adding results:** Run the benchmark suite on your machine and share the `.jsonl` file in a GitHub issue or PR. Different hardware, API regions, and model versions can all affect results.

**Adding tasks:** Create a new task class in `benchmark/tasks/` following the existing pattern. Each task needs:

- `repo`: which benchmark repo to use
- `prompt`: the code navigation question
- `ground_truth`: list of strings that must appear in a correct answer
- `difficulty`: `"easy"`, `"medium"`, or `"hard"`

Good tasks have unambiguous correct answers that can be verified by string matching. Avoid tasks where the answer depends on interpretation.

## Version history

| Version | Changes | Cost/correct (Sonnet) |
|---|---|---|
| v0.2.1 | First benchmark | baseline |
| v0.3.0 | Callee footer, session dedup, multi-symbol search | -8% |
| v0.3.1 | Go same-package callees, map demotion | +12% (regression) |
| v0.3.2 | Map disabled, instruction tuning, multi-model benchmarks | **-26%** |
| v0.4.0 | def_weight ranking, basename boost, impl collector, sibling surfacing, transitive callees, faceted results, cognitive load stripping, smart truncation, symbol index, bloom filters | **-17%** (Sonnet), **-20%** (Opus) |
| v0.4.1 | Instruction tuning: "Replaces X" tool descriptions, explicit host tool naming in SERVER_INSTRUCTIONS | **-29%** (Sonnet), **-22%** (Opus) |
| v0.4.4 | Adaptive 2nd-hop impact analysis for callers search, full 26-task Opus benchmark, Haiku adoption improvements | **-31%** (Sonnet), **-17%** (Opus), **-38%** (Haiku) |
| v0.4.5 | Read threshold bump from ~3500 to ~6000 tokens | **-34%** (Sonnet), **-19%** (Opus), **-38%** (Haiku) |
| v0.5.0 | MCP instruction overhaul (top-weighted DO NOT rules, search-first guidance), scope fallback with warning, Gemini CLI install | **-44%** (Sonnet), **-39%** (Opus), **-38%** (Haiku) |

v0.4.5 focus: bumped `TOKEN_THRESHOLD` from 3500 to 6000 estimated tokens (~24KB). Files in the 14–24KB range now return full content instead of an outline, eliminating multi-read spirals where agents read the entire file via 5–7 sequential `--section` calls. Key fixes: `gin_radix_tree` flipped from +35% regression to ~tie, `rg_search_dispatch` flipped from +90% regression to -26% win (Sonnet now 100% accuracy). Sonnet achieves 100% accuracy across all 52 tilth runs.
