---
status: trusted
last_verified: 2026-08-14
confidence: high
sources:
  - /home/paul/.local/share/cheese/paulnsorensen-tilth/specs/tilth-search-v2-roadmap.md
  - src/mcp/mod.rs:301-328
  - src/lib.rs:184-210
---
# Search v2 Public Discovery Topology

Tilth will converge on four public MCP verbs: `search`, `read`, `diff`, and conditional `write`. Public `grok` and `deps` disappear only after replacement coverage and graduation gates pass. Their useful behavior moves behind search. The user approves retiring the separate `list` verb on 2026-09-29; shell directory browsing replaces it.[^list-removal]

## Context

Session analytics showed that models use search heavily but rarely select grok or deps, even when those capabilities would improve an exact-symbol answer. The current split makes the caller classify intent before Tilth has resolved the target. In contrast, list has distinct directory-tree, overview, budget, and token-rollup behavior and materially replaces host browsing commands.

At the original decision, MCP dispatch exposed search, deps, grok, list, read, diff, and optional write (`src/mcp/mod.rs:301-328`). The Rust library independently exposes `run_deps` and `run_grok` (`src/lib.rs:184-210`).

## Decision

- Fold bounded definition/signature/body context into unique exact-symbol/file search results.
- Always attempt verified dependency impact for unique exact symbol/file results.
- Emit typed continuations for expensive callers, callees, siblings, and tests.
- Retire the MCP list registration, dispatch, and list-only implementation.
- Permit shell directory browsing. Preserve content-read and file-write guidance.
- Preserve shared CLI map and project-overview behavior.
- Remove only the MCP grok/deps registrations at graduation; retain the Rust library engines.

## Alternatives rejected

- **Keep all discovery verbs:** preserves schema tax and caller-side intent classification.
- **Delete grok/deps without replacement:** loses high-value context rather than improving adoption.
- **Fold list into read or search:** erases a coherent browse contract and harms batching/token rollups.

## Consequences

Search becomes the single semantic discovery entry point. Shell commands provide directory browsing. Search orchestration grows, but the internal grok/dependency engines remain reusable modules rather than duplicated logic. Grok/deps removal follows the graduation gates in [[tilth-search-v2-roadmap-006]], which supersedes the earlier parallel-tool trial.


The original retained-list decision is superseded for the MCP surface only.
Historical rationale above explains the earlier choice; it no longer requires keeping list.
Grok/deps removal remains a separate gate, as [[tilth-search-v2-roadmap-006]] defines.

[^list-removal]: User approval on 2026-09-29 to remove list and open a pull request, following the September reassessment in [[usage-analytics-2026-07]].

