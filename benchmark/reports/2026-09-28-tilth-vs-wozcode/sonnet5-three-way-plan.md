# Sonnet 5 three-way benchmark

## Contract

Run gin_edit_render_context five times per arm: baseline, tilth, and wozcode.
Use claude-sonnet-5 with explicit high effort and no delegated agents.
Use the existing pinned Gin fixture, task prompt, and trusted grader unchanged.
Use the existing 600-second cell timeout and a $10 per-cell safety cap.
Shuffle arm order within repetition blocks with a recorded seed.
Count task failures and timeouts. Do not replace them with extra successful runs.
Keep setup probes separate from the 15 scored cells.

## Fixed inputs

- Workspace base: 18b7534eecde023cb4a70e5d13d7de29178073de, with prior uncommitted benchmark and allocator changes.
- Gin: d7776de7d444935ea4385999711bd6331a98fecb.
- Tilth: target/release/tilth, SHA256 67385086492d457aaadea0e2caa225fccb935ac3d653dfb1d1fbe52ddbc0060d.
- WOZCODE: .context/wozcode-plugin, version 0.3.92, commit 1c7687322e2e65ae85450160489ad78a3b63cbf0.
- Claude CLI: 2.1.283.
- Go: 1.24.7, GOCACHE=/tmp/tilth-context-go-cache.SA1y6u.

## Isolation

No global plugin installation. WOZCODE authentication uses .context/wozcode-auth.
Claude OAuth is passed only in the child environment from the existing authenticated macOS keychain entry.
Never persist or print the OAuth token.
Use fresh Claude configuration per cell. Copy only WOZCODE auth state, not previous sessions or settings.
Preserve refreshed WOZCODE auth between sequential cells if needed.
Disable host skills and other plugins. Do not use Claude safe-mode because it removes explicit MCP servers.
Use explicit MCP server configuration and inspect tool inventory.
WOZCODE's normal Haiku exploration delegation is disabled for this Sonnet-only tool comparison.

## Setup evidence

- Native Sonnet 5 high smoke: .context/sonnet5-native-smoke2.jsonl; OK.
- Isolated configuration without OAuth: setup failure, not a benchmark cell.
- Plugin-only strict MCP configuration: no WOZCODE tools; setup failure, not a benchmark cell.
- Explicit WOZCODE MCP without permissions: permission-denied search; setup failure, not a benchmark cell.
- Disposable WOZCODE read/edit: .context/sonnet5-woz-edit-smoke.jsonl; fixture changed from old value to new value; success.
- All setup calls are outside scored results.

## Baseline

Command: GOCACHE=/tmp/tilth-context-go-cache.SA1y6u mise exec go@1.24.7 -- .context/benchmark-venv/bin/python -m pytest benchmark/tests -q
Result: 148 passed in 104.04 seconds.
Log: .context/sonnet5-three-way-baseline-go.log.
The initial command omitted Go from PATH and failed the grader preflight. The corrected environment passes.

## Scope

Source changes only affect benchmark configuration, runner, tests, and documentation.
Parent owns probes, frozen inputs, integrated gates, execution, and result analysis.
The Cook coder owns source edits. A fresh taste-test follows implementation.

## Integration status

Fresh taste-test: all seven lenses pass; no correction requested.
Current focused runner tests: 36 pass. Parent compileall and CLI help checks pass.
Full integrated benchmark suite is running before the scored schedule.
The optional basedpyright invocation fails; baseline attribution is not established. Do not claim a clean type check.
Tilth Sonnet read/edit smoke also passes: .context/sonnet5-tilth-edit-smoke.jsonl.
