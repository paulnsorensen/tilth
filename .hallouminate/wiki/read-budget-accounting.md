# `tilth_read` budget accounting — and why budget guards go vacuous

Two hard-won facts about `src/mcp/tools/read.rs`: where the response-budget
contract actually lives, and why the obvious test for it silently fails to
test anything. Established while curing PR #149 (bare-string `paths` coercion),
which took three passes precisely because the guard looked fine and wasn't.

## Where the budget contract lives

`finalize_response` enforces the single-read and truncated-batch budgets.
Complete batches use a separate exact-fit check that includes the JSON header and all framing.
`session.record_savings(...)` measures the body before downstream additions.[^batch-allocation]

So anything a wrapper appends to the response **after** `tool_read_paths`
returns escapes both. That is a two-part defect, and it is easy to fix only
half of it:

1. the response overshoots the caller's declared `budget` (advertised in the
   `tilth_read` schema as "Max tokens in response"), and
2. `record_savings` books tokens the caller never actually saved.

PR #149's batching nudge hit both. The fix shrinks the budget passed down to
`tool_read_paths` by the appended text's token cost, and adds that cost back at
`record_savings`.

**There are two `record_savings` call sites, and they are easy to conflate:**

| site | path | reached when |
| --- | --- | --- |
| signature / auto-promotion | `respond_signature` return | `should_auto_signature` is true — **code** file types only |
| general auto-read | the `finalize_response` return at the end | everything else, including any **non-code** file over `TOKEN_THRESHOLD` (markdown, JSON, YAML, TOML) |

A `.rs` test fixture only ever exercises the first. A cure pass fixed that one,
reported both as done, and the second stayed broken until a scoped re-review
caught it. If you touch savings accounting, **write one test per site** and use
a large-markdown fixture for the general path (`tool_read_auto_large_markdown_returns_outline`
is the shape reference).

## Why a `<= budget` assertion cannot catch a budget regression

`truncate` flat-reserves **50 header tokens**. The actual header is shorter than
that, and by an amount that depends on the absolute path length — so a read
always lands some tokens *under* the declared cap, with the gap varying by
tempdir path length.

For a short path (Linux CI, `/tmp/.tmpXXXXXX/…`) that structural slack exceeds
25 tokens, which is more than enough for an unbudgeted note to hide in. The
assertion passes on broken code. On macOS the same test fails, because
`/var/folders/…` paths are ~69 chars and eat the slack.

**Net effect: the guard bites only on the developer's laptop and is vacuous on
the CI that enforces it.** That is the worst possible arrangement, and it looks
like a passing test.

## Why the obvious differential is also wrong

The natural repair is a path-length-invariant differential — compare the
coerced read against the equivalent array-shaped read. Two traps:

- **Do not subtract the appended text's tokens from the coerced side.**
  `estimate_tokens` is `div_ceil(len, 4)` and therefore subadditive, so
  `coerced − note ≤ array` holds *identically* on the unfixed code. This
  cancels the very defect under test. It was tried in PR #149 and verified
  vacuous by reverting the fix and watching the assertion still pass.
- **Do not compare raw token counts either.** The shrink reserves 25 tokens
  (100 bytes) for a 99-byte note, so exactly **1 byte** of margin separates the
  two counts — and truncation rounds to line boundaries, which swamps it in
  both directions. `coerced <= array` fails post-fix by one token on a
  perfectly correct implementation.

## What actually works: assert the contract exactly

Byte equality has no margin to lose, is path-length independent, and fails on
the unfixed code on every platform:

```rust
// a coerced read at `budget` IS the array read at `budget - note_tokens`,
// with the note appended
assert_eq!(out, format!("{array_out}{note}"));
```

Keep the plain `<= budget` assertion alongside it as a cheap guard, but never
let it carry the test alone.

**Verification rule this episode earned:** a new budget/accounting guard is not
done when it passes. Revert the fix and confirm the test *fails* — and when the
guard has a platform-dependent component, neutralise that component first so
you are proving the assertion that CI will actually rely on.

## Known local-only test failure

`mcp::tests::batch_budget_represents_every_query` (`src/mcp/mod.rs`) **fails on
macOS and passes on CI.** Same root cause as above: long `/var/folders/…`
tempdir paths push a `tool_search` batch-budget assertion over its cap. It is
unrelated to whatever you are working on.

Confirm before chasing it: `git stash push -u` → `cargo test` → `git stash pop`.
If it fails identically with your changes stashed, it is baseline and not yours.

## Gate note

Current CI uses `cargo clippy --all-targets -- -D warnings`.
Both the baseline and allocator change pass this gate locally.[^current-gate]

[^current-gate]: .github/workflows/ci.yml:24; PR #279

## Batch allocation, 2026-09-28

Batch reads first check whether the complete framed response fits the requested estimated-token budget.
If it fits, they return all rendered parts without per-file truncation.
Otherwise, small parts keep their required allocation and larger parts share the remaining budget.
Allocation uses rendered size, so it applies to plain paths, line ranges, and symbols.
Output keeps input order and reserves space for separators and the missing-file footer.
Existing truncation markers can exceed their allocated share, so the batch checks rendered size and reduces allocations before finalization.[^batch-allocation]

Below about 48 tokens, `budget::truncate` renders a part as its first line plus a marker, so a smaller cap cannot shrink it.
The reduction loop therefore stops when the rendered size does not decrease.
If the body still overflows, the fallback keeps whole parts in input order.
It skips a part that does not fit and tries the next part.
A `── omitted (raise budget) ──` section lists each skipped part by path, before the missing-file footer.
The fallback reserves space for a list of every part path first, so both sections survive.
If the path lists still do not fit, each section shows only a count, such as `6 parts` or `6 paths`.
A follow-up can remove this loop: make `truncate` honor its cap, or share one allocator with `src/search/alloc.rs`.[^batch-allocation]

Tests cover exact fit, unequal sizes, reversed order, UTF-8, missing files, tagged ranges, and tiny-budget safety.
Use a fresh Session after any sizing read when testing whether the budgeted read records edit snapshots.
Otherwise, the first read can make the write assertion pass without testing the second read.[^batch-allocation]

Shared truncation behavior remains unchanged.
A truncated tagged section can lose its tag and numbered content at a blank-line boundary.
Below the JSON header plus the count summary, no batch body fits, and `finalize_response` clips it.
These are separate limitations, not strict-cap guarantees from the allocation change.[^batch-allocation]

[^batch-allocation]: src/mcp/tools/read.rs:158-366,708-755; src/budget.rs:52-105; PR #279

## Patched Luna read-budget benchmark, September 2026

The read-budget follow-up in [Tool Efficiency Report: tilth versus WOZCODE](sources/tilth-versus-wozcode-2026-09.md) removes observed truncation without establishing an overall performance improvement.[^sept-read]
The earlier larger-task run truncates six of 25 reads.
Equal per-file shares leave small-file budget unused while larger files lose requested content.
The tested local allocator redistributes unused shares and preserves complete batches that fit the framed budget.
The patched binary records zero truncation notices across 24 reads, including the timeout trace.
Its SHA-256 is `67385086492d457aaadea0e2caa225fccb935ac3d653dfb1d1fbe52ddbc0060d`.
This binary includes an uncommitted read allocation change; benchmark extraction does not ship that Rust change.

Native passes 3/3; patched tilth passes 2/3 and times out once after 600 seconds.
Mean native time is 391.535 seconds; tilth's timeout-inclusive mean is at least 490.445 seconds.
The two completed matched pairs average 412.692 versus 435.668 seconds.
That subset does not replace the overall result.
The timeout has no completed turn, trusted grade, or final usage. Candidate test success does not make it correct.
Missing cost and token fields remain unknown.

The timeout contains four wholly failed writes and one partial write, rejecting 18 file sections.
Some replacement strings contain literal backslash-n/backslash-t sequences; other calls fail the unread-line guard.
Corrections and retries remain included.
Old and patched binaries are not interleaved; fixed arm order, shared caches, and model variation prevent causal speed claims.
The reader already uses Rayon; this evidence does not establish serial file reads as the bottleneck.

[^sept-read]: https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/read-budget-benchmark-results.md; https://github.com/paulnsorensen/tilth/blob/2b17c3755589a4e89eccaf89c3309ea9591aaa82/benchmark/reports/2026-09-28-tilth-vs-wozcode/luna56-render-context-results.md; src/mcp/tools/read.rs:149-163,231-240 at 18b7534eecde023cb4a70e5d13d7de29178073de

_Source: PR #278 at 2b17c3755589a4e89eccaf89c3309ea9591aaa82 · Updated: 2026-09-29 · Supersedes: no historical measurements; narrows general claims to their measured configurations_
