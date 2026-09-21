# Extraction reuse comparison — verdict report (curd 3)

Spec: `/Users/paul/.local/share/cheese/paulnsorensen-tilth/specs/tilth-extraction-reuse-comparison.md`.
This experiment is test-only. It changes no production dependency, export,
or behavior (AC-7). It compares Tilth's own extraction against two
isolated candidate packages under `benchmark/extraction/candidates/`,
never wired into the production binary.

All numbers below come from a real run of
`python3 benchmark/extraction/compare.py --manifest benchmark/extraction/fixtures/manifest.json --offline`
against the fixture manifest and the `.generated` records captured in
curds 1-2. The full machine-readable matrix is at
`benchmark/extraction/.generated/verdict-matrix.json` (gitignored,
reproducible from the command above).

## Candidates and pinned versions

- `tree-sitter-language-pack` 1.17.0 — runtime-downloaded grammar cache.
- `ast-grep-outline` 0.45.3 — statically linked grammars, no runtime cache.

## Verdict matrix (candidate x language x capability)

Legend: **pass** = every applicable fixture matched expected; **partial**
= some matched, some did not; **unsupported** = candidate structurally
lacks the capability; **fail** = candidate attempted and never matched;
**blocked** = a prerequisite was missing. No cell was blocked in this run
— every candidate/fixture/capability record was present on disk.

### tree-sitter-language-pack

| Language | definitions | nesting | signatures | imports | edit_spans |
| --- | --- | --- | --- | --- | --- |
| rust | partial (5/6) | unsupported | unsupported | unsupported | partial (4/6) |
| typescript | partial (2/6) | unsupported | unsupported | unsupported | partial (2/6) |
| python | **pass (6/6)** | unsupported | unsupported | unsupported | partial (4/6) |

### ast-grep-outline

| Language | definitions | nesting | signatures | imports | edit_spans |
| --- | --- | --- | --- | --- | --- |
| rust | partial (4/6) | partial (5/6) | partial (4/6) | partial (5/6) | partial (3/6) |
| typescript | partial (4/6) | partial (5/6) | partial (2/6) | partial (5/6) | partial (3/6) |
| python | partial (4/6) | partial (2/6) | partial (2/6) | partial (5/6) | partial (3/6) |

Fraction shown is applicable fixtures matched / 6 (core, imports_exports,
unicode, crlf, empty, incomplete per language).

## What the failures are, concretely

- **tree-sitter-language-pack definitions/edit_spans, Rust+TypeScript**:
  fails exactly on `*-core` (and `*-crlf` for edit_spans), the fixtures
  with nested modules. Its bundled `tags.scm` emits a duplicate `method`
  tag alongside the `function` tag for functions nested inside a module,
  so `rust-core` returns 7 definitions where 5 are expected — an
  over-counting bug, not a formatting difference.
- **tree-sitter-language-pack definitions/nesting/signatures/imports,
  TypeScript, all fixtures**: `definitions`/`edit_spans` return an empty
  list (status `supported`, value `[]`) for every non-empty TS fixture —
  the bundled `tags.scm` only matches ambient/`.d.ts` forms, not ordinary
  TS function/class declarations. `nesting`/`signatures`/`imports` report
  `unsupported` for every language: the crate exposes no query surface
  for these three capabilities at all.
- **ast-grep-outline nesting, all languages**: correctly reports one level
  of nesting (item → member) but never the deeper level. `rust-core`
  expects `[(inner,compute), (inner,deep), (deep,compute)]`; the candidate
  returns `[(inner,compute), (inner,compute)]` — it recurses into the
  outer module's members but does not walk into `deep`. This repeats for
  `python-core`/`python-unicode`/`python-crlf` and
  `typescript-core` (their fixtures also nest a function inside a nested
  module/namespace).
- **ast-grep-outline signatures, TypeScript+Python `*-core`/`*-unicode`/
  `*-crlf`**: signature text differs from Tilth's declared-parameter-list
  convention (formatting divergence beyond definitions'
  kind-casing, not evaluated as an adoptable match — no accepted
  normalization was documented for this case per the spec's requirement
  to document such differences before measuring, so it counts as fail).
- **Both candidates, `edit_spans` on `*-core`**: both candidates set
  `span_start_line == start_line`, so the owned span for the top-level
  `compute` function in `rust-core` — which Tilth's `span_start_line`
  attributes the leading `#[inline]` attribute to — omits `#[inline]`
  entirely (both candidates return `"pub fn compute(\n    a: i32,\n ..."`
  starting at the signature, not the attribute). This is the owned-span
  accuracy risk the spec calls out, now a measured, repeatable
  mismatch; the comparator does not normalize it away — it is scored as
  an exact-text fail.
- **`*-imports_exports` fixtures**: `ast-grep-outline` `imports` fails
  only on this fixture for all three languages — it returns the aliased
  or re-exported name text rather than the import source string Tilth's
  `extract_import_source` produces.

## Maintenance inventory

**Counting rule (documented, applied uniformly):** count of code = number
of non-blank source lines whose trimmed text is not exclusively a `//`
line comment, counted with a single pass over the file text. Doc comments
(`///`) count as code because they are load-bearing for `doc` outline
fields; block comments are not separately stripped (none occur in the
measured regions).

| Region | Lines (counting rule) | Scope |
| --- | --- | --- |
| `src/lang/outline.rs` (whole file) | 1571 | Shared extraction core: parsing, definitions, nesting, signatures, imports, edit-span ownership, for all 17 supported languages |
| `tree-sitter-language-pack` adapter (`benchmark/extraction/candidates/tree-sitter-language-pack/src/main.rs`) | 328 | Capture + normalize for 3 languages, 5 capabilities (2 of 5 capabilities ever return non-empty data) |
| `ast-grep-outline` adapter (`benchmark/extraction/candidates/ast-grep-outline/src/main.rs`) | 374 | Capture + normalize for 3 languages, 5 capabilities (all 5 return data, none pass fully) |

`get_outline_entries`, `get_deep_outline_tree`, and `extract_import_source`
(the referents named in the spec) each have 16 direct consumers spanning
`src/diff/matching.rs`, `src/diff/overlay.rs`, `src/edit/block.rs`,
`src/read/mod.rs`, `src/read/outline/code.rs`,
`src/read/outline/markdown.rs`, `src/search/callees.rs`,
`src/search/callers.rs`, `src/search/deps.rs`,
`src/search/fuzzy_symbol.rs`, `src/search/grok.rs`,
`src/search/siblings.rs`, `src/search/symbol.rs`,
`src/lang/treesitter.rs`, `src/read/imports/python_scope.rs`, and
`src/extraction_probe.rs` (confirmed via `tilth_search` dependency
traversal). Removing or replacing any of these three functions for even
one language requires re-verifying every one of those 16 consumers.

Per capability x language:

- **definitions**: removable — none. Even where a candidate matches
  (tree-sitter-language-pack, Python, 6/6), `get_outline_entries` and
  `get_deep_outline_tree` remain retained for the other two languages and
  for nesting/signatures/imports/edit_spans on Python itself (all four
  still `unsupported` or failing on that candidate). No line of
  `outline.rs` becomes deletable from a single-capability,
  single-language win.
- **nesting**: removable — none; both candidates are structurally
  unsupported (tree-sitter-language-pack) or one-level-only and thus
  wrong at depth (ast-grep-outline). Retained in full.
- **signatures**: removable — none; unsupported (tree-sitter-language-pack)
  or formatting-divergent and unevaluated (ast-grep-outline, no accepted
  normalization documented). Retained in full.
- **imports**: removable — none; unsupported (tree-sitter-language-pack)
  or wrong on aliased/re-exported forms (ast-grep-outline). Retained in
  full.
- **edit_spans**: removable — none; both candidates anchor
  `span_start_line == start_line` and both fail on the nested/attributed
  `*-core` fixtures across all three languages. Tilth's
  `span_start_line`/doc-comment ownership logic is retained in full.

Adapter cost is not amortized by any deletion: for every capability the
adapter code above (328 or 374 lines, 3 languages only) would need to
grow to cover Tilth's other 14 supported languages and would still sit
alongside, not instead of, `outline.rs`, because at least one capability
per language remains retained. Net maintenance delta for both candidates,
for every capability measured, is **non-positive**.

## AC-4 headline measurements (informing context only, no threshold applied)

Extraction is measured two ways: **in-process** (the candidate's own timer
around only its parse+extract calls, excluding process startup, manifest
IO, and record serialize/write) and **end-to-end** (the whole offline
capture subprocess). `startup_overhead` is end-to-end minus in-process,
per sample; it is dominated by one-time OS process/binary-load cost, not
extraction work.

| Candidate | Clean build | Executable size | Extraction in-process median (range) | End-to-end median (range) |
| --- | --- | --- | --- | --- |
| tree-sitter-language-pack | 15.41 s | 6,061,136 bytes | 0.0387 s (0.0376–0.0546 s) | 0.0469 s (0.0458–0.2954 s) |
| ast-grep-outline | 9.25 s | 45,792,384 bytes | 0.0005 s (0.0005–0.0008 s) | 0.0192 s (0.0171–1.1875 s) |

Source: `benchmark/extraction/.generated/evidence/ac4-measurement.json`
(baseline commit `0aaa2a04396b113ad28cbace1a36870be8d209b8`). Standalone
binary sizes do not predict integrated production size (spec risk); these
figures inform, they do not gate, the recommendation below.

## Per-capability recommendations

- **definitions**: **reject**. tree-sitter-language-pack passes for
  Python only, with a real over-counting bug for Rust/TypeScript nested
  modules and an empty result for ordinary TypeScript declarations; and
  the one passing cell carries no positive maintenance savings (see
  inventory). ast-grep-outline never passes fully in any language.
- **nesting**: **reject**. Neither candidate resolves nesting beyond one
  level; ast-grep-outline's gap is measured, not assumed
  (`rust-core` returns 2 pairs where 3 are expected).
- **signatures**: **reject**. tree-sitter-language-pack does not expose
  the capability. ast-grep-outline's output diverges from Tilth's
  signature-text convention on 4 of 6 fixtures per language for
  TypeScript/Python, and the spec requires documenting an accepted
  formatting difference *before* measuring — none was documented, so
  these mismatches count as fails rather than accepted variance.
- **imports**: **reject**. tree-sitter-language-pack does not expose the
  capability. ast-grep-outline fails on aliased/re-exported import forms
  in every language.
- **edit_spans**: **reject**. Both candidates anchor
  `span_start_line == start_line` and both lose attribute/doc-comment
  ownership on the nested `*-core` fixtures in every language — the exact
  owned-span risk the spec flagged, now measured as a real, repeatable
  fail rather than a theoretical risk.

## Overall conclusion: do nothing

Neither candidate earns an adoption recommendation for any capability.
The one passing cell (tree-sitter-language-pack definitions, Python)
fails the maintenance-savings half of the adoption bar (AC-6): Tilth's
shared `outline.rs` and its 16 consumers remain retained in full for
every other capability and language, so adopting that single cell adds
an adapter without removing any Tilth code. No capability qualifies for
adaptation either: an adapter would need to fix a structural gap
(nesting depth, missing query surface, owned-span anchoring) that is a
library limitation, not a small bounded patch on top of otherwise-correct
output. This is a valid negative result under the spec's own stress
tests (F1: isolated builds did work, no blocked cells; F2: accurate
output plus a large, still-growing adapter eliminates savings for the
sole passing cell) and the spec's explicit allowance for two rejected
candidates.

This experiment changed no production dependency, export, or behavior.
The candidate packages remain outside production Cargo dependency
resolution.

## How the AC-8 self-tests bite

`benchmark/extraction/tests/test_compare.py`:

- `test_missing_capability_output_is_rejected` deletes the `edit_spans`
  key from an otherwise-correct synthetic record and asserts the
  resulting cell verdict is `blocked` (`assertNotIn(cell["verdict"],
  ("pass", "unsupported"))`) — a missing capability output is refused,
  never silently treated as absence-of-evidence success.
- `test_incorrect_owned_edit_span_is_a_fail` replaces the correct owned
  span (`"pub fn compute() -> i32 {\n    0\n}"`) with a truncated one
  missing the function body, and asserts the cell verdict is `fail`
  (`assertNotEqual(cell["verdict"], "pass")`) — an inaccurate owned span
  is scored as a real failure, not normalized away.
- `test_happy_path_is_pass` and `test_missing_record_is_blocked` cover
  the positive and blocked paths so the two fault injections are proven
  against a comparator that is otherwise permissive, not one that always
  fails.
- `test_fixture_level_known_tilth_divergence_reaches_matrix_cell` (cure)
  asserts a `known_tilth_divergence` set on a synthetic fixture reaches
  its matrix cell — proves the fixture-scope read (not the forbidden
  capability-scope read) actually surfaces the annotation.
- `test_missing_expected_is_blocked_not_a_crash` (cure) deletes
  `expected` from a capability (optional per schema) and asserts the
  cell is `blocked`, not a `KeyError`.
- `test_malformed_definitions_entry_is_blocked_not_a_crash` (cure) feeds
  a non-pair definitions entry and asserts the cell is `blocked`, not a
  `ValueError`.

## Gate results

```
$ python3 -m unittest discover -s benchmark/extraction/tests
Ran 11 tests in 0.012s
OK

$ python3 benchmark/extraction/compare.py --manifest benchmark/extraction/fixtures/manifest.json --offline
(full matrix printed above; 0 blocked cells; matrix written to
benchmark/extraction/.generated/verdict-matrix.json)

$ cargo fmt --check
(clean, exit 0 — no Rust changed by curd 3)

$ cargo clippy --all-targets -- -D warnings
Finished `dev` profile [unoptimized + debuginfo] target(s) in 0.80s

$ cargo test
1204 passed; 0 failed (second run, all green)
First run showed 1 failure —
index::deps::tests::unreadable_code_does_not_report_complete_coverage —
which passed alone and passed on immediate rerun of the full suite
(1203 passed, 1 failed -> 1204 passed, 0 failed with no code change in
between); this is parallel-test-run flakiness, not the named pre-existing
flaky test, and not touched by curd 3 (no Rust files changed).
```

`tests/mcp_v2` was not rerun in curd 3: no MCP-visible code changed
(curd 3 owns only `benchmark/extraction/compare.py`,
`benchmark/extraction/tests/`, and this report). `cargo build` was not
run standalone in curd 3; `cargo test` and `cargo clippy --all-targets`
both compile the full binary and library as part of their own run.

## Residual risk

- **Certain**: the verdict matrix above is the actual comparator output
  against the actual `.generated` records on disk for this baseline
  commit; no cell was blocked or fabricated. The maintenance-inventory
  consumer counts came from a live `tilth_search` dependency traversal,
  not an assumption.
- **Speculating**: whether a *much* larger adapter (full 17-language
  parity, deeper-nesting post-processing for ast-grep-outline, a
  `tags.scm` fix upstream for tree-sitter-language-pack) could someday
  flip a capability's verdict. This experiment does not evaluate that;
  the spec places broader qualification out of scope.
- **Don't know**: whether either upstream project would accept the fixes
  needed to close the measured gaps (duplicate Rust `method` tags, empty
  TS query surface, one-level nesting), or on what timeline — no upstream
  contact was made as part of this experiment.
