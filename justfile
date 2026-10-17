set positional-arguments

# tilth dev commands — run `just` to list recipes.

# Worker count for cargo -j and test --test-threads: the cores free when the recipe starts, at least 2.
# Several worktrees share one host, so a fixed count per core overloads it.
# Override with `just jobs=8 check` or `TILTH_JOBS=8 just check`.
jobs := env("TILTH_JOBS", `python3 -c 'import os; print(max(2, int((os.cpu_count() or 1) - os.getloadavg()[0])))'`)

# cargo reads CARGO_BUILD_JOBS as -j; libtest and nextest read these as --test-threads.
# An explicit flag on a `just verify` command still wins.
export CARGO_BUILD_JOBS := jobs
export RUST_TEST_THREADS := jobs
export NEXTEST_TEST_THREADS := jobs

default:
    @just --list

# Run the CI gate: fmt, clippy, tests, MCP suite, script tests, bash-guard self-test.
check:
    python3 scripts/verify.py

# Run the CI benchmark-tests job; needs `pip install -r benchmark/requirements-dev.txt`.
check-benchmark:
    python3 scripts/verify.py --job benchmark-tests

# Run one command with the same sccache setup, e.g. `just verify cargo test edit`.
verify *args:
    python3 scripts/verify.py "$@"
