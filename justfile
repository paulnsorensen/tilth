set positional-arguments

# tilth dev commands — run `just` to list recipes.

default:
    @just --list

# Run the CI gate: fmt, clippy, tests, MCP suite, script tests, bash-guard self-test.
check:
    python3 scripts/verify.py

# Run one command with the same sccache setup, e.g. `just verify cargo test edit`.
verify *args:
    python3 scripts/verify.py "$@"
