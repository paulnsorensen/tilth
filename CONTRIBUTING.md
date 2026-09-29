# Contributing

Thanks for your interest. tilth is small and intentionally so — clean, focused changes are easiest to land.

## Workflow

1. Fork, branch, change.
2. Run the gates locally:
   ```bash
   just check
   ```
   `just check` runs `scripts/verify.py`, which runs the same commands as the CI `check` job.
   Use `just verify <command>` to run one command with the same setup, for example `just verify cargo test edit`.
3. Open a PR. Describe what changed and how to test it.

## Optional local compiler caching

Install `sccache` to cache compiler results across builds and worktrees:

```bash
brew install sccache   # or: cargo install sccache --locked
```

`just check` and `just verify` find `sccache` on `PATH` and set `RUSTC_WRAPPER` to its absolute path.
They also set `CARGO_INCREMENTAL=0`, because sccache does not cache incremental builds.
Run `sccache --show-stats` to see cache hits and misses.

Set `RUSTC_WRAPPER=` to opt out, for example `RUSTC_WRAPPER= just check`.
Any explicit Rust wrapper variable or `CARGO_INCREMENTAL=1` also stops the automatic setup.
Direct `cargo` commands do not change.
A wrapper set only in Cargo config is not visible to this check; export the wrapper variable to keep it.

CI uses `sccache` with the GitHub Actions cache for the `check` job and the nightly native builds.

## What helps

- **Small PRs.** Easier to review, easier to merge.
- **Conventional commits.** `fix: ...`, `feat: ...`, `refactor: ...`, `docs: ...`. The log is the changelog.
- **A test.** Bug fixes need a regression test. Features need at least one.
- **Surgical edits.** Don't rewrite surrounding code unrelated to the change.

## Code style

See [CLAUDE.md](./CLAUDE.md) for the project layout and conventions. Match the style of the file you're editing.

## Bigger changes

For anything that adds an MCP tool, changes a tool schema, or restructures a module: open an issue first so we can agree on the shape before you spend time on the implementation.

## License

By contributing, you agree your work is licensed under the project's [MIT License](./LICENSE).
