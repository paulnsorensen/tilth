"""Publishing a winner: push one branch and open one draft PR. Nothing here merges."""

import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .gitops import GitError, git
from .materialize import PHRASE_PIN_EXEMPT_ENV

# The draft PR body and the run log carry this; the word "merge" stays out of every `gh pr create` argument.
REPIN_NOTE = (f"Phrase pins were exempt during the search: each candidate's `just check` set {PHRASE_PIN_EXEMPT_ENV}=1, "
              "so the Rust tests skipped their exact-phrase asserts on prompts/ text. CI does not set it. "
              "Re-pin those asserts to this PR's prompt text before it lands.")
TEST_REVIEW_NOTE = ("The tests below are new, or a repair updated them because the intended behavior change made "
                    "their old asserts obsolete. Check that each new test checks the new behavior and that no test "
                    "is deleted or weakened.")
ALLOWED_WINNER_PREFIXES = ("src/", "prompts/")
ALLOWED_WINNER_FILES = ("AGENTS.md",)
_GITHUB_URL = re.compile(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)(?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?")


class PRClient(Protocol):
    def push(self, sha: str, branch: str) -> None: ...

    def create_draft(self, *, base: str, head: str, title: str, body: str) -> object: ...


def outside_allowlist(paths: list[str]) -> list[str]:
    return [path for path in paths if not path.startswith(ALLOWED_WINNER_PREFIXES) and path not in ALLOWED_WINNER_FILES]


def changed_tests_section(changes: dict[str, str]) -> list[str]:
    """PR body lines for the tests the winner changes, each with its diff; nothing when it changes none."""
    if not changes:
        return []
    lines = ["## Test changes for review", TEST_REVIEW_NOTE, ""]
    for name, diff in sorted(changes.items()):
        lines += [f"### `{name}`", "```diff", diff, "```", ""]
    return lines


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
        completed = self.run(["gh", "pr", "create", *self._repo_flag(), "--draft", "--base", base, "--head", head,
                              "--title", title, "--body", body],
                             cwd=self.repo, capture_output=True, text=True, check=True)
        return {"url": completed.stdout.strip(), "base": base, "head": head}

    def _repo_flag(self) -> list[str]:
        try:
            url = git("remote", "get-url", self.remote, cwd=self.repo).strip()
        except GitError:
            return []
        match = _GITHUB_URL.fullmatch(url)
        return ["--repo", f"{match['owner']}/{match['name']}"] if match else []
