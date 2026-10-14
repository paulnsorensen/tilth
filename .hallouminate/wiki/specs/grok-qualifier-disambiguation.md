# Grok qualifier disambiguation (history)

Shipped in #61 (`2f7c448`, 2026-05-31). The grok half is gone: #297 (`66580d7`, 2026-10-02) retires `tilth_grok` on this fork.
The symbol-search half survives in stronger form: definition search has no nesting-depth limit.
This page is a decision record, not live guidance.

## Proposal

`grok` resolved `Alpha::dispatch` by removing the qualifier and taking the first-ranked `dispatch`.
With `Alpha::dispatch` and `Beta::dispatch` in scope, ranking order selected the owner, not the qualifier.
The PR #61 ambiguity test asserted only `other_def_count == 1`, so it passed with the bug.

The spec made the qualifier select the definition whose owner matches:

- Derive the owner from the tree-sitter outline parent. Rust `impl Foo` parents need the `impl` prefix and `<...>` generics removed.
- Derive the Go owner from the method receiver type, with a new query. `extract_go_receiver_name` returns the receiver variable, not the type.
- One owner match: return it. Many owner matches: return the first and count the others.
- Zero owner matches: return `NotFound` with a suggestion that names the real owners. Do not return the body of a different owner.
- Elixir `defimpl` stays out of scope, because its outline parent is the protocol.

Rejected alternative: return an ambiguity error for every qualified multi-match. That refuses to choose and does not honor the qualifier.

## Scope expansion (v2)

The cook phase stopped at 6 of 8 criteria.
`walk_for_definitions` in `src/search/symbol.rs` stopped at AST `depth > 3`, a limit from the initial commit with no rationale.
A TypeScript `export class` method sits at depth 4. A doubly nested Rust module function sits at depth 5.
Both became usages, not definitions, so grok had no candidate to match.
The user widened scope to remove the limit for every symbol search. #61 replaced it with a `depth > 64` guard.

## Current status

- `tilth_grok`, `src/search/grok.rs`, and the owner-match helpers do not exist. See the CLAUDE.md "Fork law" and `src/search/target.rs`.
- #300 (`8b67642`) moves the definition walk to ast-grep. `src/search/symbol.rs:341` notes that `dfs()` is iterative, so the walk has no depth guard.
- `pathological_nesting_finds_definitions_without_a_depth_cap` (`src/search/symbol.rs:1211`) finds a definition at AST depth 401.
- Qualifier-aware owner selection has no successor. `tilth_search` continuations resolve targets by path and line in `src/search/target.rs`.

## Lessons

- An ambiguity test must pin which candidate resolved, not only how many others exist.
- An arbitrary recursion limit can hide definitions. Search for the limit when nested definitions show as usages.

## Successor

None for owner matching. The depth fix lives on in `src/search/symbol.rs`.

_Source: former `.cheese/specs/grok-qualifier-disambiguation.md`; commits `2f7c448` (#61), `8b67642` (#300), `66580d7` (#297) · Updated: 2026-10-13_
