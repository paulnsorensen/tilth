---
status: reviewed
last_verified: 2026-09-06
confidence: high
sources:
  - https://github.com/paulnsorensen/tilth/pull/189
  - https://github.com/paulnsorensen/tilth/pull/231
---
# Deterministic Search v2 Contract

Search owns initial query routing.
Clients request deeper graph work by echoing server-emitted continuation hints.
Fresh queries do not accept kind, expand, or context.[^1]

## Request

The canonical request is an ordered batch of 1–10 entries:

```text
{ cwd, queries: [{ query, glob? } | { follow: hint }], budget? }
```

Each entry contains either query or follow, never both.
The optional positive budget applies to the serialized JSON response.
Glob remains the canonical query filter.
This correction does not add an alias for the original path field.[^2]

The initial query retains deterministic routing.
A continuation bypasses intent selection because its hint identifies a resolved operation and target.[^1]

## Continuations

Hints identify bounded callers, callees, siblings, tests, or dependency work.
Every emitted hint must execute inside tilth_search.
A hint preserves target identity and applicable scope constraints.
The client copies the hint into follow without constructing a fresh query.[^2]

The request-shape restriction does not require signed tokens or a session hint registry.
The server validates the concrete target and scope again when it executes the continuation.[^2]

## Response

Responses remain JSON text with top-level results, hints, and diagnostics.
Result order matches request order.
Statuses are ok, partial, no_match, and ambiguous.
Completeness describes actual output coverage.
An incomplete scan cannot establish no_match.
Routes tried remain telemetry-only.[^1][^2]

Budget reduction preserves JSON and one result record for every input.
The server rejects a budget that cannot fit required metadata.[^2]

## Non-code Filename Queries

An exact query naming a non-code file searches for repository text references.
It does not imply a request to read that file.
Issue #202 explicitly requests this behavior for uv.lock.
Restricting the search to that file would remove the requested documentation and lockfile references.[^3]

## Historical Contract

The original roadmap names the filter path and permits files, directories, and globs.
The implementation ships glob instead.
The 2026-09-06 correction selects glob without assuming that directory-valued path inputs are equivalent.[^2]

## Alternatives Rejected

- Public query kinds or prefixes: require the caller to classify intent.
- Query plus follow: restores a mode selector under another name.
- Single-query-only requests: discard useful batching.
- Prose outside the JSON envelope: forces clients to reinterpret the response.
- Unconditional graph expansion: increases cost without a continuation request.[^1][^2]

[^1]: [Merged roadmap PR #189](https://github.com/paulnsorensen/tilth/pull/189); [Discovery topology](./tilth-search-v2-roadmap-001.md).
[^2]: User-approved continuation correction for [PR #231](https://github.com/paulnsorensen/tilth/pull/231), 2026-09-06; [Continuation decision](./tilth-search-v2-roadmap-006.md).
[^3]: [Issue #202](https://github.com/paulnsorensen/tilth/issues/202).

_Source: PR #231 user direction · Updated: 2026-09-06 · Supersedes: the original path-filter request sketch; preserves server-owned initial routing._
