"""The one subprocess entry point for loading, preparing, admitting, and grading external tasks."""

import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path


def run(
    argv: Sequence[str],
    *,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    input: str | bytes | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """Run ``argv`` to completion, capturing output; never raises on a nonzero exit."""
    return subprocess.run(
        list(argv), cwd=cwd, env=None if env is None else dict(env), timeout=timeout,
        input=input, text=text, capture_output=True, check=False,
    )
