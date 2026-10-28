# Evolve

GEPA search over tilth commits. A candidate scores each task as `p * base / c` (cost per correct).

## Score and finish rule

`p` is the candidate pass rate on the task. `c` is the candidate mean row cost (`total_cost_usd`, else
`charged_usd`). `base` is the frozen baseline-arm mean `total_cost_usd` for the task. A score of 1.0 means
baseline parity. A task never solved scores 0. The ratio has no cap. Only an absent or null `total_cost_usd`
falls back to `charged_usd`. A negative, non-finite, or malformed cost, or a solved task with a mean cost of 0,
stops the run.

`buy_baselines` buys the cheap, dev and test baselines before any candidate cell. A task with no completed
baseline row, or a baseline cost that is not a number above 0, refuses the run (exit 2).

Finish opens a PR only if both hold: the lower bound of the paired test-split bootstrap interval of the
per-task score delta (winner minus seed) is above 0, and the winner's aggregate test pass rate is at least
the seed's.

Paid-tier side info for reflection carries `cost_pairs` for the task: the candidate's rows and the frozen
baseline rows, each cut to cost, turns, tokens, tool calls, tool sequence, and batch sizes. Reflection can
so see why a candidate costs more than stock Claude. The pairs carry no trajectory and no grader output.
The dispatcher sends reflection and the `src/` proposer one `scores` record per candidate (task scores, pass
rates, mean costs, baseline costs) and one `cost_pairs` record per task. Both prompts state the score
(`candidate.OBJECTIVE`) and ask for lower cost without lost correctness.

## Diagnosis records

After a candidate's paid dev tier, `attribution.summarize` runs once per content id on the candidate's dev
rows and the frozen baseline dev rows. It never reads test-split rows or grader text. A failure is logged and
skipped. The side info for the candidate carries a compact `attribution` view under its own key (never under
`scores`), at most about 4k characters: coverage, the leading causes with evidence, `call_shapes` and `errors`.
The dispatcher sends it, with a `source_map` record for the leading causes, to reflection and the proposer.

`diagnosis.WeightedSelector` is the gepa `module_selector`. It weights each component by the parent's
`dominant` hints (the hinted component takes about 44% of the picks, against 20% for a uniform choice), picks
`src_patch` within the first 2 proposals, and leaves no component out for more than 10 proposals in a row. The
bound is `MAX_UNSELECTED`. Each pick goes to `log.txt` with its evidence and reason. A reflection or proposer call
receives a `replay` record only from its own candidate, never from another one.

Reflection and the proposer end a reply with one `<hypothesis>` block (`metric`, `scope`, `direction`,
`tolerance`, `invariants`). `hypotheses.jsonl` in the run directory holds it, keyed by parent and child content
id, and then the outcome (`met`, `unmet`, `unmeasurable`) after the child's paid dev tier. Later calls get the
last outcomes as a `hypothesis_feedback` record. A hypothesis is soft: it never rejects a candidate.

`--replay-diagnostic N` (default 0, off) replays the first N seed dev cells (by task, then repetition) against
the seed binary and the candidate binary when the candidate changes `src_patch`. The `replay.compare` result goes
to `log.txt` and to later calls as a `replay` record. It reuses the loop's build directory, writes no result
rows, and never rejects a candidate.

## Strict file tools (default)

Every cell runs with Bash guarded by the tilth hook (`tilth hook`). The tilth arm runs the candidate
binary's own hook with `--hint tilth`. The baseline runs the seed's hook with `--hint native`.
Every cell uses `hook_sha` = the seed sha, so baseline run keys depend on the seed hook source.

Pass `--no-strict-file-tools` to run cells without the hook. Stored non-strict baselines are not
reused by a strict run, and the reverse. The first strict run buys a full strict baseline.

## Seed-hook preflight

Before any paid call, a strict run builds the seed hook and replays every row of the frozen corpus
(`benchmark/hook_corpus.json`) through it in both hint modes. A `must_deny` row that the hook allows,
or a `must_allow` row that it denies, refuses the run (exit 2). The log names the failing rows.

## Hook gate

A candidate may edit `src/hook/**`. After its build and before its first cell, the cascade replays the same
corpus through the candidate binary in both hint modes. Any wrong verdict fails the candidate at stage
`hook gate` with score 0 and runs no cells. The failing commands go to reflection in the stage tail.
A candidate that cannot allow `go test ./...` or deny `cat src/lib.rs` with the right hint text
also fails at `hook gate`, through the harness probe. The corpus file itself stays outside the
candidate allowlist (`src/`, `prompts/`).

## Proposer repair

The proposer has no Bash, so it cannot compile. When a `src_patch` proposal's child fails `apply` or
`just check`, the dispatcher calls the proposer once more on the same parent, with the failed patch applied and a
`repair` record: `stage`, `error` (the failure tail), and the instruction to fix only that failure. The repaired
patch replaces the first one, so the content id names the final patch and GEPA never sees the broken child. The
check is the cascade's own memoized apply and `just check` stage, so a passing child is not checked twice. A
repair that also fails is a failed candidate, with one exception: when the repair's own `just check` failure is
lint-only or fmt-only (every error block is a clippy lint with its `-D clippy::...` note or a rustfmt `Diff in`,
with no test failure and no `error[E...]`), the dispatcher makes one more repair with the new tail. That is at most
three proposer calls per proposal, and the log shows each attempt. The extra repair keeps the test grants of the
repair before it. `--no-repair` turns off both. The directive is claimed once and the
hypothesis stays on the final child. Each repair call is billed as `proposer` spend. Text components are never
repaired. `--no-repair` turns the repair off.

A long failure tail keeps the compiler, clippy, test, or fmt error blocks that cargo printed before its closing
`build failed` lines, plus the last ten lines.

### Pinned-test updates in repair

Adding tests needs no repair. A `src_patch` may add whole new `#[test]` fns inside an existing `#[cfg(test)]`
module or in a `#[cfg(test)] mod x;` module file. The materializer checks only cheap rules on an added fn: it needs
a test attribute and no `ignore`. Existing test fns change only under a repair grant (below). Added and changed tests
count as `tests_changed`, appear in the log, and appear under "Test changes for review" in the draft PR. A new
`#[test]` fn outside `#[cfg(test)]` code is listed there too. The materializer also refuses a candidate that edits
a `#[cfg(not(test))]`, `#[cfg(any(..test..))]`, `cfg_attr` or `#[cfg(debug_assertions)]` item, or that adds
an inner `#![cfg(..test..)]` attribute.

### Integrity stage

The test text is not the safe thing to check. A candidate can add a `macro_rules! assert_eq` on a test attribute line,
or put `#[cfg(any())]` above the test module. Then `just check` is green while the seed's tests no longer run as the
seed wrote them. So the cascade has a behavioral stage, `integrity`, after `just check` passes and before `build`.
The rule: the seed's tests must keep running and passing against the candidate's production code, exactly as the seed
wrote them. The one exception is a test with a repair grant, which may assert new behavior.

The stage applies to every `src_patch` candidate whose patch differs from the seed. It builds the tree in the
candidate worktree: the candidate's production code stays, and each outermost `#[cfg(test)]` item is replaced by the
seed's item with the same header. The header includes the run of attributes stacked above the item, so a candidate
that adds `#[cfg(any())]` or `#[cfg_attr(.., path = ..)]` there gets no seed twin, and its item is dropped. Granted
test fns take the candidate's version. A granted fn whose body defines an item (`impl`, `trait`, `macro_rules!`, `mod`,
`use`, `struct`, `enum`, `fn`, `static`, `const`, `type`, `union`, `extern`) is refused, since such an item can change
how other tests resolve. A candidate `#[test]` fn outside any test `cfg` item with the name of a seed test is refused. Seed `#[cfg(test)] mod x;` files are
restored byte for byte (granted fns excepted), and test modules or files the candidate added are dropped. It then
lists the tests of each cargo target apart (`cargo test --lib -- --list`, and `cargo test --bin <name> -- --list` per
bin). Every seed test name must be in the list of the same target as in the seed; the seed names are listed once per
run in a scratch worktree. Last, it runs `cargo test --lib --bins`, and all tests must pass.

A failure is a cascade failure at stage `integrity`, and it is repairable like `just check`. The tail names the
missing or failing seed tests. A repair after an integrity failure never gets test scope. Where a replacement cannot
be made sound (a production macro that shadows a seed macro, an inner test `cfg` in a production file, a granted fn
that shares lines with other code or defines an item), the candidate fails at this stage with the reason. It is never
accepted.

A cargo timeout, or a cargo failure that is not a compile error, is an infrastructure fault. The stage then reports
that cause in its headline instead of blaming the candidate's tests, and the result is not memoized.

The materializer backs the stage with these refusals: an added or changed `cfg` or `cfg_attr` attribute stacked on a
test item; a `cfg_attr(.., path = ..)`; a test name defined twice in one file; and added code that names
`current_exe`, `/deps/`, `RUST_TEST_`, or `--test-threads`. The last is a heuristic. Production code can still tell a
test run from a real run in a way it does not catch, so the winner PR note asks the reviewer to read the production
diff for that.

Cost per candidate: one incremental test build per listed target and one test run in the shared candidate target dir,
and one extra seed list build per run. Cargo trusts file mtimes and the trees share a target dir, so the stage touches
the sources before each cargo call; `just check` gets the same touch.

The stage is on whenever cargo is wired: `build(..., cargo=...)` sets it, and a custom `just_check` without `cargo` turns
it off with a warning on stderr. The stage is skipped for the seed, for an unchanged patch, and for a text-only child
(the result is memoized by patch and grant).

Limits: a granted fn that needs a test helper the candidate added fails integrity (repairable). Edits to non-test lines
in plain `#[cfg(test)]` code pass the materializer, because this stage restores the seed's version.

### Repair grants for existing tests

A proposer cannot change existing `#[cfg(test)]` code, so an intended behavior change used to die on the test that pinned the
old behavior. When the failed stage is `just check` and cargo's `failures:` list names Rust tests, the repair record
also carries `failing_tests` and `may_update_tests: true`. The repaired child may then change exactly those `#[test]`
fns (matched by module path), plus any test its parent already changed. The grant is keyed by the candidate's
`src_patch`. A child that is not a repair inherits the parent's changed tests read-only, so their text must stay
byte-equal to the parent's. A text-only child keeps the grant. The prompt allows a change only when the intended change
makes a pinned test obsolete; the test must assert the new behavior. The materializer refuses any other cfg(test)
change, a deleted or reverted granted test, and any file outside `src/` and `prompts/`. It also refuses a weakened
granted test: it needs exactly one test attribute, no added `ignore`, `should_panic`, `cfg` or `cfg_attr`, and an
assertion (`assert!`, `assert_eq!`, `assert_ne!`, `panic!` or a postfix `?` in a `Result` fn) when the old body had one. A candidate that changes
tests is `tests_changed`, and the log names the tests. If it wins, the draft PR body lists each changed test with its
diff under "Test changes for review". Nothing merges automatically.

## Auto-format

The proposer's export is `cargo fmt`-formatted before its cumulative diff is taken, so the content id and every later
proposal build on formatted code. The materializer runs `cargo fmt` on the applied tree before it commits, so
as it is, and `just check` reports the real error. A proposer `cargo fmt` failure is logged, and the diff stays
unformatted. The materializer rewrites the byte-lock literals after the first format, so it formats the tree again
when the rewrite changes it.

## Rejected-candidate cleanup

Each built candidate keeps a copy of its binary in `results/candidates/<sha>/` (about 39 MiB). Evolve
deletes that directory when the candidate is definitively rejected: it fails at `apply`, `just check`,
`build`, `hook gate`, or `cheap tier`, is dominated after one rollout or after the re-runs, or is evicted from
the frontier by a dominating candidate. It keeps the directory when a rollout did not complete, because a
resumed run reuses stored rows keyed on the binary digest and a rebuild may differ byte for byte. It never
deletes the seed's directory (the seed binary is the hook binary for every cell) or the shared
`candidates/target` build cache. A deletion error is logged, not raised.

## Seed-hook replay floor

Every strict cell replays each Bash call through the seed hook. A cell that ran a call the seed hook
denies raises `HookLoosenedError`. A candidate therefore cannot loosen the hook to win cells.
