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

## Shared parsed documents in the fork

Tilth's parsed-document cache owns cached source and syntax trees through one ast-grep `StrDoc` per revision.
Production read, search, grok, caller, and structural paths borrow that document through `ParsedFile`.
The MCP service passes one existing `OutlineCache` through these paths; it does not add a second parser cache.[^17]

Disk revisions guard cache reuse and publication.
A late parse cannot replace an already published newer revision.
Concurrent misses for one revision reuse the first published snapshot.
Old readers retain their original ast-grep source and tree through `Arc<ParsedFile>`.
Warm reads and structural matches reuse the same source pointer, tree identity, and candidate root.[^18]

The parsed cache retains at most 500 entries, with a 500,000-byte source limit per entry.
Large files use uncached parsing where existing consumers permit them.
This fallback preserves search results without increasing retained cache limits.
Full and range reads do not require a syntax parse.[^19]

Tree-aware helpers preserve existing outline extraction instead of replacing it with ast-grep outlines.
Warm writes clone the retained document into a private replacement, then use the public `AstGrep::edit` operation for incremental reparsing.
Cold parses move the owned read `String` into the document. Only `&str` input copies. Warm reads and structural matches borrow the retained string.
Tests compare source pointers, lengths, capacities, and retained-reader bytes; they do not measure total or transient allocator calls.[^18]
One compiled-query cache serves caller, callee, sibling, and receiver matching.
Keys use actual Tree-sitter grammar identity and query content, including unnamed grammars.
File edits retain compiled queries. Failed compilation remains retryable, and callbacks run after the cache mutex is released.[^20]
Parse reuse is verified behavior, not a measured end-to-end latency claim.

[^17]: src/cache.rs::ParsedFile; src/cache.rs::OutlineCache::parse_with_revision; src/read/outline/mod.rs::generate_cached; src/mcp/mod.rs::tests::documents_reuse_real_parses_across_production_requests
[^18]: src/search/structural.rs::tests::owned_document_borrows_cached_bytes_and_tree; src/mcp/tools/search_v2.rs::tests::structural_requests_retain_one_real_candidate_root; src/mcp/mod.rs::tests::incremental_write_reuses_tree_through_production_requests
[^19]: src/cache.rs::MAX_PARSED_ENTRIES; src/cache.rs::OutlineCache::get_or_parse; src/mcp/mod.rs::tests::documents_direct_reads_do_not_parse; src/mcp/mod.rs::tests::documents_large_sources_keep_existing_search_and_grok_results
[^20]: src/lang/treesitter.rs::with_query and shared_query_* tests; src/search/callers.rs::find_callers_treesitter_batch; src/search/callees.rs::extract_callee_names_from_tree; src/search/siblings.rs::extract_sibling_references_from_tree; src/lang/go.rs::extract_go_receiver_name

_Source: Fork shared-document implementation and regression tests · Updated: 2026-10-01 · Supersedes: no historical upstream assessment_

### Coordinated resident document loads

Tilth coordinates overlapping requests for one resident path and disk revision through a shared standard-library `OnceLock`.
One initializer reads and parses the file. Waiters share its immutable result without holding the global cache mutex.
Loading and ready entries share the existing 500-entry bound; no separate loader registry exists.[^23]

Reservation compares the observed entry identity after checking disk freshness outside the mutex.
A competing replacement makes a reader retry. Failed initialization releases waiters and permits a later retry.
Failure cleanup removes only its own entry, never a newer replacement.[^24]

Invalidation, revision changes, and eviction can start another resident load.
The guarantee therefore does not mean one parse forever for a revision.
Structural search uses the retained ast-grep root directly; it does not construct a second candidate-file AST.

[^23]: src/cache.rs::OutlineCache::get_or_parse; src/cache.rs::tests::concurrent_misses_perform_one_real_read_and_parse; src/cache.rs::tests::loading_and_ready_entries_share_the_hard_capacity; src/cache.rs::tests::paused_real_read_does_not_block_an_independent_key
[^24]: src/cache.rs::OutlineCache::load_for_revision; src/cache.rs::OutlineCache::discard_if_entry; src/cache.rs::tests::stale_reader_reservation_cannot_replace_a_newer_entry; src/cache.rs::tests::stale_writer_publication_cannot_replace_a_newer_entry; src/cache.rs::tests::failed_initializer_cannot_remove_its_replacement_and_releases_waiters

_Source: Local coordinated-load implementation and regression tests · Updated: 2026-10-01 · Supersedes: uncoordinated concurrent misses in the preceding document layer_

Compiled queries and edit history have different validity rules from current parsed documents.
A file edit does not change a compiled language query.
Edit history retains older text and observed-line permissions, so a current-document cache cannot replace it by itself.

_Source: Owned-document layer merged onto main at face62a; src/cache.rs; src/lang/treesitter.rs; src/search/structural.rs; src/edit/snapshots.rs:30-37,126-173 · Updated: 2026-10-01_

### Verified incremental writes

Verified warm writes reuse parsed snapshots through a cloned tree and incremental parsing.
The cache checks exact old bytes, bounded on-disk new bytes, and disk revisions before publication.
Old readers retain their original source and tree. Parsing holds no global cache mutex.[^21]

Cold writes do not populate the parsed-document cache. External changes use full parsing.
Create, delete, and move operations invalidate affected paths, not unrelated snapshots.
A failed move still invalidates source bytes already committed before the rename failure.[^22]

[^21]: src/cache.rs::OutlineCache::update_after_write; src/lang/treesitter.rs::document_after_edit; src/mcp/mod.rs::tests::incremental_write_reuses_tree_through_production_requests
[^22]: src/mcp/tools/write.rs::tests::incremental_write_cold_noop_and_external_changes_do_not_reuse_stale_trees; src/mcp/tools/write.rs::tests::incremental_write_failed_move_invalidates_already_committed_source

_Source: Verified incremental-write implementation and regression tests · Updated: 2026-10-01 · Supersedes: no historical upstream assessment_

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

