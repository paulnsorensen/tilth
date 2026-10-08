"""Small git helpers for the evolve loop."""

import os
import subprocess
from pathlib import Path

# Candidate commits carry a fixed tool identity, so a host without git user config can commit.
COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "tilth-evolve", "GIT_AUTHOR_EMAIL": "tilth-evolve@localhost",
    "GIT_COMMITTER_NAME": "tilth-evolve", "GIT_COMMITTER_EMAIL": "tilth-evolve@localhost",
}


class GitError(RuntimeError):
    def __init__(self, args: list[str], output: str) -> None:
        super().__init__(f"git {' '.join(args)} failed: {output.strip()[-500:]}")
        self.output = output


def git_bytes(*args: str, cwd: Path, input: bytes | None = None, env: dict[str, str] | None = None) -> bytes:
    completed = subprocess.run(["git", *args], cwd=cwd, input=input, capture_output=True,
                               env={**os.environ, **COMMIT_ENV, **(env or {})})
    if completed.returncode != 0:
        raise GitError(list(args), (completed.stdout + completed.stderr).decode(errors="replace"))
    return completed.stdout


def git(*args: str, cwd: Path, input: str | None = None, env: dict[str, str] | None = None) -> str:
    data = None if input is None else input.encode("utf-8")
    return git_bytes(*args, cwd=cwd, input=data, env=env).decode("utf-8", errors="replace")
