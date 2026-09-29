# Evaluate partial extraction reuse by capability

## ADR-002: Evaluate partial extraction reuse by capability  [status: accepted]
- **Context:** One candidate can support some languages or capabilities while requiring adapters for others.[^1]
- **Decision:** Score each language-capability cell independently. Count adapter code and retained Tilth code in every recommendation.
- **Alternatives:** Require one all-or-nothing replacement, or treat accurate output alone as sufficient for adoption.
- **Consequences:** Partial adoption remains possible. Large adapters or absent maintenance savings can still force rejection.

[^1]: Approved spec `tilth-extraction-reuse-comparison`, decisions F2 and acceptance AC-3 through AC-6.