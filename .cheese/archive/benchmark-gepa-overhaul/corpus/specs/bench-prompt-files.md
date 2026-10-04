---
slug: bench-prompt-files
status: draft
source: mold-curd-mini-spec
created: 2026-10-04
confidence: high
intent: Move the tilth_search and tilth_write tool descriptions out of inline string literals in src/mcp/tools/definitions.rs into prompts/tools/search.md and prompts/tools/write.md, loaded through include_str! like prompts/tools/read.md, with byte-identical served tools/list descriptions.
blast_radius: low
leverage: []
inputs: src/mcp/tools/definitions.rs inline tilth_search and tilth_write description literals at HEAD da84a41; prompts/tools/read.md include_str! precedent
outputs: prompts/tools/search.md and prompts/tools/write.md; two include_str! loads in definitions.rs; tests/mcp_v2/test_tool_descriptions_stable.py guard test
agent_resolution: []
execution_holds: []
gate_applicability:
  disposition: not-applicable
  work_class: refactor-only
  ui_surface: not-applicable
  reason: Byte-identical move of two description strings from Rust literals into include_str! prompt files; served tools/list bytes do not change, so no behavior exists to drive red first.
landing:
  shape: single
  layers: []
  per_layer_green: required
  review_fixes: fold
verification: the tools/list response from the post-move build is byte-identical to the one from a HEAD da84a41 build, and just check exits 0 including the new tests/mcp_v2/test_tool_descriptions_stable.py
---

## Parent
- Spec: benchmark-gepa-overhaul
- Goals: G-4
- Depends on: none
- Frozen decisions: F-2

## Problem

Parent goal: Grow the benchmark with FeatureBench, the Gin render-context task, and a few cherry-picked tasks, in a format GEPA can optimize against once-run stored baselines, with a model judge for structural-tool applicability.

This curd (parent curd c0, split out of c5 with user approval) is an enabler for G-4 under F-2. The evolution loop child `bench-evolution-loop` treats `prompts/mcp.md`, `prompts/tools/read.md`, `prompts/tools/search.md`, and `prompts/tools/write.md` as its four text components, and its AC-12 refuses to start when any of them is missing at the seed commit. At HEAD `da84a41` only `prompts/tools/read.md` exists as a file, loaded at `src/mcp/tools/definitions.rs:4`; the `tilth_search` description is an inline literal at `definitions.rs:10` and the `tilth_write` description is an inline literal at `definitions.rs:117`. The original detail is AC-18 of the pre-umbrella parent draft (`benchmark-gepa-overhaul.r3-pre-umbrella.md.bak:153`).

## Contract

The prompt-file move changes only where two strings live. `src/mcp/tools/definitions.rs` loads the `tilth_search` description from `prompts/tools/search.md` and the `tilth_write` description from `prompts/tools/write.md` through `include_str!`, exactly as it already loads `prompts/tools/read.md`. Each new file holds the decoded description bytes that HEAD `da84a41` serves, with no trailing newline (as `prompts/tools/read.md` has none, and `.markdownlint.json` disables MD047). The served `tools/list` response stays byte-identical. Input schemas, nested property descriptions, the shared `cwd` property, `prompts/mcp.md`, `prompts/tools/read.md`, `AGENTS.md`, `scripts/regen-agents-md.sh`, `server_instructions_byte_lock`, and the crate and npm versions stay untouched. `scripts/regen-agents-md.sh` reads only `prompts/mcp.md` (`scripts/regen-agents-md.sh:14,24`), so the new files do not feed `AGENTS.md` and regeneration is a no-op. A new guard test pins the served descriptions to the prompt file bytes. The child `bench-evolution-loop` depends on this curd.

## Grounding

| Probe | Outcome | Evidence |
| --- | --- | --- |
| wiki | hit | `.hallouminate/wiki/mcp-instructions-limits-and-format.md`: Claude Code truncates each tool description at 2,048 chars, which `tool_descriptions_fit_2kb` (`src/mcp/tools/definitions.rs:522`) already guards; the move leaves lengths unchanged |
| explorer | hit | HEAD `da84a41` read with `git show`: `include_str!("../../../prompts/tools/read.md")` at `src/mcp/tools/definitions.rs:4`, inline `tilth_search` description at `definitions.rs:10`, inline `tilth_write` description at `definitions.rs:117` (holds `\"` escapes, `\\t` and `\\n` escapes, and an em dash); `prompts/tools/read.md` is 749 bytes with no trailing newline; `SERVER_INSTRUCTIONS` at `src/mcp/mod.rs:76`, byte lock at `mod.rs:771`, surface cap 13,779 at `mod.rs:817`, `agents_md_matches_prompt_sources` at `mod.rs:1230`; regen script reads only `prompts/mcp.md` (`scripts/regen-agents-md.sh:14,24`); CI markdown job lints `prompts/` and checks the regen is a no-op (`.github/workflows/ci.yml:37,43,49`); `tests/mcp_v2/harness.py:55,90` provide `run_mcp` and `tools_list_request` |

## Approach

1. **Record pre-move bytes.** Build the HEAD `da84a41` binary and capture its `tools/list` response through `tests/mcp_v2/harness.py` into the scratchpad, outside the repo and uncommitted (F-2).
2. **Write the prompt files from the capture.** Write `prompts/tools/search.md` and `prompts/tools/write.md` from the decoded `description` strings of the captured `tilth_search` and `tilth_write` entries, byte for byte with no trailing newline, so no escape is hand-transcribed: `\"` becomes `"` and `\\t` becomes the two characters backslash and t (F-2).
3. **Load them through `include_str!`.** Beside `read_desc` at `definitions.rs:4`, add `search_desc` and `write_desc` from `include_str!("../../../prompts/tools/search.md")` and `include_str!("../../../prompts/tools/write.md")`, and replace the two inline literals with those bindings, with no runtime trim (F-2).
4. **Add the guard test.** `tests/mcp_v2/test_tool_descriptions_stable.py` drives `tools/list` and asserts each served description equals its prompt file bytes. It compares against the files, not against frozen pre-move bytes, because `bench-evolution-loop` candidates rewrite these files and must still pass `just check` (F-2).
5. **Prove identity and gates.** Rebuild, capture `tools/list` again, and compare it with the step 1 capture; run `bash scripts/regen-agents-md.sh && git diff --exit-code AGENTS.md`; run `just check` (F-2).

## Interface sketches

```text
slice:            mcp (existing tool registry)
spine step:       infra (compile-time prompt embedding)
public interface: MCP tools/list descriptions for tilth_search, tilth_write, tilth_read, unchanged bytes  (F-2)
public interface: prompts/tools/search.md and prompts/tools/write.md via include_str! in src/mcp/tools/definitions.rs  (F-2)
private:          search_desc and write_desc bindings in tool_definitions()
crust delta:      two new prompt files; two inline literals removed; one new tests/mcp_v2 guard test
arrows:           src/mcp/tools/definitions.rs -> prompts/tools/*.md (compile time); no new runtime dependency
```

## Acceptance
- AC-1: WHEN the MCP server answers `tools/list` THE SYSTEM SHALL serve the `tilth_search` description from `prompts/tools/search.md` and the `tilth_write` description from `prompts/tools/write.md`, each loaded through `include_str!` in `src/mcp/tools/definitions.rs` as `prompts/tools/read.md` is, and `definitions.rs` SHALL hold no inline literal for either description, so the four text components that `bench-evolution-loop` reads exist as files.  (F-2, G-4)
- AC-2: WHEN the post-move binary and a HEAD `da84a41` binary each answer the same `initialize` plus `tools/list` requests THE SYSTEM SHALL produce byte-identical `tools/list` responses, and `prompts/tools/search.md` and `prompts/tools/write.md` SHALL each equal the corresponding pre-move served description bytes with no trailing newline.  (F-2)
- AC-3: WHEN `python3 -m unittest discover -s tests/mcp_v2 -t tests/mcp_v2` runs THE SYSTEM SHALL pass `tests/mcp_v2/test_tool_descriptions_stable.py::ToolDescriptionsStable::test_descriptions_match_prompt_files`, which asserts that the served descriptions of `tilth_read`, `tilth_search`, and `tilth_write` each equal the bytes of `prompts/tools/read.md`, `prompts/tools/search.md`, and `prompts/tools/write.md`.  (F-2)
- AC-4: WHEN the change lands THE SYSTEM SHALL touch only `src/mcp/tools/definitions.rs`, `prompts/tools/search.md`, `prompts/tools/write.md`, and `tests/mcp_v2/test_tool_descriptions_stable.py`; `bash scripts/regen-agents-md.sh && git diff --exit-code AGENTS.md` SHALL exit 0; and `prompts/mcp.md`, `prompts/tools/read.md`, `server_instructions_byte_lock`, and the `version` fields in `Cargo.toml` and `npm/package.json` SHALL be unchanged.  (F-2)
- AC-5: WHEN `just check` runs THE SYSTEM SHALL exit 0 with the existing `definitions.rs` tests (`tilth_write_surface_teaches_replace_text_first`, `tool_descriptions_fit_2kb`, `tilth_write_schema_includes_replace_text_branch`) and `mcp_surface_stays_within_cap` unmodified, and markdownlint over `prompts/` with `.markdownlint.json` (the CI markdown job) SHALL report no finding.  (F-2)

## Non-goals
- Changing any description wording, input schema, nested property description, or the shared `cwd` property; the move is byte-identical.
- Changing `prompts/mcp.md`, `prompts/tools/read.md`, `AGENTS.md`, `scripts/regen-agents-md.sh`, or `server_instructions_byte_lock`.
- A test that freezes the pre-move bytes permanently; it would fail every `bench-evolution-loop` text mutation, so pre-move identity is a landing check (AC-2).
- The evolution loop, its applier, or any `benchmark/` file (parent curd c5, child `bench-evolution-loop`).
- Bumping the crate or npm version (fork law).

## Provenance (tier 2 only)
- culture: the user approved splitting the prompt-file move out of `bench-evolution-loop` as its own child; scope comes from AC-18 of the pre-umbrella parent draft and the parent F-2 decision that a candidate is a commit.
