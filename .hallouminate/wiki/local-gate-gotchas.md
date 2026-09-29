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



Keep research worktrees outside the checkout under test.
A nested upstream worktree under `.context/` appears in repository-wide search results despite the directory's ignored status.
During the atomic-write fix, `detect_file_type` resolves to both the fork and the nested upstream definition.
This causes six Rust failures and seventeen Python failures/errors unrelated to atomic replacement.
Move the research worktree outside the checkout before rerunning the gates.
After that move, all 1,092 Rust tests, 47 Python tests, and 77 Bash guard checks pass.[^4]

[^4]: P0 verification on 2026-09-12, fork base `4895b4d9f67e9fe70832e1e53745fbe4749bb810`. The actual MCP response names `src/lang/mod.rs` and `.context/upstream-core-review/crates/tilth-core/src/lang/mod.rs`. The same production diff passes after `git worktree move` relocates the research checkout outside the tested tree.

## MCP acceptance harness constraints

The Python harness is a batch client, not an interactive MCP client.
It sends all requests before it collects responses.[^5]
A scenario cannot consume a returned read tag and then construct its write request through this helper.
Starting another process also creates another snapshot store.[^6]
Read-to-write acceptance scenarios therefore need one live process and response-dependent requests.
This is a harness requirement, not a reason to select one Gherkin runner.

The binary existence check does not establish build freshness.[^5]
A local behavior run can exercise an older binary after source changes.
The helper also ignores stdout lines that fail JSON parsing.
Successful response assertions alone therefore do not establish clean protocol output.[^5]

Use isolated fixture repositories when testing repository changes.
Some worktree-named tests only inspect the current repository's coverage field.
They do not perform the branch, revert, rename, delete, or untracked-file actions named by those tests.[^7]
This finding concerns those tests, not the repository's total coverage.

[^5]: `tests/mcp_v2/harness.py:6-7,24-26,55-80`, inspected at `7005d6ab912843456dec498266fc3644fbcd4ea4` on 2026-09-29.
[^6]: `src/mcp/mod.rs:175-178,228-231`; `src/session.rs:125-129,145-156`; `src/mcp/tools/write.rs:306-326`.
[^7]: `tests/mcp_v2/test_ac08_worktree.py:63-73,86-90`, inspected on 2026-09-29.

## Gherkin search and edit scenarios

The Rust BDD harness tests MCP search and tagged edits against isolated example files.
Run `cargo test --test bdd`; the normal Cargo test command also includes this target.[^8]
Cucumber stays in development dependencies and uses Cargo's compiled binary instead of an existence-only binary check.[^8]

Each scenario owns one live MCP process, one temporary project, and one isolated cache.
The driver consumes each response before it sends the next request.
This preserves response-minted read tags and session snapshots across the edit sequence.[^9]
The driver rejects malformed responses and imposes a 15-second request deadline.
It terminates and waits for the child before it joins the I/O thread.[^9]

The prototype checks symbol locations and source text, exact edited bytes, search refresh, independent drift, and conflicting-edit rejection.[^10]
Undefined or skipped steps fail the runner, including steps marked `allow.skipped`.[^11]
Keep this focused driver beside the Gherkin steps; it is not a replacement for the existing Python suite.

[^8]: `Cargo.toml:127-135`; `tests/bdd/README.md:1-22`; `tests/bdd/session.rs:40`.
[^9]: `tests/bdd/session.rs:25-91,103-149`.
[^10]: `tests/bdd/features/edit.feature:1-81`; `tests/bdd/main.rs:51-83,138-175`.
[^11]: `tests/bdd/main.rs:194-207`.

The BDD runner accepts Cargo positional filters as literal scenario-name substrings.
Without this custom CLI field, the documented `cargo test edit` command fails before Cucumber runs scenarios.
Explicit Cucumber `--name` and `--tags` filters retain priority over the positional filter.[^12]

[^12]: `tests/bdd/main.rs:187-207`; `tests/bdd/README.md:10-21`; `CONTRIBUTING.md:13`. Verified on 2026-09-29.

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

_Source: PR #242 review and MCP acceptance harness code · Updated: 2026-09-29 · Supersedes: August 2026 local-gate baseline guidance._
