# Local gate gotchas (macOS)

Use the current CI workflow for local gate commands.
The August 2026 failure baseline below no longer defines the required checks.[^1]

## Current gates

The CI check job runs these commands:[^1]

```sh
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
python3 -m unittest discover -s tests/mcp_v2 -t tests/mcp_v2
python3 scripts/tilth-bash-guard --self-test
```

The markdown job checks `prompts/` and `AGENTS.md`.
It also verifies that the regeneration script leaves `AGENTS.md` unchanged.[^1]

## Historical baseline

The August 2026 review of PR #144 reports a macOS batch-budget test failure.
PR #227 changes that test to remove path dependence.[^2]
Do not classify a current failure as the old baseline without a fresh comparison against `origin/main`.

The former clippy claim covers a workflow that runs without `--all-targets`.
The current workflow includes that flag.[^1]

The original PR #242 description also says the MCP Python suite is outside CI.
That description uses an older base and does not describe current `main`.[^1][^3]

[^1]: `.github/workflows/ci.yml:23-41`, verified at `89ffbcb3a18d199dcbeceda0af4236891394171f`.
[^2]: https://github.com/paulnsorensen/tilth/pull/227
[^3]: https://github.com/paulnsorensen/tilth/pull/242

_Source: PR #242 review · Updated: 2026-09-07 · Supersedes: August 2026 local-gate baseline guidance._
