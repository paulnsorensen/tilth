status: ok
next: done
artifact: (no prior press / cure report — first review of this stack)
Externalises six MCP tool descriptions to `prompts/tools/*.md` (PR #24) and refines two of them plus one schema desc with default-steering prose and hash-prefix documentation (PR #25).

# Age Report — pr-stack-23-24-25

## Orientation

Stack of two commits on top of `origin/main`:

- `64f72d8` (PR #24, base) — `refactor(mcp): externalize tool descriptions to prompts/tools/*.md`. Replaces six inline string literals in `src/mcp/tools/definitions.rs` with `include_str!`-backed consts pointing at new `prompts/tools/{deps,diff,list,read,search,write}.md`. Each consumer site calls `.trim_end()` so the source file can keep its final newline without affecting wire bytes.
- `3a9922b` (PR #25, top) — `docs(mcp): steer toward calibrated defaults; document hash prefix format`. Edits prose in `prompts/tools/read.md` and `prompts/tools/search.md`, and one schema-arg description in `src/mcp/tools/definitions.rs:80` (`tilth_read.mode`).

Scope is small: 7 files, +57 / −8, no Rust logic touched beyond moving strings.

## Parity check (PR #24)

Byte-compared each of the six externalised `prompts/tools/*.md` files (after applying `.trim_end()` semantics) against the corresponding inline string literal in `origin/main:src/mcp/tools/definitions.rs`. All six are identical:

- `deps.md` IDENTICAL
- `diff.md` IDENTICAL
- `list.md` IDENTICAL
- `read.md` IDENTICAL (matches the prior `read_desc` local)
- `search.md` IDENTICAL
- `write.md` IDENTICAL (including the `\n` escape sequences inside the embedded JSON example, which decode to the literal two-char `\n` in both forms)

The refactor is a true no-op on wire bytes when no further edits are made.

## High-stake findings

None.

## Medium-stake findings

- **[assertions]** `src/mcp/tools/definitions.rs:6-15` — the parity contract between the inline strings and the new `prompts/tools/*.md` files is not test-pinned. Today the round-trip is byte-identical, but `include_str!` + `.trim_end()` is sensitive to: a stray trailing space, a hidden BOM, a CRLF added by an editor, or accidental edits to one of the six files. None of those would fail the build, none would fail clippy, and none would surface in the existing in-source `#[cfg(test)]` modules. A single golden-snapshot test (e.g. assert each `tool_definitions(true)` `description` field against a frozen `.snap` or against a baked-in constant) would catch silent prompt drift, which is exactly what PR #25 demonstrates is going to happen routinely. Recommendation: add a snapshot test (or even an integration test that diffs `tool_definitions(true)` output between two builds) so prompt edits stay deliberate.

## Low / informational

- **[complexity / doc-precision]** `src/mcp/tools/definitions.rs:5` — the new module doc says "Trailing whitespace is stripped at load time so source files can keep their final newline without affecting wire bytes." `include_str!` is resolved at *compile time* and `.trim_end()` runs on every call to `tool_definitions()`, not at "load time". Minor wording nit; semantics are correct. Recommendation: reword to "stripped before serialisation" or "at call time".
- **[deslop / duplication]** PR #25 adds the steering sentence "Defaults to `auto`" in two places: `prompts/tools/read.md:1` (tool-level description) and the `mode` arg description at `src/mcp/tools/definitions.rs:80`. The two surfaces serve different roles in the JSON-RPC payload (top-level `description` vs `inputSchema.properties.mode.description`), so the duplication is intentional. Flagging only as a future drift risk: an edit to one and not the other will silently get out of sync. No fix recommended now.
- **[encapsulation]** PR #24 moves agent-facing prose out of the Rust source. The new `prompts/tools/` directory is **not** referenced by `scripts/regen-agents-md.sh` (which only concatenates `prompts/mcp-base.md` + `prompts/mcp-edit.md` into `AGENTS.md`), and is **not** packaged by `npm/package.json` (`files: [install.js, run.js, README.md]` — npm ships pre-built binaries). Both are correct: tool descriptions reach agents via the JSON-RPC `tools/list` response baked into the compiled binary, not via repo-root prompt files. No action needed. Confirms the refactor's stated rationale ("simplifies versioning for hosts that read prompts directly") is forward-looking, not a current consumer.

## Empty dimensions

- correctness — no logic changed; parity is byte-identical.
- security — no I/O, parsing, or trust-boundary changes.
- spec — no spec attached to this stack.
- nih — `include_str!` and `.trim_end()` are stdlib; no reinvention.
- efficiency — `.trim_end()` per call is a `&str` slice; negligible.

## Confidence

`certain` — diff is small and entirely text. Parity was verified by reconstructing the post-`.trim_end()` runtime strings from the six new `.md` files and byte-comparing against the prior inline literals (after Rust single-level unescape) at `origin/main:src/mcp/tools/definitions.rs`; all six matched. No evidence sources were unavailable.

## Next step

No `AskUserQuestion` prompt — caller requested a read-only run with no handoff. One medium-stake finding (snapshot test for description parity) and three low/informational notes; nothing approaches high-stake.
