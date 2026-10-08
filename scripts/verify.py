#!/usr/bin/env python3
"""Run the local gate, or one targeted command, with sccache when available.

No arguments runs the same commands as the CI `check` job. `--job <name>` runs
another CI job's commands, for example `--job benchmark-tests`. Any other
arguments run as one command, for example `just verify cargo test edit`.
"""

import os
from pathlib import Path
import shutil
import subprocess
import sys

# One entry per job in .github/workflows/ci.yml; keep the two in step.
# `just check` runs only `check`: benchmark evolve runs `just check` for every
# candidate, and the benchmark suite needs its own Python dependencies.
GATE_COMMANDS = {
    "check": [
        ["cargo", "fmt", "--check"],
        ["cargo", "clippy", "--all-targets", "--", "-D", "warnings"],
        ["cargo", "test"],
        ["python3", "-m", "unittest", "discover", "-s", "tests/mcp_v2", "-t", "tests/mcp_v2"],
        ["python3", "-m", "unittest", "discover", "-s", "tests/scripts", "-t", "tests/scripts"],
        ["python3", "scripts/tilth-bash-guard", "--self-test"],
    ],
    "benchmark-tests": [
        ["python3", "-m", "pytest", "benchmark/tests", "-q"],
    ],
}

WRAPPER_VARIABLES = (
    "RUSTC_WRAPPER",
    "CARGO_BUILD_RUSTC_WRAPPER",
    "RUSTC_WORKSPACE_WRAPPER",
    "CARGO_BUILD_RUSTC_WORKSPACE_WRAPPER",
)


def configure_sccache(environment: dict[str, str]) -> None:
    # Any explicit wrapper variable wins, including an empty opt-out value.
    if any(name in environment for name in WRAPPER_VARIABLES):
        return
    # sccache cannot cache incremental builds, so respect an explicit request for them.
    if environment.get("CARGO_INCREMENTAL") == "1":
        return
    wrapper = shutil.which("sccache", path=environment.get("PATH"))
    if wrapper is None:
        return
    environment["RUSTC_WRAPPER"] = str(Path(wrapper).absolute())
    environment["CARGO_INCREMENTAL"] = "0"


def main(arguments: list[str]) -> int:
    environment = os.environ.copy()
    configure_sccache(environment)
    if not arguments:
        commands = GATE_COMMANDS["check"]
    elif arguments[0] == "--job":
        if len(arguments) != 2 or arguments[1] not in GATE_COMMANDS:
            print(f"usage: verify.py --job {{{','.join(GATE_COMMANDS)}}}", file=sys.stderr)
            return 2
        commands = GATE_COMMANDS[arguments[1]]
    else:
        commands = [arguments]
    for command in commands:
        status = subprocess.run(command, env=environment).returncode
        if status != 0:
            return status
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
