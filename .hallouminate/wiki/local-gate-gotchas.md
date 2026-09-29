# Local gate gotchas

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



Keep research worktrees outside the checkout under test.
A nested upstream worktree under `.context/` appears in repository-wide search results despite the directory's ignored status.
During the atomic-write fix, `detect_file_type` resolves to both the fork and the nested upstream definition.
This causes six Rust failures and seventeen Python failures/errors unrelated to atomic replacement.
Move the research worktree outside the checkout before rerunning the gates.
After that move, all 1,092 Rust tests, 47 Python tests, and 77 Bash guard checks pass.[^4]

[^4]: P0 verification on 2026-09-12, fork base `4895b4d9f67e9fe70832e1e53745fbe4749bb810`. The actual MCP response names `src/lang/mod.rs` and `.context/upstream-core-review/crates/tilth-core/src/lang/mod.rs`. The same production diff passes after `git worktree move` relocates the research checkout outside the tested tree.

## Fingerprint language ties after benchmark additions

The fingerprint CI test can fail when benchmark files tie the checkout's sampled language counts.
At PR #280 revision `50300a2`, the depth-two sample contains 84 Rust files and 84 Python files.
The fingerprint chooses a maximum from a `HashMap`, so either language can win that tie.
The live-checkout test requires Rust and fails three of five isolated reruns.[^5]

Use a temporary Rust project for the language assertion instead of the changing repository.
The replacement test keeps the manifest, project-name, nonempty-output, and token-budget checks.
It passes 20 consecutive runs without changing runtime language selection.[^6]

[^5]: [Failed PR #280 CI run](https://github.com/paulnsorensen/tilth/actions/runs/36541092921/job/109316414746); `src/overview.rs:35-46,259-354,819-838` at `50300a271a897e88426f5841b948d78639a90e52`. Local reproduction on 2026-09-29 uses five isolated executions of `cargo test -q --lib overview::tests::test_fingerprint_on_tilth -- --exact --nocapture`.
[^6]: [PR #280 fingerprint fixture correction](https://github.com/paulnsorensen/tilth/pull/280); `src/overview.rs::tests::test_fingerprint_detects_rust_project`. Local verification on 2026-09-29: 20/20 focused executions and all three overview tests pass.

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

_Source: PR #242 review and PR #280 CI diagnosis · Updated: 2026-09-29 · Supersedes: August 2026 local-gate baseline guidance._
