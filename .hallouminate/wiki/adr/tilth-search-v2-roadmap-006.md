---
status: reviewed
last_verified: 2026-09-06
confidence: high
sources:
  - https://github.com/paulnsorensen/tilth/pull/231
  - https://github.com/paulnsorensen/tilth/issues/190
---
# Search Continuations Before MCP Verb Removal

Search continuations replace the caller-selected search kind.
MCP grok/deps remain temporarily until Part B passes its removal and graduation gates.[^1]

## Context

ADR-001 plans five public verbs: search, read, list, diff, and conditional write.
Its replacement for grok/deps combines bounded search enrichment with typed continuations.[^2]
ADR-002 rejects caller-selected kind, expand, and context.[^3]

The initial PR #231 draft retains grok/deps and restores kind:callers.
Its usage count establishes demand for callers, not a requirement for that request shape.
The user rejects that exception and approves executable search continuations instead.[^1]

## Decision

- Accept either a query entry or an echoed follow hint in each search batch.
- Keep glob as the canonical query filter.
- Reject a fresh query combined with follow.
- Reject caller-selected kind, expand, and context.
- Execute every emitted hint inside tilth_search.
- Use fetch_callers, fetch_callees, fetch_siblings, fetch_tests, and fetch_dependencies.
- Preserve the resolved target, original scope, and applicable glob in each hint.
- Validate hint data without introducing signed tokens or a session-issued-hint registry.
- Return the requested bounded section and report incomplete output accurately.
- Keep MCP grok/deps in this phase.
- Remove those verbs only after Part B proves replacement coverage and the graduation gates pass.
- Keep the public Rust grok/deps APIs.[^1][^2]

The remaining kind field belongs inside the server-emitted follow hint.
It identifies a continuation, not a caller-selected mode for a fresh query.[^3]

## Contract Corrections

The canonical status values are ok, partial, no_match, and ambiguous.
A failed index refresh cannot establish complete dependency coverage.
Budget reduction preserves valid JSON and one result record per batch entry.
A budget below the required metadata size produces an explicit error.[^1]

The earlier path field accepts directories as well as files and globs.
The shipped glob filter is not automatically an equivalent alias.
This phase keeps glob and does not add a path alias.[^1][^3]



The pre-merge review of PR #231 fixed four contract gaps that the tests now pin:[^4]

- An echoed follow target must be cwd-relative and must canonicalize under the canonical cwd, glob or not. Absolute hint paths and symlinks that escape cwd are rejected. `cwd` and `scope` themselves stay trusted-absolute per fork law; only the hint's `path` is constrained.
- Every emitted text payload (`core`, continuation `signature` and `call`) passes the secrets denylist. v1's formatters were the only prior gate, so the cutover would otherwise have emitted `credentials.py` bodies verbatim.
- Budget reduction downgrades only `ok` to `partial`; an `ambiguous` or `no_match` result keeps its status and gains `budget_limited`.
- Telemetry is captured before budget reduction and records `budget_limited`, a session-derived `first_call`, and `route: batch` for multi-entry calls, so a small client budget is never logged as dependency-index incompleteness.

A bare `worktrees/` directory is skipped only when an immediate child holds a `.git` entry; `.worktrees/` is skipped unconditionally. The deps reconcile walk shares that predicate with the search walker.

## Alternatives Rejected

- Restore kind:callers: preserves the mode-selection problem instead of implementing the planned continuation.
- Accept query plus follow: turns a continuation back into an intent selector.
- Rename hints without dispatch support: leaves the replacement workflow unusable.
- Remove grok/deps immediately: bypasses Part B and the measured graduation gate.
- Preserve both path and glob without normalization rules: risks a silent scope change.

## Verification

Each continuation test copies a hint from a real search response.
The test follows that hint and checks the requested output.
Additional tests cover target collisions, scope, batch order, invalid requests, partial coverage, and JSON budgets.[^1]

[^1]: User-approved continuation correction for PR #231, 2026-09-06: https://github.com/paulnsorensen/tilth/pull/231
[^2]: [Search v2 public discovery topology](./tilth-search-v2-roadmap-001.md), lines 12–26; [Measured parallel trial](./tilth-search-v2-roadmap-004.md), lines 20–26.
[^3]: [Deterministic search contract](./tilth-search-v2-roadmap-002.md).
[^4]: Affinage review of PR #231, 2026-09-06 (`.cheese/affinage/pr-231.md`); deferred findings tracked in https://github.com/paulnsorensen/tilth/issues/233.

_Source: PR #231 user direction · Updated: 2026-09-06 · Supersedes: the initial ADR-006 caller-kind exception and permanent seven-verb conclusion._
