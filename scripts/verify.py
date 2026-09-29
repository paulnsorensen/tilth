#!/usr/bin/env python3
"""Run the local gate, or one targeted command, with sccache when available.

No arguments runs the same commands as the CI `check` job. Any other
arguments run as one command, for example `just verify cargo test edit`.
"""

import os
from pathlib import Path
import shutil
import subprocess
import sys

GATE_COMMANDS = [
    ["cargo", "fmt", "--check"],
    ["cargo", "clippy", "--all-targets", "--", "-D", "warnings"],
    ["cargo", "test"],
    ["python3", "-m", "unittest", "discover", "-s", "tests/mcp_v2", "-t", "tests/mcp_v2"],
    ["python3", "-m", "unittest", "discover", "-s", "tests/scripts", "-t", "tests/scripts"],
    ["python3", "scripts/tilth-bash-guard", "--self-test"],
]

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
    commands = [arguments] if arguments else GATE_COMMANDS
    for command in commands:
        status = subprocess.run(command, env=environment).returncode
        if status != 0:
            return status
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
