---
kind: goal
slug: fix-accuracy-failures
roadmap: tilth-vs-builtin
created: 2026-11-07
prereqs: [confirm-accuracy-gap, diagnose-accuracy-failures]
---
# Fix the tool causes of the accuracy failures

## Intent

Remove each tool cause that `diagnose-accuracy-failures` finds.
When the diagnosis finds only reasoning failures, record that and close this goal. Do not try prompt steering for it: the preamble rounds and the stock-prompt A/B showed no detectable prompt effect.

## Acceptance

- Each fix has a regression test that fails on main `30f64208` and passes with the fix.
- `just check` passes.
- A paid re-run of the tilth arm, with the settings of `confirm-accuracy-gap` and 20 reps (about $3, user-approved), passes at least 18/20 on each task that has a confirmed gap and a tool cause; a task whose failures are all reasoning failures is not gated.
