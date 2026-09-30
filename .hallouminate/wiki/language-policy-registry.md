# Language policy registry

LangSpec owns language-specific decisions used by shared code.
Generic consumers select policy fields or callbacks instead of comparing language identities.
This boundary preserves behavior across the 19 supported languages and 39 filename forms.[^1]

## Policy ownership

The language policy registry has one full Lang dispatch: `spec(lang)`.
Each language module defines its SPEC record.
Use data for syntax facts, limits, queries, ranking, and labels.
Use callbacks for language-specific parsing and predicates.
Reuse the existing defaults when behavior does not differ.[^2]

DefinitionOps owns definition recognition, name extraction, grouped-name lines, explicit owners, container recognition, labels, and weights.
C++ supplies explicit qualified ownership.
Go supplies receiver queries and grouped-declaration behavior.
The [semantic declaration spans ADR](adr/semantic-declaration-spans.md) covers canonical anchors and adornment ownership.[^3]

## Shared consumers

Shared consumers read LangSpec policy for lexer, imports, outline, search, and overview behavior.
The lexer reads its capabilities when it constructs the identifier iterator.
It does not resolve the registry for every scanned byte.[^4]

Scoped-import consumers use the existing `scoped_imports` capability.
The scoped Python resolver remains specialized.
Do not replace capability checks with Python identity checks.[^5]

Language-local syntax helpers may name their language.
Registry enumeration and test fixtures may name Lang variants.
Shared AST node-kind algorithms can remain shared.
StripFamily dispatch already selects behavior through LangSpec.
Do not add an abstraction for each grammar token.

## Filename ranking is not language detection

LangSpec preserves source filename ranking separately from language detection.
Search uses a separate extension-policy map built from SPEC records.
This prevents a refactor from adding detection aliases or changing basename eligibility.[^6]

JavaScript ranks `.mjs` and `.cjs` as source candidates without adding them to language detection.
Those forms remain outside the preferred basename-source set.
Case sensitivity, ranking ties, and fallback ordering remain unchanged.[^7]

Overview test detection also preserves legacy behavior.
Filename rules scan all code files, while inline markers use only the primary language.
Python-style filename labels can therefore describe a `test_` file with another extension.[^8]

## Verification boundary

LangSpec refactors preserve the existing public regression suite and private invariants.
Exact characterization tests cover search priorities, basename eligibility, ties, fallback ordering, and overview labels.
Use the [local gate guidance](local-gate-gotchas.md) for BDD migration and upstream regression rules.
Policy changes do not authorize new parsers, languages, or filename aliases.

[^1]: `src/types.rs:20-44`; `tests/bdd/features/languages.feature:1-69`.
[^2]: `src/lang/spec.rs:122-260,388-412`; per-language SPEC records.
[^3]: `src/lang/spec.rs:333-386`; `src/lang/cpp.rs:6-36`; `src/lang/go.rs:67-109`.
[^4]: `src/index/bloom.rs:180-203` (`IdentifierIter::new` after the main cache-freshness merge).
[^5]: `src/read/imports.rs:23-68`; `src/read/imports/python_scope.rs:281-299`.
[^6]: `src/lang/mod.rs:76-118`.
[^7]: `src/lang/javascript.rs:35-42`; `src/search/mod.rs` source-priority and basename characterization tests.
[^8]: `src/overview.rs` test_style and its characterization tests.

_Source: PR #283 LangSpec cleanup and approved behavior-preserving scope · Updated: 2026-09-30._
