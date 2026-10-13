# Benchmark harness gotchas (Sonnet 5 investigation, 2026-08-08)

Four independent ways the benchmark harness silently produced invalid or
misleading numbers, found while investigating PR #196
(`jahala/tilth#196`). Full forensics in `.cheese/notes/tilth-pr196-sonnet5-audit.md`
and `.cheese/notes/tilth-sonnet5-cost-attribution.md`.

## `--safe-mode` strips ALL MCP servers, including `--mcp-config` ones

The 2026-08-07 sonnet5 run (`benchmark_20260807_075044_sonnet5.jsonl`) compared
three arms that were supposed to differ by which tilth MCP server was attached.
`--safe-mode` disabled every MCP server — including ones passed via
`--mcp-config` — so all three arms ran native-tools-only. Init messages showed
`tools=[5 native], mcp_servers=[]` in every cell; first-turn prompt size was
identical (~8,124 tokens) across arms. Every accuracy/cost delta in that run
was sampling noise (fork-upstream +$0.21 total, bootstrap 95% CI
`[-0.66, +1.13]`). The PR #196 failure-audit and benchmark comments posted
against this run (comments `5223809583`, `5216075559`) rest on the invalid
comparison.

Fix: `--setting-sources ""` for harness isolation instead of `--safe-mode`.

## Guards added on PR #168 (commit `d4d41b5`)

- Per-cell `available_tools`, `mcp_servers`, and `model_usage` recorded in
  every result row.
- `McpUnavailableError` — the harness now aborts fast if a pinned arm's MCP
  server fails to attach, instead of silently degrading to native tools.
- `check_experiment` validates tilth-availability per pinned cell (fails the
  run if a tilth arm didn't actually get tilth attached).
- Per-arm and per-task tool-usage reporting in `analyze.py`.

Verified by a 6-cell haiku smoke run
(`benchmark_20260808_021626_haiku.jsonl`): tilth attached and was called in
both pinned arms.

## `pricing.yaml` drift vs Claude Code's native billing

Sonnet 5 was initially priced in `benchmark/pricing.yaml` using the wrong
rate family, understating computed cost by **-33.7%** vs Claude Code's native
billing. Corrected to the introductory Sonnet-5 rates ($2/M input, $10/M
output, $2.50/M 5m cache writes, $4/M 1h cache writes, $0.20/M cache reads,
`as_of: 2026-08-07`) — residual dropped to -0.50% (remainder attributed to a
haiku sidecar call, decomposable via the new per-row `model_usage` field).
Reports now show a `Δnative` residual line so a future pricing drift is
visible instead of silent.

## Stale installed binary served pre-#151 instructions for weeks

`~/.local/bin/tilth` was a stale build that predated PR #151 (the 2KB
instruction-cap shortening) — it served an 8,688-char instructions blob for
weeks after #151 landed. It was mistaken for current HEAD behavior during an
early measurement pass (the "24,917-char fork surface" reading). Root cause:
version is pinned at `0.8.4` across the whole fork (see CLAUDE.md fork law),
so `tilth --version` cannot distinguish a stale binary from current HEAD.

**Check byte sizes (or rebuild from source), not `--version`, when a fork
binary's prompt surface is in question.**

## Related

- `.cheese/notes/tilth-pr196-sonnet5-audit.md`
- `.cheese/notes/tilth-sonnet5-cost-attribution.md`
- PR #168 (paulnsorensen/tilth): harness + reporting fixes, commits `d4d41b5`, `f8a92c9`

## September 2026 Codex isolation and trusted grading

The September benchmark adds explicit Codex isolation and a forward-migration grader, documented in [Tool Efficiency Report: tilth versus WOZCODE](sources/tilth-versus-wozcode-2026-09.md).[^sept-harness]
These are properties of the extracted harness commit, not claims that every installed runner supports them.
Codex scheduling rejects other providers and `tilth_forced` before inference.
The runner disables discovered skills explicitly; `skip_host_skill_discovery` alone is insufficient in CLI 0.154.0.
It disables skill search, plugins, hooks, apps, and delegation for the measured invocation.
The tilth server is required. A missing successful MCP call or detected skill-access attempt invalidates the run.
Codex lacks Claude's init inventory, so an empty `available_tools` field does not prove MCP absence.
The tilth arm retains native tools and receives explicit MCP-first guidance.
Invocation-only write approval enables `tilth_write`; it neither changes global settings nor confines MCP paths.

The small Luna selection uses two test-graded Gin repairs and excludes read-only, single-file, history-only, and keyword-graded tasks.
It excludes `express_diff_multi_mutation`: the supposedly harmless rename leaves an unresolved `app` reference.
Passing mutation preflight alone does not establish a valid task specification.[^sept-selection]

The larger `gin_edit_render_context` reference changes 20 files across 90 edit sites: 14 production files and six original test files.
It migrates 17 renderers, request-context dispatch, nil-request behavior, cancellation-before-side-effects, and private SSE adaptation.
Cancellation of blocking IO after entry is outside the task contract.
The grader copies candidate source into a clean pinned fixture, restores original assertions, and injects held-out tests after inference.
It accepts new local helpers and tests, then checks the candidate suite separately.
Both default and `nomsgpack` grades run full render/binding suites, selected root regressions, and all-package compilation.
Preflight accepts the reference and rejects incomplete migration, omitted cancellation, premature headers, lost dispatch, and weakened assertions.
The first grader misses `binding/json_test.go:59`; that setup attempt stays excluded.
Unrelated network tests stay outside grading because the pinned Unix-socket readiness test races.
An explicit writable temporary `GOCACHE` prevents unrelated global-cache failures.[^sept-large]

## September 2026 strict arms and invalid attempts

The strict Sonnet benchmark matches tool restrictions, not operating-system confinement.[^sept-strict]
Native retains file tools. Each MCP arm uses only its own file tools and Bash.
All arms share a fail-closed Go test/build/vet/formatting guard, with no delegation.
WOZCODE hooks remain active. Explicit `plugin_woz_code` registration is necessary under `--strict-mcp-config`.
Verify tool inventory and a successful edit before scored inference.
Fresh per-cell Claude configuration copies authentication only and preserves refreshed credentials between cells.
Without an explicit seed, legacy Claude authentication keeps its original path.
Require successful completion and `is_error: false` before grading.

| Excluded attempt | Reason | Disposition |
|---|---|---|
| Luna `20260928_152154` | Grader rejects a valid binding caller migration | Preserve timeout and interrupted trace; fix grader before a fresh schedule |
| Sonnet `20260928_191916` | Tilth uses 47 native edits while WOZCODE has different restrictions | Preserve stopped outcomes; restart all arms under strict tools |
| Sonnet `20260928_201559` | One pass followed by 14 revoked-token errors | Authentication-invalid, not task failures; restart all 15 cells |

The valid strict retry is `20260928_202801`.
Its wrapper checks credential lifetime and stops on authentication failure.
Do not copy an interactive access token and assume it survives an unattended schedule.
Keep credentials out of reports and logs.
Historical test results are evidence for their original tree, not substitutes for testing an extracted branch.

When rebasing this wiki-only PR after #280, retain the benchmark tree from `main`.
The split commit removes earlier benchmark files, not #280's replacement.
Retarget report links to the rebased archive commit, which remains in the PR history.

[^sept-harness]: https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/luna56-fixed-results.md; https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/run.py
[^sept-selection]: https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/luna56-curated-selection.md
[^sept-large]: https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/luna56-render-context-results.md; https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/luna56-render-context-invalid-attempt.md
[^sept-strict]: https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/sonnet5-strict-plan.md; https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/sonnet5-strict-auth-failure.md; https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/sonnet5-hybrid-stopped-results.md

## October 2026 GEPA overhaul gotchas

The benchmark GEPA overhaul found these silent-failure modes. PR #312 is the all-in-one provenance PR. It closes unmerged, and the work lands as the stacked PRs #313-#319. The guards that fix these modes are on [Benchmark GEPA loop invariants and guards](benchmark-gepa-invariants.md).

- The `bare` variant flag means `--setting-sources ""`, not `claude --bare`. The real `--bare` ignores `CLAUDE_CODE_OAUTH_TOKEN`, so it breaks subscription runs (`benchmark/run.py:989-990`).
- A quota rejection can arrive with no `result` event. Without a result event, a rejected `rate_limit_event` must classify as quota, or the run treats it as an ordinary error (`benchmark/parse.py:648`).
- pytest-pretty hides the pytest summary line, so a gold patch scored 0 until the grader passed `-p no:pretty`.
- FeatureBench environment builds fail unless install order and `set -e` match the upstream setup script.
- Stored rows written before the `bare` field existed never fill a baseline slot. Their stock arms re-run instead of raising drift.
- A benchmark cell that reads the checkout's `.cheese/` tree is contaminated (`harness_notes`): review notes and specs quote local-task ground-truth identifiers.
- Phase-agent self-reviews missed blockers that independent mutation-tested reviews found; see [Multi-agent workflow notes](multi-agent-workflow-notes.md).

### gepa 0.1.4 integration gotchas

Each item is a gepa 0.1.4 behavior that silently breaks a naive integration. The gepa source lines come from the evolve spec (`e42d451:.cheese/specs/bench-evolution-loop.md:47,50,52-54,92`). The evolve lines are verified at `70466b9`.

- gepa sends each dataset item to the evaluator only as the keyword `example`. It drops each keyword that the evaluator does not name (`gepa/optimize_anything.py:1002-1044`). So the evaluator signature is `evaluate(candidate, example)` (`benchmark/evolve/loop.py:313`).
- When `custom_candidate_proposer` is set, it owns every proposal, and gepa never calls `reflection_lm`. The default `reflection_lm` is `openai/gpt-5.1` through litellm, which needs an OpenAI API key. Evolve sets `reflection_lm=None` and registers one dispatcher (`benchmark/evolve/engine.py:1-6,113`).
- A proposer exception does not stop the search. gepa catches it, calls the proposer again, and then skips the proposal (`gepa/proposer/reflective_mutation/reflective_mutation.py:203-225`, `gepa/core/engine.py:846-849`). So evolve never stops by raising. It sets a shared stop state, and a `StopperProtocol` callback ends the run (`benchmark/evolve/engine.py:37-44`; reasons at `benchmark/evolve/calls.py:16`).
- A candidate that ties the best score on an instance joins that instance's Pareto front (`gepa/core/state.py:496-508`). Grader scores of 0 and 1 make ties routine, so the front can hold a candidate with one rollout. Evolve keeps its own accepted frontier and never reads the gepa front (`benchmark/evolve/loop.py:402-438`).
- gepa evaluates a child on its minibatch before the child gets a pool index. A child that fails minibatch acceptance never gets an index (`gepa/proposer/reflective_mutation/reflective_mutation.py:517,533-550`). So evolve gives each content id a first-seen sequence number and orders candidates only by it (`benchmark/evolve/loop.py:212,487,513`).

### External-task data gotchas

These items come from the external-tasks spec (`e42d451:.cheese/specs/bench-external-tasks.md:59,68,101`).

- The gin fixture at `d7776de7` (`benchmark/config.py:130`) already holds the gold change of `gin-gonic__gin-3741` (`logger.go:93-94`). An external cell that reads the local clones under `/tmp/tilth_bench/repos/` can see the fix. The contamination scan flags such a read as `foreign_repo_clone` (`benchmark/external/contamination.py:149`). Unverified: this is a spec claim. The fixture was not on disk for a check on 2026-10-13.
- A FeatureBench mask `patch` can also edit a FAIL_TO_PASS file. The row `pydata__xarray.97f3a746.test_backends_chunks.fa55f68a.lv1` removes an import line from `xarray/tests/test_backends_chunks.py`. The `test_patch` is a deleted-file diff against `base_commit`, so it does not apply after a mask hunk edits that file. The mask applies forward (`benchmark/external/featurebench.py:78`).
- `repo_settings.docker_specs.run_args.cuda_visible_devices` is not a GPU signal, because CPU-only Pareto-12 rows set it. The curated CPU candidate list and an admission round trip on a CPU host keep GPU tasks out.

_Source: PR #278 at 2b17c3755589a4e89eccaf89c3309ea9591aaa82; PR #312 session archive (closed unmerged; work lands as #313-#319) verified at `e77a567`; gepa and data gotchas from specs at `e42d451`, evolve code verified at `70466b9` · Updated: 2026-10-13 · Supersedes: no historical measurements; narrows general claims to their measured configurations_
