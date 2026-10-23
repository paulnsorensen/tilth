# rg_edit_flow_migration evidence

No-model preflight record. No paid model call produced this evidence.

## Fixture

- Repository: ripgrep, pinned revision `0a88cccd5188074de96f54a4b6b44a63971ac157`.
- Migration: `grep_matcher::Flow { Continue, Stop }` replaces `bool` and `Result<bool, E>` in the matcher iteration and replace callbacks, the `Sink` methods, the `sinks::{UTF8, Lossy, Bytes}` closures, and the searcher core plumbing.
- Predicates such as `is_match` and `should_binary_quit` stay `bool`.
- Reference size: 14 files, 241 edit sites (diff hunks), 5 crates (matcher, regex, pcre2, searcher, printer), +345/-293 lines.
- One sed cannot do it. A `bool` is a Stop flag at some sites and a judgment at others, and `Result<bool, E>` needs `?` handling.

## Held-out tests

- `crates/searcher/tests/flow_heldout.rs`, `crates/printer/tests/flow_heldout.rs`, `crates/regex/tests/flow_heldout.rs`, from `heldout/`.
- `crates/matcher/tests/test_matcher.rs` is a trusted copy that uses the new API.
- All pass on the reference.

## Grader commands

All commands run with `CARGO_BUILD_JOBS=4` and a persistent `CARGO_TARGET_DIR`.

1. `cargo test -j4 --no-run -p grep-matcher -p grep-regex -p grep-pcre2 -p grep-searcher -p grep-printer` (warm build)
2. `cargo check -j4 --workspace --lib --bins --tests --examples`
3. `cargo test -j4 -p grep-matcher -p grep-regex -p grep-pcre2 -p grep-searcher -p grep-printer`

`--all-targets` is not usable: the globset benches need nightly.

## Preflight results

Command: `flock /tmp/tilth-gepa-v2-tests.lock python benchmark/tasks/rg_flow_migration_fixtures/preflight.py` (or `RG_FLOW_PREFLIGHT=1` with the offline test). Output, trimmed to the verdicts:

```
original (no change): rejected No source changes
original source with held-out tests: rejected Trusted tests failed: cargo test -j4 -p grep-matcher ...
reference size: 14 files, 241 edit sites, 5 crates, +345/-293 lines
reference: accepted Trusted and candidate tests pass
edited manifest: rejected Unsafe changed path: crates/searcher/Cargo.toml
printers not migrated: rejected Trusted tests failed: cargo test -j4 --no-run ...
regex and pcre2 matchers not migrated: rejected Trusted tests failed: cargo test -j4 --no-run ...
searcher example not migrated: rejected Trusted tests failed: cargo test -j4 --no-run ...
quiet summary no longer stops: rejected Trusted tests failed: cargo test -j4 ...
searcher ignores an after-context Stop: rejected Trusted tests failed: cargo test -j4 ...
try_find_iter ignores Stop: rejected Trusted tests failed: cargo test -j4 ...
reference then printers not migrated, same directory and target dir: rejected Trusted tests failed: cargo test -j4 --no-run ...
preflight exit=0
```

- Incomplete-migration controls (3): printers, regex and pcre2 matchers, searcher example. Each fails to compile.
- Behavior-breaking controls (3): the quiet summary stops no more, the searcher ignores an after-context `Stop`, `try_find_iter` ignores `Stop`. Each compiles and fails a test.
- An earlier `searcher_stop` control, on a rare match-limit path, was accepted. It was replaced by the after-context site.

## Stale shared build

The grader keeps one persistent `CARGO_TARGET_DIR`. Cargo judges a workspace crate fresh when no source is newer than its dep-info, and a copied tree keeps old mtimes.

- In the normal flow the trusted copy lives in a random temp directory. The path is part of each crate's identity, so the preflight order (reference, then printers not migrated) never reused a build. The risk shows when the same directory path is graded twice.
- Reproduction: the `stale_target` control grades the reference in one directory, then restores the old printer files (old mtimes) in the same directory and grades again. With `_refresh_sources` disabled, the second grade was wrongly accepted (`AssertionError: incomplete migration passed on a target dir warmed by the reference`).
- Fix: `_grade` first calls `_refresh_sources`, which sets the mtime of every file outside `target/` and `.git/` to now. Every workspace crate rebuilds per grade. Third party crates stay cached. The helper is local, modelled on `refresh_sources` in `benchmark/external/swebench_ml.py`, to avoid importing that module.
- With the fix, `stale_target` rejects the second grade (last result line above).
- Offline test: `test_grade_makes_every_workspace_source_newer_than_the_shared_build_before_cargo_runs`.
## Known gap

Most ripgrep tests are in-source `#[cfg(test)]` modules. The candidate controls them. Only the held-out integration tests and the trusted matcher test are protected. The grader also runs the candidate copy, so a candidate that breaks its own in-source tests fails.

pcre2 has no held-out test. Existing tests cover it.

## Generator commands

- `reference.patch`: `git diff --binary` in a scratch copy of the fixture, after the migration and `cargo fmt`.
- `heldout/test_matcher.rs`: `cp` of the migrated `crates/matcher/tests/test_matcher.rs`.
