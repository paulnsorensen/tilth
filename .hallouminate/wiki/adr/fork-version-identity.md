# ADR: fork version identity

The fork version is `0.0.0-experimental`. `tilth --version` prints `tilth 0.0.0-experimental+<short sha>`, so a stale binary is visible. The fork has no tag release flow.

Status: implemented (2026-11-22) in PR #471. The owner settled decisions V-1 to V-4. They replace the old fork-law rule "Version stays 0.8.4".

## Decisions (user-settled)

- V-1: `version` in `Cargo.toml` and `npm/package.json` is `0.0.0-experimental`. The nightly npm channel already publishes `0.0.0-experimental.<run>.<attempt>` (`.github/workflows/nightly.yml`), so both surfaces share one base.
- V-2: The fork version never tracks upstream's number. Before, the fork kept upstream's `0.8.4`, so a fork binary looked like an upstream release. Upstream is `jahala/tilth` (latest tag v0.9.0 on 2026-11-22).
- V-3: `build.rs` sets `TILTH_VERSION` to `<CARGO_PKG_VERSION>+<git rev-parse --short HEAD>`. `src/main.rs` (clap `version`) and the MCP `serverInfo.version` in `src/mcp/mod.rs` both read it. There is no dirty flag.
- V-4: PR #471 deletes `.github/workflows/release.yml`. The fork ships only `@paulnsorensen/tilth-nightly`. A `v*` tag publishes nothing to crates.io or npm.

## Sync rule

On an upstream sync, take the fork side for the `version` lines in `Cargo.toml`, `npm/package.json`, `Cargo.lock`, and `fuzz/Cargo.lock`. Upstream's `release.yml` stays deleted. `tests/mcp_v2/test_version_identity.py` reads `Cargo.toml` and `npm/package.json`, not the lockfiles. It fails when a sync brings back upstream's number in either file, or when a workflow runs on a tag push. CLAUDE.md lists this in the fork-law keep-ours list.

## Build behavior

- A build without git metadata (source tarball, Nix sandbox, a `cross` container without `git`) gets `tilth 0.0.0-experimental`. A missing `git` never fails the build.
- `build.rs` uses the sha only when `git rev-parse --show-toplevel` is the package directory. A tarball inside an unrelated repository, or a `GIT_DIR` that points elsewhere, gets the bare version. `build.rs` reruns when `GIT_DIR` changes.
- `build.rs` watches `HEAD`, the HEAD reflog `logs/HEAD`, the current branch ref, and `packed-refs` through `git rev-parse --git-path`. That resolves in a linked worktree too, where `.git` is a file. The reflog catches a commit after `git pack-refs`, when the branch has no loose ref. It skips absent paths, because Cargo reruns a build script on every build for a path that does not exist.
- A commit or checkout reruns the build script and recompiles the crate with the new suffix.
- A checkout with a `.git` entry where git gives no commit (one failed git call, a sandbox that hides the repository, no `git` on `PATH`) gets the bare version for that build. `build.rs` then prints a Cargo warning with the reason and watches a path that does not exist, so Cargo runs it again on the next build. Without that retry the result is sticky: no git file is watched, so no later build gets the suffix. A `just check` run on 2026-11-22 failed `test_version_identity.py` this way.

## Consequences

- New builds fix the stale-binary gotcha in [Benchmark harness gotchas](../benchmark-harness-gotchas.md): compare the `--version` suffix with `git rev-parse --short HEAD`.
- Benchmark run keys do not include `tilth_version`. It is a row field only (`benchmark/run.py` `_variant_metadata`). The parsers strip the `tilth ` prefix (`benchmark/run.py` `_tilth_version`, `benchmark/variants.py` `hydrate_mode_metadata`), so the new string parses unchanged.
- The strict-mode `hook_source_key` hashes `Cargo.toml`, `Cargo.lock`, and `src/main.rs` at the hook commit (`benchmark/run.py` `_HOOK_BUILD_INPUTS`). This change edits all three, so strict cells keyed at a hook commit after this change get a new `harness_digest`. Stored rows keep their keys.
- A tilth arm keys on `binary_sha256`. The embedded sha makes each commit's binary distinct, also for commits that do not touch `src/`. Candidate arms already key on `git_sha`, so this adds no new re-key for them.
