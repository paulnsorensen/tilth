---
status: trusted
last_verified: 2026-08-14
confidence: high
sources:
  - /home/paul/.local/share/cheese/paulnsorensen-tilth/specs/tilth-search-v2-roadmap.md
  - .cheese/research/code-review-graph-indexing/code-review-graph-indexing.md
  - .cheese/research/lazy-incremental-code-indexing/lazy-incremental-code-indexing.md
  - src/mcp/mod.rs:23-60
---
# Persistent Per-client Dependency Index

Dependency enrichment uses persistent per-file redb shards under the XDG cache, isolated by MCP client profile and Git worktree identity. This is provisional until a focused redb spike passes; stale edges are never returned.

## Context

Exact dependency analysis currently rescans source and does not give search a restart-persistent reverse index. Tilth MCP is a per-client stdio process, not a shared daemon (`src/mcp/mod.rs:23-60` and the server loop). A single writable redb database cannot safely be shared by independently launched Claude, Codex, and OMP processes.

## Decision

- Store one database at `$XDG_CACHE_HOME/tilth/deps/<worktree-key>/<client-key>.redb`.
- Derive the client key from normalized MCP `clientInfo.name`.
- Derive the worktree key from canonical Git top-level plus absolute Git dir, never the shared common dir.
- Persist per-file outgoing import/call/symbol edges, content fingerprints, and reverse indexes.
- Reconcile from the last usable Git HEAD; keep dirty/untracked paths volatile until clean; handle rename/delete and missing anchors.
- Replace one file shard and its reverse edges atomically.
- Stop refresh/traversal cooperatively at an internal sub-deadline.
- Return core search plus verified-only partial impact, coverage counts, and a typed continuation when refresh is incomplete, locked, corrupt, or timed out.

## Spike gate

The concrete redb spike must cover atomic replacement, concurrent readers/serialized writer, same-profile process locking, restart reuse, linked worktrees, branch switching, dirty-to-revert, rename/delete, missing anchors, integrity recovery, churn/compaction, file size, stale-cache cleanup, and cold/warm latency. A failed gate reopens backend selection. No speculative store trait is introduced.

## Alternatives rejected

- **Shared redb file:** violates redb's process-lock model.
- **Shared SQLite/WAL:** viable, but abandons the selected pure-Rust shard experiment before measuring it.
- **New index daemon:** adds a service protocol, lifecycle, authentication boundary, and failure recovery beyond this roadmap.
- **In-memory-only cache:** loses restart reuse and repeated exact-search enrichment.
- **Stale-with-warning:** weakens correctness; callers may act on removed edges.

## Consequences

Indexes are duplicated across harnesses and worktrees. This spends disk to preserve isolation and zero daemon operations. Removed/moved worktrees require bounded cache garbage collection. Search correctness remains independent of index availability.



## Freshness implementation audit: 2026-09-29

The roadmap's content-fingerprint wording is an intended design, not the initial implementation.
Revision `7005d6ab912843456dec498266fc3644fbcd4ea4` stores only mtime and length.
Reconciliation and impact verification trust matching signatures.
Equal-length edits that restore mtime can therefore retain stale dependency edges.[^freshness-before]

The authorized fix uses a per-file revision with richer Unix change metadata.
It preserves cheap warm scans and selective shard replacement.
Platforms without that metadata use a content fingerprint.
Legacy signatures remain readable but cannot count as fresh revisions.[^freshness-fix]

This approach does not establish a transactional repository snapshot.
Filesystems with coarse or unreliable change metadata remain a stated limitation.
Do not describe metadata equality as a universal proof of content equality.
Compiled grammar/query caches do not depend on file contents and need no edit-triggered flush.[^freshness-limits]

[^freshness-before]: https://github.com/paulnsorensen/tilth/blob/7005d6ab912843456dec498266fc3644fbcd4ea4/src/index/deps/storage.rs#L34-L70; https://github.com/paulnsorensen/tilth/blob/7005d6ab912843456dec498266fc3644fbcd4ea4/src/index/deps/mod.rs#L555-L590
[^freshness-fix]: src/util.rs::FileRevision; src/index/deps/storage.rs::signature_of; src/index/deps/mod.rs::reconcile; implementation contract: /home/paul/.local/share/cheese/paulnsorensen-tilth/research/tilth-search-ripgrep-tree-sitter/cache-freshness-contract.md
[^freshness-limits]: src/util.rs::FileRevision; src/search/callee_query.rs::with_callee_query; src/lang/treesitter.rs::with_query; https://doc.rust-lang.org/std/os/unix/fs/trait.MetadataExt.html



File revisions represent modification times as signed nanoseconds, including pre-1970 timestamps.
Revision creation and mtime comparison use the same conversion.
Rejecting negative durations prevents valid source files from entering the parsed cache.
Regression tests cover fractional pre-epoch times, warm reuse, and restored-mtime invalidation.[^pre-epoch]

[^pre-epoch]: src/util.rs:19-37,165-174; src/cache.rs:236-269. Verified by `python3 scripts/verify.py` on 2026-09-30: 1208 library tests, 4 binary tests, 67 MCP tests, 6 script tests, and 77 bash-guard checks pass.

_Source: cache-freshness publication review and signed-timestamp regression tests · Updated: 2026-09-30_

