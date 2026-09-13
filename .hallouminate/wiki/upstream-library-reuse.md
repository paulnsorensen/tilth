# Upstream library reuse assessment

Keep library migrations separate from the two-crate extraction in upstream PR #210.
The September 2026 assessment identifies possible replacements, not approved migrations.[^1]
See [Upstream core extraction CI assessment](./upstream-core-extraction-ci.md) for the measured CI evidence.

## Scope

The assessed source is upstream commit `7f38db58696e16c2df2be71da985f47097f34920`.
Do not assume these findings describe the current fork.
The structural scan covers 44 production Rust files and identifies four non-parser candidates.
Parser research separately checks ast-grep, grammar-owned tags queries, and a grammar pack.

## Important parser distinction

Tilth already uses Tree-sitter and published grammar crates.
The replacement question concerns outline extraction and query adapters, not a custom parser engine.[^2]

The released ast-grep 0.45.3 workspace includes `ast-grep-outline`.
Do not describe that project as only an AST pattern matcher.
Its outline model exposes top-level items and direct members, with names, ranges, signatures, and AST kinds.[^3]
A migration must still cover Tilth's deeper hierarchy, caller queries, local import resolution, and test classification.
The release uses Tree-sitter 0.27; the assessed Tilth commit uses 0.26.[^2][^4]
No migration benchmark or complete per-language coverage comparison exists in this assessment.

Grammar-owned `queries/tags.scm` provides another, narrower reuse path.
Tree-sitter specifies definition and call-reference captures, but each shipped grammar needs a coverage check.[^5]
A static comparison checks Rust, TypeScript, and Python against representative Tilth scope fixtures.
The existing scope tests pass 12/12; no replacement extractor or tags query executes in this comparison.
Rust tags cover trait parents but omit `function_signature_item` patterns for required trait members.[^10]
Python tags identify classes and functions but do not encode the parent relation required by Tilth's outline.[^11]
TypeScript's Rust binding exports its local tags query without automatic JavaScript query composition.[^12]
That bare query omits ordinary class and method-definition patterns.
JavaScript tags contain those patterns, but a combined query remains unverified against the TypeScript grammar.[^13]
Treat these findings as adapter requirements, not evidence that Tree-sitter cannot represent the constructs.

[^10]: https://github.com/tree-sitter/tree-sitter-rust/blob/v0.24.0/queries/tags.scm
[^11]: https://github.com/tree-sitter/tree-sitter-python/blob/v0.25.0/queries/tags.scm
[^12]: https://github.com/tree-sitter/tree-sitter-typescript/blob/v0.23.2/bindings/rust/lib.rs; https://github.com/tree-sitter/tree-sitter-typescript/blob/v0.23.2/queries/tags.scm
[^13]: https://github.com/tree-sitter/tree-sitter-javascript/blob/v0.25.0/queries/tags.scm

## Other candidate boundaries

`tempfile` already exists as a development dependency.
`NamedTempFile::new_in` and `persist` can replace temporary-path creation and cleanup.
They do not replace Tilth's permission policy or expected-content check.[^6]

A macOS probe imports the actual upstream `src/util.rs` at the assessed commit.
Its controlled temporary-directory test plants `.tilth-tmp.<pid>.0` as a symlink before the first helper call.
The upstream helper overwrites the linked victim, then installs the symlink as the destination.[^14]
The `tempfile` 3.27.0 candidate preserves the victim and installs a regular destination.
Both implementations pass the checked normal-write, stale-target, missing-target, permission, cleanup, and existing-mmap assertions.
The probe does not establish safety against compare/rename races or hostile parent-directory changes.

Fork base `4895b4d9f67e9fe70832e1e53745fbe4749bb810` also uses the unsafe replacement pattern.
The issue #251 regression reproduces redirected victim bytes against that fork helper before the fix.
The fix uses exclusive `tempfile::Builder` creation and `persist` for atomic replacement.
Unix creation requests mode `0666`, filtered by umask, to preserve the old new-file permission policy.
A default `NamedTempFile` would instead create mode `0600`.[^16]
The helper copies existing permissions through the open file handle and retains ignored permission errors.
The separate create-only helper remains unchanged.[^15]
All ten utility tests pass after the fix, including permissions, target symlink replacement, cleanup, and existing memory maps.
The comparison does not prove safety against every hostile-directory race.

[^14]: Historical macOS scratch probe, verified 2026-09-12 against https://github.com/jahala/tilth/blob/7f38db58696e16c2df2be71da985f47097f34920/src/util.rs#L43-L81. The research checkout later moves outside the tested tree. The original scratch command is not a maintained reproduction entry point. The committed fork regression is `src/util.rs::tests::atomic_write_rejects_predictable_temp_symlink`.
[^15]: Fork baseline replacement helper: https://github.com/paulnsorensen/tilth/blob/4895b4d9f67e9fe70832e1e53745fbe4749bb810/src/util.rs#L12-L34. The unchanged create-only helper: https://github.com/paulnsorensen/tilth/blob/4895b4d9f67e9fe70832e1e53745fbe4749bb810/src/util.rs#L45-L68.

The official `rmcp` SDK can replace MCP protocol machinery, but its async service model needs an adapter for synchronous tool work.[^7]
`moka` supplies bounded concurrent caching, but file freshness and parse limits remain Tilth policy.[^8]
`git2::Diff::from_buffer` supplies Git patch parsing, but a local type adapter and native libgit2 build/link path remain.[^9]
These are possible maintenance reductions, not verified CI speed improvements.

## Evidence location

Research artifacts use the durable project corpus under `research/`:

- `tilth-parser-library-reuse-options/`
- `tilth-mcp-sdk-reuse-evidence/`
- `tilth-diff-parser-library-options/`
- `tilth-tree-sitter-library-reuse/`

[^1]: https://github.com/jahala/tilth/pull/210
[^2]: https://github.com/jahala/tilth/blob/7f38db58696e16c2df2be71da985f47097f34920/crates/tilth-core/Cargo.toml
[^3]: https://github.com/ast-grep/ast-grep/blob/0.45.3/crates/outline/src/model.rs
[^4]: https://github.com/ast-grep/ast-grep/blob/0.45.3/Cargo.toml
[^5]: https://github.com/tree-sitter/tree-sitter/blob/master/docs/src/4-code-navigation.md
[^6]: https://docs.rs/tempfile/3.27.0/tempfile/struct.NamedTempFile.html; https://github.com/jahala/tilth/blob/7f38db58696e16c2df2be71da985f47097f34920/src/util.rs
[^7]: https://github.com/modelcontextprotocol/rust-sdk/releases/tag/rmcp-v3.3.0; https://github.com/modelcontextprotocol/rust-sdk
[^8]: https://docs.rs/moka/0.12.16/moka/sync/struct.Cache.html
[^9]: https://docs.rs/git2/0.21.0/git2/struct.Diff.html


## Follow-up issues

- P0 atomic-write safety: [#251](https://github.com/paulnsorensen/tilth/issues/251).
- P1 dependency audit: [#252](https://github.com/paulnsorensen/tilth/issues/252).
- P1 extraction and release gates: [#253](https://github.com/paulnsorensen/tilth/issues/253).
- P2 parser reuse experiment: [#254](https://github.com/paulnsorensen/tilth/issues/254).
- P3 conditional library evaluation: [#255](https://github.com/paulnsorensen/tilth/issues/255).

[^16]: https://docs.rs/tempfile/3.27.0/tempfile/struct.Builder.html#method.permissions; src/util.rs::atomic_write_bytes and its utility tests; https://github.com/paulnsorensen/tilth/issues/251

