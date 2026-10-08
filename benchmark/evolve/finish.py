"""Publishing a winner: push one branch and open one draft PR. Nothing here merges."""

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .gitops import git

ALLOWED_WINNER_PREFIXES = ("src/", "prompts/")
ALLOWED_WINNER_FILES = ("AGENTS.md",)


class PRClient(Protocol):
    def push(self, sha: str, branch: str) -> None: ...

    def create_draft(self, *, base: str, head: str, title: str, body: str) -> object: ...


def outside_allowlist(paths: list[str]) -> list[str]:
    return [path for path in paths if not path.startswith(ALLOWED_WINNER_PREFIXES) and path not in ALLOWED_WINNER_FILES]


class GitHubPRClient:
    """Pushes the winner commit as a branch with git and opens a draft PR with ``gh``."""

    def __init__(self, repo: Path, *, remote: str = "origin",
                 run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> None:
        self.repo = Path(repo)
        self.remote = remote
        self.run = run

    def push(self, sha: str, branch: str) -> None:
        """Create ``branch`` on the remote at ``sha``; a branch that already exists there is refused."""
        git("push", f"--force-with-lease=refs/heads/{branch}:", self.remote, f"{sha}:refs/heads/{branch}",
            cwd=self.repo)

    def create_draft(self, *, base: str, head: str, title: str, body: str) -> dict:
        completed = self.run(["gh", "pr", "create", "--draft", "--base", base, "--head", head, "--title", title,
                              "--body", body], cwd=self.repo, capture_output=True, text=True, check=True)
        return {"url": completed.stdout.strip(), "base": base, "head": head}
