# Compare extraction reuse before production migration

## ADR-001: Compare extraction reuse before production migration [status: accepted]

- **Context:** Existing libraries provide parts of Tilth's syntax extraction, but available evidence does not establish compatibility or maintenance savings.[^1]
- **Decision:** Run a test-only, three-language comparison before any production migration.
- **Alternatives:** Migrate immediately, evaluate grammar tags only, or retain the current implementation without measurement.
- **Consequences:** The experiment produces evidence without changing production behavior. A later migration still requires separate approval.
- **Outcome:** The experiment ran in closed PR #275 and rejects both candidates. See "Extraction reuse experiment result (measured)" in `upstream-library-reuse.md`.

[^1]: `upstream-library-reuse.md` ("Symbol analysis precedents"); approved spec `tilth-extraction-reuse-comparison`.
