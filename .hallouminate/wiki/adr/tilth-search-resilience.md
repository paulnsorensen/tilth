# ADRs — tilth_search resilience (slug: tilth-search-resilience)

Session 2026-08-02; spec at `paulnsorensen-tilth/specs/tilth-search-resilience.md`.
Evidence: [[../usage-analytics-2026-07]].

### ADR-001: Failed regex falls back to literal, disclosed [status: accepted]

- **Context:** Observed `kind:regex` queries that were plainly literal text
  (`#[tool(`, `.step(`, `write_lane: Arc::new(Semaphore`) dying with parse
  errors. A teaching error would cost a retry; a silent fallback could
  mislead an agent that typo'd a genuine regex.
- **Decision:** Fallback with disclosure: rerun as escaped literal, output
  leads with the parse reason and "treated as literal". Acting is justified
  because an uncompilable pattern has no regex interpretation to lose; the
  note preserves the typo'd-regex agent's ability to notice and fix.
- **Alternatives:** Teaching error suggesting kind:content (stricter,
  consistent with suggest-only, one retry cost) — rejected since unlike
  fuzzy path matching there is no wrong-guess risk: the literal reading is
  the only executable interpretation.
- **Consequences:** kind:content path stays byte-identical (regression
  test); genuine regex behavior unchanged.



#### Search v2 disclosure regression (verified 2026-09-29)

Search v2 preserves literal fallback but loses the required disclosure.
This contradicts ADR-001; it does not supersede the accepted decision.

At revision `7005d6ab912843456dec498266fc3644fbcd4ea4`, `content::search` returns the regex parse reason with its literal result.
Both raw adapters discard that reason (`src/search/mod.rs:615-631`).
The MCP regex route still reports `resolved_as: regex` (`src/mcp/tools/search_v2.rs:408-415`).

A live probe searches for `RegexMatcher::new(` with glob `src/search/content.rs`.
It returns three literal matches, `status: ok`, `resolved_as: regex`, and empty diagnostics.
The escaped regex `RegexMatcher::new\(` returns the same three locations.
The source lines are 41, 45, and 52.

Treat this as an unresolved implementation gap, not proof of regex behavior.
The research session changes no search code and runs no regression suite.

_Source: checkout source and live MCP query comparison · Updated: 2026-09-29_

### ADR-002: Multi-symbol cap goes soft [status: accepted]

- **Context:** >5 symbols hard-errors in three arms (any/symbol/callers).
  The cap protects output budget, not correctness.
- **Decision:** Run the first 5, append a note naming the dropped symbols
  and advising a second call — the same shape as the tool's existing
  budget truncations, and nothing is silently lost.
- **Alternatives:** Keep hard error (observed failures stay failures);
  raise cap to 10 (bigger responses, rejected without benchmark evidence).
