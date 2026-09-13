# ADR: scoped Python import dependencies (#197 part A)

Date: 2026-09-12. Status: accepted (part A). Issue #197.

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
  file is **part B** and deliberately not done here.
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

The redb index schema is unchanged: `reconcile` stores only proven edges;
ambiguity is recomputed at query time, so no ambiguity state is persisted.

## Notes / limits

- The two engines share the resolver but neither uses the other's result as an
  oracle. Each is covered by its own MCP contract test
  (`tests/mcp_v2/test_python_deps.py`).
- Perf: `PyRoots::discover` runs per pass (reconcile) / per `tilth_deps` call.
  Cold monorepo reconciles may hit the 200 ms deps deadline and degrade to
  partial — best-effort by design, not incorrect.
- The `MAX_SUGGESTIONS` (8) per-file import cap is preserved from the unscoped
  resolver; a file importing >8 modules can lose edges beyond the cap.
