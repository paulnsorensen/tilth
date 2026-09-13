# Upstream core extraction CI assessment

The observed upstream CI runs do not justify more crate boundaries for speed.
This assessment covers upstream PR #210, not this fork's current implementation.[^1]

## Pinned scope

PR #210 has head `7f38db58696e16c2df2be71da985f47097f34920`.
Its base is `garden/map` at `c3ece9b9959ed59edc139b4ac067b0aa40724a90`, from PR #209.[^1]
The extraction serves another consumer, Weeder, formerly named weed; it is not primarily a CI optimization.[^1]
Weeder v0.2.2 pins `tilth-core` to the exact PR #210 head, so this is a released consumer, not a proposed one.[^6]
Weeder checks diffs with deterministic rules for weakened tests, swallowed errors, secrets, and related defects.[^7]
It uses no language model and reports findings as terminal text or SARIF.
The project changes its name because SeaweedFS already uses the `weed` executable name.[^8]

[^6]: https://github.com/jahala/weeder/blob/v0.2.2/Cargo.toml
[^7]: https://github.com/jahala/weeder/blob/1a43f398222392e1c1b5f0469def7a49f485c0c8/README.md
[^8]: https://github.com/jahala/weeder/commit/1f42bedb07c4614cc39f5fa9cf180bd7d7d52166

## Measured evidence

The September 5, 2026 check job takes 69 seconds on #210.
Its Clippy step takes 27 seconds, and its workspace test step takes 23 seconds.[^2]
The base PR's check job takes 74 seconds, with 29 seconds for Clippy and 24 seconds for tests.[^3]
These are single-run observations, not a controlled performance comparison.
The #210 cache step reports no cache found and `cache-workspace-crates: false`.[^2]
The action excludes workspace crates from its cache by default.[^4]
Thus, another workspace crate does not automatically provide a persistent CI compilation cache.

## Check coverage caveat

The dependency-review workflow selects pull requests into `main`.
PR #210 targets `garden/map`, so its green check set does not establish dependency-review success against `main`.[^5]
Do not substitute this upstream evidence for the fork's gates in [Local gate gotchas](./local-gate-gotchas.md).

## Recommendation

Keep the two-crate extraction unless controlled build measurements identify another useful boundary.
Measure clean builds, cached CI builds, and face-only changes before proposing more crates.
Treat more pull requests as a review-scope choice, not an established CI speed improvement.

[^1]: https://github.com/jahala/tilth/pull/210
[^2]: https://github.com/jahala/tilth/actions/runs/33988163020/job/101365448178
[^3]: https://github.com/jahala/tilth/actions/runs/33981670435/job/101347877542
[^4]: https://github.com/Swatinem/rust-cache/blob/e18b497796c12c097a38f9edb9d0641fb99eee32/README.md
[^5]: https://github.com/jahala/tilth/blob/7f38db58696e16c2df2be71da985f47097f34920/.github/workflows/dependency-review.yml
