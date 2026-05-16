status: ok
next: age
artifact: .cheese/cook/mcp-27d-feedback.md
Added 4 integration hardening tests covering F1 first-line JSON parseability, F3 zero-match wiring (kind hint + glob-zero override), F5 stray-context-field tolerance; no defects exposed.

# Press Report — mcp-27d-feedback

## Orientation
Press added four integration-level hardening tests so the cooked F1 / F3 / F5 contracts have executable coverage at the `tool_search` seam, not just at the helper level. The cooked unit tests in `src/format.rs` and `src/mcp/iso.rs` already cover the leaf behaviour; press locked the wiring on top. F2 (prompt edits), F6 (prompt edit), and F7 (already shipped with an integration test from cook) needed no additional surface — the F7 ambiguous-fresh-region case is the new contract and is fully covered by `tool_write_auto_fix_shift_returns_fresh_region_not_relocation`.

## Checks run
- `cargo build`: pass.
- `cargo clippy -- -D warnings`: pass (no issues found).
- `cargo fmt --check`: pass.
- `cargo test`: pass — 457 across 3 suites (up from 453 after cook; +4 press tests).
- `cargo test --lib tool_search_first_line_is_parseable`: pass (1).
- `cargo test --lib tool_search_zero_matches_emits_empty_header`: pass (1).
- `cargo test --lib tool_search_glob_excludes_everything`: pass (1).
- `cargo test --lib tool_search_tolerates_stray_context`: pass (1).

## Findings
| Severity | Category | Evidence | Recommendation |
| --- | --- | --- | --- |
| low | wiring coverage | F3's `count_files_for_empty` walker is only exercised indirectly via the two integration tests above. | No fix — the two integration tests pin both branches (`files_matched_glob == 0` and `files_matched_glob > 0` with zero content hits). A direct unit test on `count_files_for_empty` would be redundant. |
| low | F7 contract scope | The body-moved auto-fix scenario emits a fresh region but the existing `tool_write_auto_fix_applies_on_single_match` test continues to cover the supported stale-hash-same-line case. | No action — together the two tests document both halves of the narrowed contract. |

## Coverage
- Spec coverage: F1 round-trips a parseable one-line JSON cache token; F3 emits the three counts plus per-kind hint for empty results, with the glob-zero override taking precedence over the kind hint; F5's silently-tolerated stray `context` field is locked. F7's tightened prompt is paired with `tool_write_auto_fix_shift_returns_fresh_region_not_relocation` from cook.
- Boundary coverage: glob-matches-nothing (`*.bogus_ext_does_not_exist`), kind-content-zero-hits, stale-hash-same-line (existing), shifted-body-no-relocation (cook).
- Assertion strength: each new test asserts on the literal output substring callers will pattern-match on, plus structural shape where it matters (JSON parse on the first line). No `assert!(out.is_empty())`-style placeholders.

## Readiness
ready for /age

## Next step
ready for /age:           /age mcp-27d-feedback     — review the cooked + pressed diff
