# lack435/tilth — sibling fork survey (2026-08-24)

Comparison of https://github.com/lack435/tilth against this fork, from a four-agent
git-history analysis. Pinned SHAs: base `e30dedc2` (our last common ancestor),
upstream jahala/tilth `ad9eb2cd`, lack435 main `81c3a72` (v0.9.0-lack.15), ours `ce95421`.

## Structure

- lack435/tilth is a **downstream fork of jahala/tilth** (same upstream as us). Its main
  contains **all of upstream** plus **204 original commits**, released as `v0.9.0-lack.N`.
- **Fork-law hazard: lack435's history contains both never-merge commits `399721c9` and
  `10bec56a`.** Never merge or pull the lack435 branch wholesale — cherry-pick only.
- Our fork was found essentially current with upstream (the `aaac6eb` sync + targeted ports
  cover nearly all of `e30dedc2..ad9eb2cd`); the residual gap is dependabot bumps plus small
  fixes (`2f596b0` fit_to_budget caller budget, `d791449` outline-preview suppression).

## lack435-only work we do NOT have (themes, ranked)

1. **C++ hardening campaign** — `src/lang/cpp_macro.rs` (~1200 lines): mask export/UE
   macros before parsing, CRLF misparse recovery, multi-declarator indexing, typedef/
   template/operator naming, #include resolution. Partly Unreal-Engine-calibrated.
   NOT cherry-pickable: our `src/lang/` split into per-language files; theirs stayed
   monolithic — any port is a re-implementation.
2. **BOM campaign** — `src/mcp/bom_surfaces.rs` + `tests/oracle.rs` (~2800 lines): every MCP
   surface audited for UTF-8/UTF-16 BOM leakage, with an oracle table pinning each
   surface's BOM contract.
3. **vcsignore** — hand-rolled git-equivalent ignore matching (~1500 lines, incl. p4).
   We use the `ignore` crate instead; their version needed 12+ review defects fixed.
4. **walkbudget + cancel** — bounded, interruptible walks; a timed-out request cancels its
   walk. Our `src/timeout.rs` is byte-identical to upstream's: timed-out workers run to
   completion, no walk-entry ceiling.
5. **retain.rs** — streaming bounded symbol-search retention (their fixture: 2.4M matches →
   1154MB RSS unbounded). We still use an early-quit cap in `symbol.rs`; possible live
   memory pathology on dense-match trees (unverified).
6. **Post-write parse check** — `edit_parse_check.rs` (510 lines): diffs pre/post-edit
   tree-sitter ERROR/MISSING node multisets, surfaces only edit-introduced errors.
   Decoupled from their hash-anchor edit model; portable onto our `edit::apply` path.
7. **Scope semantics** — refuse-bad-scope-instead-of-silently-widening (`19c235e`),
   file-path-as-scope (`b5e964c`). Our `resolve_scope()` still silently falls back to cwd
   on a missing scope and rejects file paths.
8. Smaller: smart-case/case-insensitive search, `|` multi-symbol separator, 1MB parse gate,
   multi-term false-zero warning, bloom cache byte-ceiling rewrite (+2100 lines),
   determinism fixes, leveldb C++ benchmark tasks.

## What we have that lack435 doesn't

Whole-file-tag edit model with 3-way-merge recovery (theirs: per-line u16 hash anchors),
required-`cwd` trust-absolute posture (theirs: upstream's optional `root`), `tilth_list`
(theirs: flat `tilth_files`), `tilth_search_v2` trial engine, benchmark statistical rigor.
They kept `tilth_savings`; we cut it deliberately (#121).

## Adoption guidance

Cheapest wins first: scope refuse-don't-widen (single function), post-write parse check,
symbol-retention bound (verify pathology first), walkbudget/cancel. C++ + BOM campaigns are
high value for C++-heavy use but are re-implementations, not ports.


## Ported 2026-08-24 (branch paulnsorensen/lack435-ports)

Re-implemented (never merged — lack435 history carries the never-merge commits):
parse budget (byte-based, 6 sites, 1MB gate), post-write parse check (advisory,
wired to whole-file-tag commit path), bounded retention (replaced our early-quit,
which was confirmed nondeterministic and under-counting), walkbudget + cancel
(per-walk entry windows after review), scope refuse-don't-widen + file-path-as-scope.
Review (2 HIGH, 4 MED, 4 LOW) all cured. Durable gotchas:

- `cancel::current()` is thread-ambient. Any walk running on a rayon pool thread
  must use `run_walk_with` with a token captured on the request thread —
  `symbol::search`'s rayon::join walks silently lost cancellation otherwise.
- The walk budget must be charged per walk (WalkWindow), not per request: one
  batched request legitimately runs dozens of walks.
- Any new tree-sitter parse site must route through `parse_budgeted`/
  `try_parse_budgeted` or be listed as exempt in parse_budget.rs's inventory.
- Deliberately NOT ported: vcsignore (we use the `ignore` crate), C++/BOM
  campaigns (re-implementations against our per-language src/lang/ layout).
- Open item: early-quit removal means full-tree scans per search; benchmark
  cost-per-correct-answer delta not yet measured.

