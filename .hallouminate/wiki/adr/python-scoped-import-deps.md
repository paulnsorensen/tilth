# ADR: scoped Python import dependencies (#197 part A)

Date: 2026-09-12. Status: accepted (parts A-D). Issue #197. Parts B ("follow
Python `__init__` re-exports to the defining file", PR #263) and C ("surface
Python import uncertainty and drop the 8-edge cap", PR #267) are merged; part
D (this section) covers warm-reconcile invalidation of those re-export
chains and is below under **Part D**.

## Context

Both dependency engines missed import-only consumers across src-layout Python
packages. `resolve_python` (`src/read/imports.rs`) only followed leading-dot
(relative) imports and classified every absolute `from package.module import X`
/ `import package.module` as external (`is_external`, Python arm). So a consumer
`from producer.ingest.rankings import RankingEntry` produced no edge:

- `tilth_deps` (`search::deps::analyze_deps`) found reverse dependents only
  through call expressions (`find_callers_batch`), never through import bindings,
  so an importer that never *calls* the symbol was invisible.
- `fetch_dependencies` (the persistent redb index; `src/index/deps/mod.rs`,
  reached via `search_v2/continuations.rs`) builds reverse edges from
  `resolve_related_files_with_content`, which inherited the same relative-only
  limitation, and falsely reported `coverage: complete`.

## Decision

One shared scoped resolver, `src/read/imports/python_scope.rs`, used by both
engines. The unscoped `resolve_related_files_with_content` stays as-is for
overview and callee callers.

- **Package roots** are discovered under an **explicit request/worktree scope**,
  never inferred from an absolute target path. Supported layouts: `<scope>/src`,
  `<scope>/<project>/src`, `<scope>/packages/<project>/src`. Root discovery and
  the reverse walk honor `search::skip_dir_entry`, so `.venv`/nested checkouts
  never contribute.
- **Resolution** maps `a.b.c` to `<root>/a/b/c.py` or `<root>/a/b/c/__init__.py`.
  Import statements are read from the tree-sitter AST (handles aliases and
  parenthesized from-imports); relative imports still route through the existing
  dot-resolver unchanged.
- **`from pkg import X` resolves the module `pkg`**, not `X`. So a barrel import
  depends on `pkg/__init__.py` (part A); re-export propagation to the originating
  file (part B) is done, and `PyResolution` now carries the forward-import
  uncertainty needed to surface it rather than guess (part C, see below).
- **Same-name safety**: resolution is by module path, so `from other.rankings
  import RankingEntry` never counts as a dependent of `producer/.../rankings.py`.

### Ambiguity (duplicate package roots)

When a module name maps to more than one in-scope file, the edge is **omitted,
never guessed**. The engines surface it via one query-time check,
`python_scope::target_ambiguity(target, scope)`, which is symmetric: a consumer's
import of module M is ambiguous w.r.t. the target exactly when the target's own
module identity has >1 candidate.

- `tilth_deps`: returns `isError: true` — `TilthError::AmbiguousModule { module,
  candidates }` — carrying the module and bounded candidate paths.
- `fetch_dependencies`: returns `coverage/completeness/status: partial` (not
  `timed_out`); proven edges are kept, ambiguous ones omitted.

Ambiguity is recomputed at query time, so no ambiguity state is persisted —
that part of "schema unchanged" still holds. Part D below adds different,
invalidation-only persisted state (never ambiguity-related).

## Notes / limits

- The two engines share the resolver but neither uses the other's result as an
  oracle. Each is covered by its own MCP contract test
  (`tests/mcp_v2/test_python_deps.py`).
- Perf: `PyRoots::discover` runs per pass (reconcile) / per `tilth_deps` call.
  Cold monorepo reconciles may hit the deps deadline (`DEPS_WARM_DEADLINE`,
  `src/mcp/tools/search_v2/continuations.rs`) and degrade to partial —
  best-effort by design, not incorrect.
- The `MAX_SUGGESTIONS` (8) per-file cap is gone from the scoped Python path
  (part C, PR #267, dropped it from `resolve_python_edges`'s graph
  collection); it still applies to the unscoped resolver's own post-read hint
  suggestions (`src/read/imports.rs`), which is a separate, legitimate
  presentation limit, not a graph-collection cap.

## Part D — warm-reconcile invalidation of named re-export chains (#197 part D)

Part A/B resolve a named re-export's *forward* edges to `[direct module,
final owner]` only — deliberately lean, never every intermediate
`__init__.py` hop in a multi-hop chain. That's still true for what
`impact`/`tilth_deps` present. But the persistent index's `reconcile`
(`src/index/deps/mod.rs`) needs to know about those intermediate hops
anyway, for one purpose only: knowing which consumer to rescan when a hop
edits, is deleted, or redirects a name to a different leaf.

- `PyResolution` (`src/read/imports/python_scope.rs`) gained
  `reexport_hops: Vec<PathBuf>` — every intermediate `__init__.py` visited
  while resolving a named re-export, recorded regardless of whether the
  chain ultimately resolves (so an edit that makes a previously-broken chain
  newly resolvable is also caught, not just a successful-to-successful
  redirect). This is invalidation-only state; it is never read by `impact`
  or any MCP-facing path.
- `storage::FileShard` gained a matching `reexport_hops` field
  (`#[serde(default)]`, so a pre-existing on-disk shard without it
  deserializes as empty rather than poisoning the whole index), plus two new
  redb tables: `reexport_hop_reverse` (hop path → consumers whose
  resolution passed through it) and `pending_rescan` (a single persisted
  list of rels a deadline-cut invalidation pass couldn't reach yet).
- `reconcile` invalidates via one bounded worklist
  (`reexport_worklist`/`drain_worklist`): a changed or deleted
  `__init__.py` forces a rescan of every importer that stored it as a
  direct/owner edge *or* an intermediate hop — never an unbounded/recursive
  transitive walk. A partial pass persists whatever the worklist couldn't
  reach as `pending_rescan`, so the *next* reconcile resumes exactly there
  even though the triggering file's own signature is already committed and
  no longer looks "changed." The deadline is now checked across the walk,
  the invalidation worklist, the forced rebuild, and the write stage, not
  the walk alone.
- `DEPS_WARM_DEADLINE` (`src/mcp/tools/search_v2/continuations.rs`) moved
  from 200ms to 500ms: the new tables/fields add a small, correctness-required
  constant cost per warm pass, which occasionally pushed a cold reconcile of
  a larger repo past the old budget under load.
- Known residual, not addressed here: `rescan_shard` returning `None` on a
  transient read failure (as opposed to a genuine deletion) silently drops
  that rel from the pass without setting a `failed` flag, unlike the main
  walk loop. This is inherited unchanged from the pre-part-D forced-rescan
  code; a follow-up could align it with the walk's `failed` handling.
