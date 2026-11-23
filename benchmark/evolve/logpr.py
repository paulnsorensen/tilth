"""Publishing a benchmark log entry: one commit on the fetched base branch, one branch, one ready PR.

The run checkout does not change. The commit goes on a temporary detached worktree at the fetched base, and the
worktree goes away after the push, also after a fault. Nothing here merges.
"""

import json
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import Protocol

from . import benchlog
from .finish import GitHubPRClient
from .gitops import git

# The committed log, relative to the repository root; its charts go next to it.
LOG_PATH = Path("benchmark/log/evolve.jsonl")
BRANCH_PREFIX = "benchmark-log/"
# The temporary worktree lives outside the repository under a directory with this prefix.
WORKTREE_PREFIX = "tilth-benchmark-log-"


class PublishError(RuntimeError):
    """One publish step failed; the message is ``<step> failed (<detail>)``."""

    def __init__(self, step: str, detail: str) -> None:
        super().__init__(f"{step} failed ({detail})")
        self.step = step


class LogClient(Protocol):
    def fetch(self, branch: str) -> str: ...

    def remote_branch(self, branch: str) -> str | None: ...

    def push_log(self, sha: str, branch: str, *, lease: str) -> None: ...

    def open_pr(self, *, head: str) -> tuple[str, str] | None: ...

    def create_ready(self, *, base: str, head: str, title: str, body: str) -> str: ...


class GitHubLogClient(GitHubPRClient):
    """Fetches and pushes with git, and finds or opens a ready PR with ``gh``."""

    def fetch(self, branch: str) -> str:
        """Fetch ``branch`` from the remote and return its commit."""
        tracking = f"refs/remotes/{self.remote}/{branch}"
        git("fetch", "--quiet", self.remote, f"+refs/heads/{branch}:{tracking}", cwd=self.repo)
        return git("rev-parse", "--verify", f"{tracking}^{{commit}}", cwd=self.repo).strip()

    def remote_branch(self, branch: str) -> str | None:
        """The commit of ``branch`` on the remote; None when the remote has no such branch."""
        found = git("ls-remote", "--heads", self.remote, f"refs/heads/{branch}", cwd=self.repo).split()
        return found[0] if found else None

    def push_log(self, sha: str, branch: str, *, lease: str) -> None:
        """Set ``branch`` on the remote to ``sha`` only if it is still at ``lease``; an empty lease means absent."""
        git("push", "--quiet", f"--force-with-lease=refs/heads/{branch}:{lease}", self.remote,
            f"{sha}:refs/heads/{branch}", cwd=self.repo)

    def open_pr(self, *, head: str) -> tuple[str, str] | None:
        """The base branch and the URL of the open PR from ``head``; None when there is none."""
        completed = self.run(["gh", "pr", "list", *self._repo_flag(), "--head", head, "--state", "open",
                              "--json", "baseRefName,url"],
                             cwd=self.repo, capture_output=True, text=True, check=True)
        # Parsed as JSON, not split on spaces: a branch name can hold a space that is not ASCII.
        found = json.loads(completed.stdout or "[]")
        return (found[0]["baseRefName"], found[0]["url"]) if found else None

    def create_ready(self, *, base: str, head: str, title: str, body: str) -> str:
        """Open a ready (not draft) PR and return its URL."""
        completed = self.run(["gh", "pr", "create", *self._repo_flag(), "--base", base, "--head", head,
                              "--title", title, "--body", body],
                             cwd=self.repo, capture_output=True, text=True, check=True)
        return completed.stdout.strip()


def _add_worktree(repo: Path, tree: Path, sha: str) -> None:
    git("worktree", "add", "--quiet", "--detach", str(tree), sha, cwd=repo)


def _commit(tree: Path, message: str) -> str:
    git("add", "--", str(LOG_PATH.parent), cwd=tree)
    git("commit", "--quiet", "--no-verify", "-m", message, cwd=tree)
    return git("rev-parse", "HEAD", cwd=tree).strip()


def _remove_worktree(repo: Path, scratch: Path) -> None:
    """Remove the temporary worktree and its directory; a step that fails here leaves the result as it is."""
    try:
        git("worktree", "remove", "--force", str(scratch / "tree"), cwd=repo)
    except Exception:  # noqa: BLE001 - the worktree may not exist; prune below drops a stale record
        pass
    shutil.rmtree(scratch, ignore_errors=True)
    try:
        git("worktree", "prune", cwd=repo)
    except Exception:  # noqa: BLE001 - cleanup only
        pass


def publish_command(run_dir: Path | str, base: str, *, replace: bool = False) -> str:
    """The shell command that publishes ``run_dir``'s entry against ``base``; every message that names one uses it.

    The base is always explicit: the command's default is ``main``, and a rebuild on another base than the PR's
    target would add that base's commits to the PR. Each argument is shell-quoted, so a branch such as
    ``release(testing)`` or a path with a space gives a command that a shell accepts.
    """
    words = ["python3", "benchmark/evolve/benchlog.py", "publish", str(run_dir), "--base-branch", base]
    return shlex.join(words + (["--replace"] if replace else []))


def _body(entry: dict, run_dir: Path | str, base: str) -> str:
    return "\n".join([
        f"Adds the benchmark log entry of evolve run `{entry['run_id']}` to `{LOG_PATH}` and redraws both charts.",
        "",
        f"Outcome: `{entry.get('outcome')}`. Seed: `{entry.get('seed_sha')}`.",
        "",
        f"When another log PR lands on `{base}` first, this PR conflicts. Rebuild it on the new base:",
        "",
        f"    {publish_command(run_dir, base, replace=True)}",
    ])


def publish(run_dir: Path | str, entry: dict, *, repo: Path, base: str, client: LogClient,
            replace: bool = False) -> str:
    """Open a PR that appends ``entry`` to the log of ``base`` and redraws both charts; the PR URL.

    The commit goes on branch ``benchmark-log/<run-id>``. Without ``replace``, a branch that exists on the remote
    is a fault. With ``replace``, the push overwrites that branch with ``--force-with-lease`` and an open PR from
    it stays. An open PR that targets another branch than ``base`` is a fault: the rebuilt branch would add the
    commits of ``base`` to that PR. A fault raises ``PublishError`` and removes the temporary worktree.
    """
    run_id = entry["run_id"]
    branch = BRANCH_PREFIX + run_id
    scratch = None
    step = "fetch"
    try:
        base_sha = client.fetch(base)
        step = "branch"
        current = client.remote_branch(branch)
        if current is not None and not replace:
            raise PublishError(step, f"{branch} exists on the remote; to rebuild it on {base}, run "
                                     f"{publish_command(run_dir, base, replace=True)}")
        existing = client.open_pr(head=branch) if replace else None
        if existing is not None and existing[0] != base:
            raise PublishError(step, f"the open PR {existing[1]} targets {existing[0]}, not {base}; to rebuild it "
                                     f"there, run {publish_command(run_dir, existing[0], replace=True)}")
        step = "worktree"
        scratch = Path(tempfile.mkdtemp(prefix=WORKTREE_PREFIX))
        tree = scratch / "tree"
        _add_worktree(repo, tree, base_sha)
        log = tree / LOG_PATH
        step = "append"
        benchlog.append(log, entry)
        step = "redraw"
        benchlog.redraw(log)
        step = "commit"
        commit = _commit(tree, f"chore(benchmark): log evolve run {run_id}")
        step = "push"
        client.push_log(commit, branch, lease=current or "")
        step = "pr"
        if existing is not None:
            return existing[1]
        return client.create_ready(base=base, head=branch, title=f"chore(benchmark): log evolve run {run_id}",
                                   body=_body(entry, run_dir, base))
    except PublishError:
        raise
    except Exception as error:  # noqa: BLE001 - every fault names its step
        detail = getattr(error, "stderr", None) or str(error)
        raise PublishError(step, f"{type(error).__name__}: {str(detail).strip()}") from error
    finally:
        if scratch is not None:
            _remove_worktree(repo, scratch)
