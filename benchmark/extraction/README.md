# Tilth extraction reuse comparison

This experiment measures whether a third-party Rust crate can replace parts
of Tilth's own extraction machinery (`src/lang/outline.rs` and friends). It
compares two candidate packages against the same frozen fixtures Tilth uses
internally, without changing any production code or export (see the ADR at
`.hallouminate/wiki/adr/tilth-extraction-reuse-comparison-*.md` for the
full decision record).

This directory holds three sequential curds:

1. Fixtures, schema, and a Tilth baseline capture (frozen manifest under
   `fixtures/`, schemas under `schema/`).
2. Two isolated candidate packages plus this orchestrator
   (`candidates/`, `capture.py` — this file's subject).
3. A comparator that scores candidate records against the Tilth baseline
   (`compare.py`, not part of curd 2).

## Candidates

| Candidate | Crate | Pinned version | Provisioning |
|---|---|---|---|
| `tree-sitter-language-pack` | `tree-sitter-language-pack` | `=1.17.0` | **Network, run once.** Downloads a runtime grammar cache. |
| `ast-grep-outline` | `ast-grep-outline` (+ `ast-grep-{core,config,language}`) | `=0.45.3` | None. Grammars are statically linked into the binary. |

Each candidate is its own standalone Rust package under
`candidates/<name>/`, with its own `Cargo.toml` and its own `Cargo.lock`.
Neither candidate is a workspace member of the root `Cargo.toml`: building
or cleaning a candidate never touches the root `Cargo.lock` or root build
artifacts. `capture.py`'s `capture` subcommand asserts this by hashing the
root `Cargo.lock` before and after every candidate build and failing loudly
if the bytes differ.

Neither candidate ever calls into Tilth's own extraction code: both read
only the frozen fixture manifest and their own upstream library, and Tilth
production dependencies/exports are unchanged by this experiment (see AC-7
in the spec).

## Setup: provisioning (network, run once)

`tree-sitter-language-pack` resolves and downloads tree-sitter grammars at
runtime into `~/Library/Caches/tree-sitter-language-pack/v1.17.0/` the
first time `get_language()` is called for a given language. This step
requires network access and only needs to run once per machine:

```bash
python3 benchmark/extraction/capture.py provision
```

This builds the `tree-sitter-language-pack` candidate in release mode (that
build step may itself need network access for uncached crates.io
dependencies, same as any `cargo build`) and then runs its `provision`
subcommand, which calls `get_language("rust")`, `get_language("typescript")`,
and `get_language("python")` to warm the cache.

`ast-grep-outline` needs no provisioning: its rust/typescript/python
grammars are compiled statically into the crate, so `provision` prints a
note and does nothing for it.

## Offline capture and measurement (AC-4)

Once provisioned, everything else is offline:

```bash
python3 benchmark/extraction/capture.py capture
```

For each candidate this:

1. Runs `cargo clean --manifest-path <candidate>/Cargo.toml`, then a timed
   `cargo build --release --manifest-path <candidate>/Cargo.toml` — this is
   both the candidate's build and its clean-build timing measurement.
2. Records the built binary's size with `os.path.getsize`.
3. Runs the candidate's own `capture` subcommand 5 times against the same
   frozen fixture manifest (`fixtures/manifest.json`), each time with a
   *poisoned* proxy environment
   (`http_proxy=https_proxy=all_proxy=http://127.0.0.1:1`, upper- and
   lower-case). If a candidate ever tried to reach the network during this
   step, the connection would fail immediately and loudly — proving the
   measured capture is genuinely offline, not just apparently offline.
4. Validates every emitted `raw/<fixture>.json` and `normalized/<fixture>.json`
   record against `schema/raw-record.v1.schema.json` and
   `schema/normalized-record.v1.schema.json` with a hand-rolled stdlib
   checker (no `jsonschema` dependency): `version == 1`, all required keys
   present, capability `status` in `{supported, unsupported, error}`, and
   every `error` status carries a string reason.
5. Writes a machine-readable AC-4 evidence record to
   `.generated/evidence/ac4-measurement.json` (gitignored, regenerated on
   every run).

Exact offline capture command per candidate (also recorded verbatim in the
evidence file):

```bash
benchmark/extraction/candidates/tree-sitter-language-pack/target/release/tsl-pack-candidate \
  capture --manifest benchmark/extraction/fixtures/manifest.json \
  --out benchmark/extraction/.generated/candidates/tree-sitter-language-pack

benchmark/extraction/candidates/ast-grep-outline/target/release/ast-grep-outline-candidate \
  capture --manifest benchmark/extraction/fixtures/manifest.json \
  --out benchmark/extraction/.generated/candidates/ast-grep-outline
```

### AC-4 evidence fields

`.generated/evidence/ac4-measurement.json` records, per AC-4:

- `baseline_commit` — `git rev-parse HEAD`.
- `toolchain` — `rustc --version`, `cargo --version`.
- `host` — `platform.platform()`.
- `fixture_hashes` — copied from `fixtures/manifest.json`, never recomputed.
- `root_cargo_lock_self_check` — sha256 of the root `Cargo.lock` before and
  after all candidate builds, and whether it stayed byte-identical.
- `provisioning_vs_capture_separation` — a fixed statement of the
  provision/capture boundary.
- `candidates.<name>.package` — crate name + version, parsed from the
  candidate's own `Cargo.lock`.
- `candidates.<name>.grammar_or_asset_identity` — runtime cache directory
  for `tree-sitter-language-pack`; "statically linked" for `ast-grep-outline`.
- `candidates.<name>.provisioning` — provisioned/not-required, or a
  `{blocked, reason}` pair naming the missing prerequisite.
- `candidates.<name>.offline_capture_command` — the exact command run.
- `candidates.<name>.clean_build_seconds` — from step 1 above.
- `candidates.<name>.executable_size_bytes` — from step 2 above.
- `candidates.<name>.extraction_timing` — `{samples, count, median, range}`
  from 5 repeated offline capture runs (`statistics.median`; range is
  `{min, max}`), or `{blocked, reason}` if capture could not run.
- `candidates.<name>.validation` — `{status: ok, fixtures_checked}` or
  `{status: failed, errors}` or `{blocked, reason}`.

A missing prerequisite (cache not warmed, build failure, capture failure)
produces an explicit `blocked` cell naming that prerequisite. `capture.py`
never fabricates a timing, size, or extraction number for a blocked
candidate — this keeps benchmark-**execution** failures (blocked cells)
separate from negative candidate **findings** (a capability that genuinely
returns `unsupported` or an empty result once capture succeeds).

## Clean-build and executable-size measurement (standalone)

The same measurements `capture.py capture` performs internally can be run
by hand for spot-checking:

```bash
cargo clean --manifest-path benchmark/extraction/candidates/tree-sitter-language-pack/Cargo.toml
time cargo build --release --manifest-path benchmark/extraction/candidates/tree-sitter-language-pack/Cargo.toml
ls -l benchmark/extraction/candidates/tree-sitter-language-pack/target/release/tsl-pack-candidate

cargo clean --manifest-path benchmark/extraction/candidates/ast-grep-outline/Cargo.toml
time cargo build --release --manifest-path benchmark/extraction/candidates/ast-grep-outline/Cargo.toml
ls -l benchmark/extraction/candidates/ast-grep-outline/target/release/ast-grep-outline-candidate
```

## Isolation

- Each candidate is a separate Cargo package with its own `Cargo.toml` and
  `Cargo.lock`, outside the root workspace.
- Candidate builds never touch the root `Cargo.lock` (asserted by the
  byte-identity self-check above).
- Neither candidate calls into Tilth's own extraction code or falls back to
  it: each is a standalone adapter over its own upstream library, reading
  only the frozen fixture manifest.
- `.generated/` (this directory's captured records and evidence) is
  gitignored; nothing it produces is a supported Tilth interface.

## Established findings

These are real findings from capturing candidate output against the frozen
fixtures, not benchmark-execution failures:

- **tree-sitter-language-pack / TypeScript definitions are near-empty.**
  The crate's bundled TypeScript `tags.scm` matches only ambient/`.d.ts`
  declaration forms (`declare`, ambient modules). Concrete TypeScript
  declarations — ordinary `function`, `class`, `interface`, `const`
  statements in application code — yield no `@definition.*` captures, so
  `definitions` and `edit_spans` come back near-empty for the TypeScript
  fixtures even though capture itself succeeds. Its bundled `tags.scm` also
  declares no nesting, signature, or import captures for any of
  rust/typescript/python, so those three capabilities are `unsupported` by
  construction, not a capture failure.
- **ast-grep-outline nesting is one level only.** `ast-grep-outline`
  captures all 5 capabilities (definitions, nesting, signatures, imports,
  edit_spans) for all three languages, but its `OutlineEntry` model exposes
  only an item→member relationship: a top-level item's direct members are
  visible, but members never carry further nested members. This is a real
  structural limit of the library's model, not a fixture gap — it misses
  Tilth's arbitrary-depth nesting (e.g. a method inside an `impl` inside a
  nested `mod`).

See `compare.py` (curd 3) for the fixture-by-fixture comparison against the
Tilth baseline that quantifies these findings per capability and per
language.
