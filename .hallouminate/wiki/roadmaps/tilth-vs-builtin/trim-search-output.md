---
kind: goal
slug: trim-search-output
roadmap: tilth-vs-builtin
created: 2026-11-07
prereqs: [content-replay-gate]
---
# Cut tilth search output toward the size of Grep output

## Intent

Search output is the largest remaining content cost. In the 2026-11-05 litmus, tilth search returned 57k characters per `chain:rg-edit-6` cell against 46k for `Grep`. `chain:express-edit-6` still costs about 15% more than baseline.
Two levers from [Stock A/B trajectory diagnosis](../../stock-ab-trajectory-diagnosis.md#levers-to-beat-the-stock-arm) are still open:

1. Remove the four continuation hints (`fetch_callers`, `fetch_callees`, `fetch_siblings`, `fetch_tests`) from the default output. Each hint repeats the full target object. The hints were 14% of chain search output, and no agent sent a `follow` query. Main `30f64208` still emits them for each unique hit (`unique_hit` in `src/mcp/tools/search_v2.rs`). This changes the search response contract, so get user approval first.
2. Print one line for each usage hit by default, and expand only definitions. The tools-sonnet-r1 directive `search-compact-usage-hits` cut search characters by only 6%. Start from its diff and from the cause of that small cut.

## Acceptance

- A replay of the stored chain cells (goal `content-replay-gate`) shows search result characters at least 15% below main `30f64208`, with no new errors.
- The replayed search responses keep the same definitions and the same hit paths for each recorded query.
- Regression tests cover the new default output, and `just check` passes.
- A paid check of `chain:express-edit-6`, tilth arm, 6 reps (about $9, user-approved), costs at or below the baseline mean of $1.24 per cell.
