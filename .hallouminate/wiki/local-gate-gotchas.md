# Local gate gotchas

Use the current CI workflow for local gate commands.
The August 2026 failure baseline below no longer defines the required checks.[^1]
Cucumber BDD language coverage verifies public behavior; unit tests retain private parser invariants.[^14][^15]

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
[^10]: `tests/bdd/features/edit.feature:1-81`; `tests/bdd/main.rs:97-105,343-395`.
[^11]: `tests/bdd/main.rs:489-503`.

The BDD runner accepts Cargo positional filters as literal scenario-name substrings.
Without this custom CLI field, the documented `cargo test edit` command fails before Cucumber runs scenarios.
Explicit Cucumber `--name` and `--tags` filters retain priority over the positional filter.[^12]

[^12]: `tests/bdd/main.rs:482-503`; `tests/bdd/README.md:10-21`; `CONTRIBUTING.md:13`. Verified on 2026-09-29.

## Cucumber BDD language coverage

Cucumber BDD language coverage follows the existing registry: 19 languages and 39 filename forms.
The matrix contains 29 extension forms and 10 exact filenames.
Seventeen languages provide grammars for 35 forms.
Docker and Make supply the remaining four filename forms without structural grammars.[^13]

Grammar-backed rows require a structural symbol result with an exact name, path, canonical line, and source body.
Docker and Make rows use valid native syntax and require literal-match evidence instead.
Every row reads, applies a tagged edit, checks complete file bytes, and searches the changed source.
Expected values come from test fixtures, not production registry or parser output.[^14]

Move a unit case into BDD only when its public assertions have exact replacements.
Keep private invariants in units, including registry completeness, ranking weights, byte identity, cache keys, and depth limits.
MCP search deduplicates overlapping definition candidates before it returns a unique result.
Retain raw-definition count assertions in units; one public result cannot prove one parser match.[^15]
A successful response alone does not prove matching, ownership, ambiguity, or source-span correctness.
A demonstrated defect requires a failing regression before the smallest production fix.
Do not add parsers or supported aliases as part of test migration.
Use the [language policy registry](language-policy-registry.md) for behavior-preserving LangSpec refactors.

Upstream language matching regressions belong in `upstream.feature`.
Compare exact caller paths, lines, and owners on cold and warm requests.
Compare exact dependency paths and existing reverse call-site edges.
Keep private lexer-token assertions because Bloom false positives can hide omitted identifiers.
Check LF, CRLF, and CR comments without admitting comment tokens.[^16]
C++ qualified queries retain bare canonical names and select the immediate explicit owner before an enclosing namespace.
Use exact declaration byte identity to distinguish same-line classes and methods.
Use canonical-line matching only when byte identity is absent.[^17]
JavaScript source-extension fallback does not add import-only reverse scanning; that scan remains limited to scoped-import languages.[^18]

[^16]: `tests/bdd/features/upstream.feature:6-55,214-249`; `tests/bdd/main.rs:313-406`; `src/index/bloom.rs:421-452`.
[^17]: `src/search/grok.rs:274-385`; `tests/bdd/features/upstream.feature:97-212`; `src/types.rs:124-125`.
[^18]: `src/lang/javascript.rs:79-110`; `src/search/deps.rs:300-307,530-631`.

[^13]: `src/types.rs:20-44`; `src/lang/mod.rs:33-109`; per-language `SPEC` records.
[^14]: `tests/bdd/features/languages.feature:1-69`; `tests/bdd/fixture_catalog.rs:1-151`; PR #283 approved migration scope.

[^15]: `src/mcp/tools/search_v2.rs:506-519`; assertion-preserving migration review for PR #283.

## Fingerprint language ties after benchmark additions

The fingerprint CI test can fail when benchmark files tie the checkout's sampled language counts.
At PR #280 revision `50300a2`, the depth-two sample contains 84 Rust files and 84 Python files.
The fingerprint chooses a maximum from a `HashMap`, so either language can win that tie.
The live-checkout test requires Rust and fails three of five isolated reruns.[^19]

Use a temporary Rust project for the language assertion instead of the changing repository.
The replacement test keeps the manifest, project-name, nonempty-output, and token-budget checks.
It passes 20 consecutive runs without changing runtime language selection.[^20]

[^19]: [Failed PR #280 CI run](https://github.com/paulnsorensen/tilth/actions/runs/36541092921/job/109316414746); `src/overview.rs:35-46,259-354,819-838` at `50300a271a897e88426f5841b948d78639a90e52`. Local reproduction on 2026-09-29 uses five isolated executions of `cargo test -q --lib overview::tests::test_fingerprint_on_tilth -- --exact --nocapture`.
[^20]: [PR #280 fingerprint fixture correction](https://github.com/paulnsorensen/tilth/pull/280); `src/overview.rs::tests::test_fingerprint_detects_rust_project`. Local verification on 2026-09-29: 20/20 focused executions and all three overview tests pass.

## macOS temporary-path cache keys

On macOS, `tempfile` paths start with `/var`, but `/var` is a symlink to `/private/var`.
The search walker and the test fixture can then key one file under two paths.
Cache lookups miss, and parse-count assertions in `documents_*` MCP tests fail.
At PR #287 head `53a76df`, `documents_reuse_real_parses_across_production_requests` and `documents_python_dependencies_reuse_trees_and_refresh_reexports` fail this way without any code change.[^21]
At main `998f311`, `tool_read_from_line_suffix` and `batch_read_shrink_floor_keeps_not_found_footer` also fail with the default `TMPDIR` and pass with the override.[^22]
Run the local gate with `TMPDIR=/private/var/tmp just check` on macOS.
Linux CI does not have this symlink and is unaffected.

[^21]: PR #287 affinage cure on 2026-10-01. Both tests fail at pristine `53a76df` with the default `TMPDIR`. With `TMPDIR=/private/var/tmp`, `just check` exits 0 (1,216 Rust library tests).
[^22]: PR #293 affinage rebase on 2026-10-01. The same four tests fail at pristine `998f311` and on the rebased PR with the default `TMPDIR`. With `TMPDIR=/private/var/tmp`, `just check` exits 0 (1,258 Rust library tests).

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

_Source: PR #242 review, PR #280 CI diagnosis, MCP acceptance harness code, PR #283 language BDD scope and upstream regressions, PR #287 and PR #293 macOS gate runs · Updated: 2026-10-01 · Supersedes: August 2026 local-gate baseline guidance._
