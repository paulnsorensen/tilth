# Press report: bench-evolution-loop

Readiness: ready for /age, with review follow-ups for Age.

## Attempts
| attempt | outcome | router action | candidate | route | telemetry |
| --- | --- | --- | --- | --- | --- |
| 1 | green | Dispatch("/age") | .cheese/press/candidates/bench-evolution-loop.attempt-1.json | .cheese/press/bench-evolution-loop.attempt-1.route.json | .cheese/press/bench-evolution-loop.attempt-1.telemetry.json |

## Evidence
- Attack identity: evolve-boundaries (benchmark/tests/test_evolve_press.py, 7 tests: cfg(test) lexing with raw strings, char and comment braces, and a const item; live code next to a cfg(test) const stays editable on the real tilth tree; the byte-lock literal edit is rewritten, not refused; quota stays the stop reason after a later ceiling; the proposer diff carries new and deleted src files and drops Cargo.toml; a failed proposer call keeps the parent's patch and still charges; the content id hashes UTF-8 bytes).
- Test digest: sha256 441808e0033464f313f3e22eb1f018f7d0ee278930add3496582b7a0aa4e00ef
- Production tree unchanged during the Press interval (only the new test file changed).

## Review follow-ups
- run.build_candidate locates candidate worktrees through config.REPO_ROOT; evolve's hidden --repo flag is used only by tests. A --repo other than REPO_ROOT would build from the wrong repository.
- The reflection prompt carries records only, never the apply or just check tail, so reflection gets no feedback on build failures. That follows the spec's "built only from these records", but it weakens the search.
