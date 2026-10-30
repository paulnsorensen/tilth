# ADR: benchmark GEPA overhaul

The benchmark GEPA overhaul grows the benchmark with FeatureBench, SWE-bench Multilingual, and the Gin render-context task. It also freezes stock baselines, adds a model judge, and adds a GEPA evolution loop over tilth commits.
PR #312 is the all-in-one PR, and it closes unmerged. The work lands as the stacked PRs #313 to #319 (`stack/bench-gepa/0-prompt-files` to `stack/bench-gepa/6-wiki`).
In the #312 session archive, "Landed" means implemented on the #312 branch, not merged.
The umbrella spec `benchmark-gepa-overhaul` (forks F-1 to F-6) and six `bench-*` child specs held the contracts. The stack removes the spec files, so this page keeps their goals, picks, and curd map.
Each spec used its three fork-coherence rounds and was accepted with a recorded override. paulnsorensen/easy-cheese#745 tracks that Mold override gap.
The invariants and guards that enforce these decisions are on [Benchmark GEPA loop invariants and guards](../benchmark-gepa-invariants.md).

## Goals and non-goals

- **G-1:** FeatureBench tasks run in the harness.
- **G-2:** The Gin render-context task (`gin_edit_render_context`) is in the panel.
- **G-3:** The three local hard tasks plus one Go and one Rust SWE-bench Multilingual instance are in the panel.
- **G-4:** The research model reasons over raw result JSONL.
- **G-5:** Stock baselines run once, are stored, and are reused.
- **G-6:** A model judge decides where structural (ast-grep) tools apply.

Non-goals:

- A structural write op in `tilth_write` (issue #310).
- Docker, Harbor, or native `fb infer`/`fb eval`.
- GPU tasks, Level 2 tasks, and tasks whose environment does not build in a local `uv` venv.
- Matching published leaderboard numbers.
- Auto-merging a winner, and a crate or npm version bump.

## Decisions

### ADR-001: Run external tasks fully native, with no Docker (F-1) [status: accepted]

- **Context:** The maintainer runs the benchmark on a Claude and Codex subscription on his own machine and asked for no container infrastructure. Research ([subscription-auth research note](../sources/benchmark-subscription-auth-2026-10.md)) showed grading fidelity comes from host preflight, not from images.
- **Decision:** FeatureBench Lite Level 1 and SWE-bench Multilingual tasks run on a local checkout without upstream history. They use a local `uv` venv or Go/Rust toolchain. Our own code replays FAIL_TO_PASS and PASS_TO_PASS tests on a clean copy. Any `docker` or `podman` step is an error.
- **Alternatives:** Harbor supports both subscription logins, but its FeatureBench verifier shares the agent container and leaves the gold patch readable. Native `fb infer` requires `OPENAI_API_KEY` for Codex and has no MCP hook. Upstream Docker graders were rejected with the container requirement.
- **Consequences:** Agent cells get no network or filesystem isolation, so a contamination scan replaces isolation. Tasks that need a GPU or a heavy Python environment are deferred (`bench-gpu-host`, `bench-heavy-python-envs`). A task enters the panel only after a native admission round trip on the host.

### ADR-002: Authenticate only through subscription OAuth [status: accepted]

- **Context:** The maintainer directed subscription use, not an API key. Under `claude -p`, `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_API_KEY` beat `CLAUDE_CODE_OAUTH_TOKEN`, so a stray key silently moves billing to the API. `claude --bare` ignores OAuth.
- **Decision:** Claude cells, the judge, reflection, and the proposer all call `claude -p` with `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`. One shared guard refuses to start when `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` is set. A usage-limit rejection stops the run as `quota`. Cells run one at a time, and Codex uses the host `CODEX_HOME`, because Codex rotates `auth.json` and forbids sharing it across concurrent jobs.
- **Alternatives:** API-key billing (rejected by directive). Per-cell copies of `/login` credentials (refresh tokens rotate; the one-year setup token does not).
- **Consequences:** `total_cost_usd` is a notional price-table estimate, and quota is the binding limit. Anthropic's Consumer Terms on automated access are unreconciled with its `setup-token` docs; the exposure falls on the maintainer's account. The archive's umbrella text names only `ANTHROPIC_API_KEY`; the code also refuses `ANTHROPIC_AUTH_TOKEN`, and the code wins.
- **Amendment (2026-10-30, maintainer directive "Claude subscribers should be able to run this"):** the goal is subscription billing, not the token. Without `CLAUDE_CODE_OAUTH_TOKEN`, every call now uses the host `claude /login` session in place, and the preflight requires `authMethod` `claude.ai` on the `firstParty` provider. The API-key refusals, the settings scan, and the `apiKeySource` `none` init gate are unchanged. No credentials are copied, so the rejected per-cell-copy alternative stays rejected. Remaining risk: concurrent calls on one login can contend for the shared OAuth refresh lock; cells run one at a time.

### ADR-003: Buy stock baselines once and refuse silent drift [status: accepted]

- **Context:** Published leaderboards have no per-instance rows for current stock agents. Every A/B run used to re-buy the baselines.
- **Decision:** Every row carries a run key. A result store answers any baseline cell whose key matches a completed row. When a baseline slot has a completed row only under a different key, the run refuses. The flag `--refreeze-baselines` then buys the baseline once under the new key.
- **Alternatives:** Re-running baselines per experiment (cost). Letting a changed key silently start a new baseline (hides drift and re-buys without consent).
- **Consequences:** A host toolchain or agent CLI update between runs costs one deliberate re-buy. Expect one `--refreeze-baselines` the first time an older store meets the new key.

### ADR-004: Evolve commits with GEPA and land winners as draft PRs (F-2, F-3, F-4) [status: accepted]

- **Context:** MCP instructions and tool descriptions are compiled in with `include_str!`, so an optimizer must produce builds, not text swaps. `variant.git_sha` and `binary_sha256` already prove what a cell served.
- **Decision:** A candidate is a commit. GEPA `optimize_anything` (`gepa==0.1.4`) evolves text components under `prompts/**` with its reflection model and a `src_patch` over `src/**` from a coding-agent proposer. Frontier candidates are re-run before acceptance. The winner opens as an unmerged draft PR.
- **Alternatives:** Text-only optimization (impossible while prompts are compiled in). HGM clade-metaproductivity parent selection is deferred (`bench-hgm-parent-selection`) until frontier noise is shown to dominate.
- **Consequences:** The widest search space invites overfitting to a small dev split. `just check`, the candidate guards, and the held-out test split are the guards.

### ADR-005: Grader correctness is the only score; the judge only informs reflection (F-5) [status: accepted]

- **Context:** The maintainer preferred a model judge over manual task tagging to decide where structural (ast-grep) tools apply. A judge that moved the score would let the loop optimize the judge instead of the task.
- **Decision:** A pinned judge assigns categorical applicability labels and critiques rollouts from the same stripped record that reflection receives. Labels slice reports and enter reflection. Critiques enter reflection only. Neither enters a score, and both are withheld while the judge fails its calibration gate.
- **Consequences:** The calibration set must be hand-labelled before labels flow. Until then the loop runs uncalibrated.

### ADR-006: Retire the Phase 4 power gate; require a spend ceiling (F-6) [status: accepted]

- **Context:** The power gate in `benchmark/PHASE4_PLAN.md` had no exception process and blocked task growth.
- **Decision:** The gate is retired. Stack layer 1 (`stack/bench-gepa/1-result-substrate`, #314) deletes `benchmark/PHASE4_PLAN.md`. The spec planned a superseded marker in the file; that plan did not ship. The `analyze.py` power readout is informational only. Every paid run requires `--max-usd`.
- **Consequences:** `--max-usd` limits spend, not false-positive conclusions. Nothing replaces the statistical stop.

### ADR-007: Admit a task only when a single gold hunk is load-bearing [status: accepted]

- **Context:** The first tampered check removed only the last gold hunk. It refused tasks whose last hunk the held-out tests never exercise.
- **Decision:** Admission requires that the gold patch resolves, the empty patch does not, and removing some single gold hunk fails the held-out tests. Removals are tried largest first, bounded by `$TILTH_BENCH_TAMPER_MAX_HUNKS` (default 8). Cached verdicts carry a version and recompute on a rule change.
- **Supersedes:** the gold-minus-last-hunk rule in the first `bench-external-tasks` implementation.

### ADR-008: Child-spec defaults [status: accepted]

- **Context:** The child specs marked some choices `[AGENT-DECIDED]` and recorded an alternative for each. The maintainer approved the specs with these defaults.
- **Decision:**
  - Harness data lives in `$TILTH_BENCH_DATA`, default `~/.local/share/tilth-bench/external`, not beside the `/tmp` fixture repos.
  - External cells time out per language: Python 1200 s, Go and Rust 1800 s, not one global flag.
  - `pass_rate` counts FAIL_TO_PASS plus PASS_TO_PASS entries, not FAIL_TO_PASS only.
  - The contamination scan reads tool-call inputs only, not outputs. A row with no sidecar is `contaminated: true` with an `unscanned` hit (spec claim, not re-verified).
  - Panel strata are source families, not family plus language. Family plus language left single-member strata and could empty the test split.
  - The split locks on the first completed stored row, not on any row. Every `load_panel` call checks it.
  - The panel loader does not re-run a primary's preflight before it accepts a fallback (spec claim, not re-verified).
  - `run.py --tasks <instance_id>` resolves an admitted external task; `--tasks all` stays local-only. `preflight.py --panel` reads only `members[].id` and `members[].family` (spec claim, not re-verified).
- **Alternatives:** Each bullet names the rejected alternative.
- **Enforcement:** [Benchmark GEPA loop invariants and guards](../benchmark-gepa-invariants.md) holds the code citations.

## Task picks and fallbacks

- SWE-bench Multilingual at HF revision `846e647b9f33c0b51b739d005d13d85493c9af09` (`benchmark/external/data.py:17`): Go `gin-gonic__gin-3741` with fallback `prometheus__prometheus-14861`; Rust `sharkdp__bat-2650` with fallback `tokio-rs__tokio-6724`.
- FeatureBench Lite at commit `76b4a4566e04f4bcc13c35125d4f301791efa736` (`benchmark/external/data.py:16`). That commit is the Hugging Face tag `v1.1` on 2026-10-13. A pinned parquet sha256 checks the download (`benchmark/external/data.py:25-28`). The task list is the COSPA Pareto-12 list restricted to `.lv1` IDs, minus pandas, astropy, transformers, Lightning, and mlflow.
- Local: `rg_search_dispatch`, `rg_trait_implementors`, and `gin_servehttp_flow` form the dev-only cheap tier. `gin_edit_render_context` joins the stratified split.
- The committed `gepa-v1` panel uses both primaries and no fallback (`benchmark/panels/gepa-v1.json:60,66`).

## Admission on the cloud host

The model-free preflight on the #312 cloud host admitted 5 of 7 FeatureBench Lite Level 1 candidates (sphinx, metaflow, seaborn x2, sympy). It also admitted `gin-gonic__gin-3741` and `sharkdp__bat-2650`.
It refused pydantic (gold 4/6) and xarray (pytest-mypy-plugins option mismatch) as `gold_unresolved`.
Dataset rows hold gold patches and held-out tests, so they are not in the checkout. To regenerate them, run `python3 benchmark/external/preflight.py --panel benchmark/panels/gepa-v1.json`.

## Curd to stack map

The umbrella planned a diamond (c0 beside c1, c3 beside c4). The stack lands the curds in a line.

| Curd | Child spec | Stack layer | PR |
|---|---|---|---|
| c0 | `bench-prompt-files` | `0-prompt-files` | #313 |
| c1 | `bench-result-substrate` | `1-result-substrate` | #314 |
| c2 | `bench-external-tasks` | `2-external-tasks` | #315 |
| c3 | `bench-panel-manifest` | `3-panels` | #316 |
| c4 | `bench-model-judge` | `4-judge` | #317 |
| c5 | `bench-evolution-loop` | `5-evolve` | #318 |
| (none) | wiki ingest | `6-wiki` | #319 |

## Deferred follow-ups

- `bench-gpu-host`: rent a GPU host to admit the GPU-flagged FeatureBench Lite tasks.
- `bench-heavy-python-envs`: admit the pandas, astropy, transformers, Lightning, and mlflow Pareto-12 tasks when a native environment recipe exists.
- `bench-hgm-parent-selection`: replace GEPA parent selection with HGM clade-metaproductivity if frontier noise dominates.
- `bench-oauth-fixture`: the maintainer records a scrubbed real `claude -p` OAuth stream and commits it as a parser fixture.
- `fix-tilth-deps-route`: done. #311 removed the retired `tilth_deps` route from `prompts/mcp.md` and `AGENTS.md` and added a guard test.

## References

- FeatureBench dataset: https://huggingface.co/datasets/LiberCoders/FeatureBench
- COSPA Pareto-12: https://raw.githubusercontent.com/shisa-ai/cospa/main/configs/featurebench_lite_pareto12_v1.json
- SWE-bench Multilingual: https://www.swebench.com/multilingual.html
- Claude Code authentication: https://code.claude.com/docs/en/authentication
- GEPA `optimize_anything`: https://gepa-ai.github.io/gepa/blog/2026/02/18/introducing-optimize-anything/
- Huxley-Gödel Machine: https://arxiv.org/html/2510.21614v2

All were accessed on 2026-10-03.

_Source: umbrella and child specs at `9e84659` (removed from the stack; the head of closed PR #312, reachable through `refs/pull/312/head`) and the PR #312 session archive README. Decisions verified against the layer 6 head of PR #319 after the PR #318 cure · Updated: 2026-10-14 · Supersedes: gold-minus-last-hunk admission rule_
