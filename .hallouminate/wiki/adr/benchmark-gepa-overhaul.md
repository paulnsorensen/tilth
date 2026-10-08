# ADR: benchmark GEPA overhaul

The benchmark GEPA overhaul (PR #312, landed as the `stack/bench-gepa/*` stack) grows the benchmark with FeatureBench, SWE-bench Multilingual, and the Gin render-context task, freezes stock baselines, adds a model judge, and adds a GEPA evolution loop over tilth commits.
These decisions explain why the harness looks the way it does. The specs are the contracts: `.cheese/specs/benchmark-gepa-overhaul.md` (umbrella, forks F-1 to F-6) and its children `bench-prompt-files`, `bench-result-substrate`, `bench-external-tasks`, `bench-panel-manifest`, `bench-model-judge`, and `bench-evolution-loop`.
The invariants and guards that enforce these decisions are on [Benchmark GEPA loop invariants and guards](../benchmark-gepa-invariants.md).

### ADR-001: Run external tasks fully native, with no Docker (F-1) [status: accepted]

- **Context:** The maintainer runs the benchmark on a Claude and Codex subscription on his own machine and asked for no container infrastructure. Research ([subscription-auth research note](../sources/benchmark-subscription-auth-2026-10.md)) showed grading fidelity comes from host preflight, not from images.
- **Decision:** FeatureBench Lite Level 1 and SWE-bench Multilingual tasks run on a local checkout without upstream history, a local `uv` venv or Go/Rust toolchain, and our own replay of FAIL_TO_PASS and PASS_TO_PASS tests on a clean copy. Any `docker` or `podman` step is an error.
- **Alternatives:** Harbor supports both subscription logins, but its FeatureBench verifier shares the agent container and leaves the gold patch readable. Native `fb infer` requires `OPENAI_API_KEY` for Codex and has no MCP hook. Upstream Docker graders were rejected with the container requirement.
- **Consequences:** Agent cells get no network or filesystem isolation, so a contamination scan replaces isolation. Tasks that need a GPU or a heavy Python environment are deferred (`bench-gpu-host`, `bench-heavy-python-envs`). A task enters the panel only after a native admission round trip on the host.

### ADR-002: Authenticate only through subscription OAuth [status: accepted]

- **Context:** The maintainer directed subscription use, not an API key. Under `claude -p`, `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_API_KEY` beat `CLAUDE_CODE_OAUTH_TOKEN`, so a stray key silently moves billing to the API. `claude --bare` ignores OAuth.
- **Decision:** Claude cells, the judge, reflection, and the proposer all call `claude -p` with `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`. One shared guard refuses to start when `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` is set (`benchmark/run.py:207-220`). A usage-limit rejection stops the run as `quota`. Cells run one at a time, and Codex uses the host `CODEX_HOME`, because Codex rotates `auth.json` and forbids sharing it across concurrent jobs.
- **Alternatives:** API-key billing (rejected by directive). Per-cell copies of `/login` credentials (refresh tokens rotate; the one-year setup token does not).
- **Consequences:** `total_cost_usd` is a notional price-table estimate, and quota is the binding limit. Anthropic's Consumer Terms on automated access are unreconciled with its `setup-token` docs; the exposure falls on the maintainer's account. The archive's umbrella text names only `ANTHROPIC_API_KEY`; the code also refuses `ANTHROPIC_AUTH_TOKEN`, and the code wins.

### ADR-003: Buy stock baselines once and refuse silent drift [status: accepted]

- **Context:** Published leaderboards have no per-instance rows for current stock agents. Every A/B run used to re-buy the baselines.
- **Decision:** Every row carries a run key, and a result store answers any baseline cell whose key matches a completed row. When a baseline slot has a completed row only under a different key, the run refuses until `--refreeze-baselines` buys the baseline once under the new key.
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
- **Decision:** The gate is retired (`PHASE4_PLAN.md` is marked superseded; `analyze.py` power readout is informational). Every paid run requires `--max-usd`.
- **Consequences:** `--max-usd` limits spend, not false-positive conclusions. Nothing replaces the statistical stop.

### ADR-007: Admit a task only when a single gold hunk is load-bearing [status: accepted]

- **Context:** The first tampered check removed only the last gold hunk. It refused tasks whose last hunk the held-out tests never exercise.
- **Decision:** Admission requires that the gold patch resolves, the empty patch does not, and removing some single gold hunk fails the held-out tests. Removals are tried largest first, bounded by `$TILTH_BENCH_TAMPER_MAX_HUNKS` (default 8). Cached verdicts carry a version and recompute on a rule change.
- **Supersedes:** the gold-minus-last-hunk rule in the first `bench-external-tasks` implementation.

_Source: PR #312 session archive (umbrella and child specs, wheypoint decisions, phase outcomes), verified against code at `e77a567` · Updated: 2026-10-08 · Supersedes: gold-minus-last-hunk admission rule_
