# ADR: Semantic declaration spans

Date: 2026-09-19. Status: accepted.
Spec: `~/.local/share/cheese/paulnsorensen-tilth/specs/semantic-declaration-spans.md`.

## Context

Tree-sitter declaration nodes can begin before their canonical declaration line. Python decorators, Rust attributes, JVM annotations, and Elixir metadata are examples.

One line cannot represent both stable symbol identity and complete semantic ownership. Using the node start for both made identity language-dependent. Using the keyword line for both omitted leading syntax from ranges.

## Decision

`OutlineEntry.start_line` remains the canonical display and search anchor. `OutlineEntry.span_start_line` begins the semantic ownership range. `end_line` closes that range.

Each `LangSpec` owns canonical-anchor, transparent-wrapper, and leading-adornment policies. Shared outline traversal applies those policies and requires adornments to be contiguous.

Python decorated definitions unwrap to the inner declaration. A continued `async` header remains anchored on the `def` line while its semantic span includes decorators.

Range consumers use `span_start_line..=end_line`. Identity, output headings, and continuation keys use `start_line`. See `src/types.rs:167-175` and `src/lang/outline.rs:94-219`.

Search reconciliation requires both the definition name and semantic containment. It selects the deepest matching entry and never adopts an unrelated enclosing declaration. See `src/search/symbol.rs:326-348`.

One parsed tree supplies the shallow outline and an exact deep-definition lookup. This avoids a second parse on path-line resolution. See `src/lang/outline.rs:1071-1122`.

Deep symbol and block lookups convert sibling groups from the same parsed tree. This preserves attached syntax beyond the display outline's nesting cap.

Diff changes carry separate old and new semantic ranges. Removed lines use the old range. Added and context lines use the new range.

Embedded declaration ranges include the complete contiguous header prefix through the canonical token. This includes multiline modifiers and return types, not only annotations. Blank rows and parsed comments split ownership, including inline comments on an adornment row.

Production block edits build one flattened, deepest-first semantic index from one parse. Symbol and line anchors share that index, including declarations beyond the display outline depth cap.

Deep indexes suppress an inner declaration only through its actual transparent-wrapper parent. They never globally deduplicate by name or line, because distinct nested declarations can share both.

Diff attribution keys buckets by `SymbolAttributionKey`, which combines `SymbolIdentity`, the canonical line, and side-specific spans. This distinguishes overload occurrences. Detailed lines retain exact old-side and new-side source coordinates.

## Rejected alternatives

- Keep the Python-only grok fallback. This leaves the same defect in search, diff, edit blocks, and other languages.
- Replace `start_line` with the earliest syntax line. This destabilizes symbol identity and user-facing declaration anchors.
- Treat every preceding comment as ownership syntax. This crosses unrelated comments and blank separators.
- Reparse for each deep lookup. This adds latency on the interactive MCP path.

## Consequences

New range consumers must choose the semantic span explicitly. New language grammars must define policies only when their AST shape needs them.

Regression coverage includes decorated and continued Python definitions, Rust attributes, Elixir metadata, annotation-bearing languages, diff attribution, and qualified owners.
