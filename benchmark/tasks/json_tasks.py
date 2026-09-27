"""Load benchmark tasks from a JSON file instead of the static `tasks/` package.

Two shapes are supported, detected by whether the JSON has a top-level
`repo` key:

- Mutation shape (top-level `repo`): a local fixture directory with a fixed
  file mutation and a `test_command`, e.g. the `#722` mined-candidate fixture
  shape. The fixture is copied into `REPOS_DIR` and git-bootstrapped there
  (never in the source tree), so `check_task`/`fixtures.reset` (which expect
  a real `.git`) can manage the copy.
- Mined shape (no top-level `repo`): each task self-describes `repo_url`,
  `base_sha`, `head_sha`, and a `test_patch` (the PR's test-file diff), as
  written by `agent_lab_mine.py`. The repository clone is deferred to first
  access via `on_demand_clone`; loading the JSON never touches the network.
  The agent workspace hides `.git` (`hide_git=True`), so the agent cannot
  read `head_sha` or any other post-base history. `test_patch` is applied
  only at grading time (`JsonMinedTask.check_correctness`), never before the
  agent runs, so the agent cannot see or weaken the regression test.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from config import REPOS, REPOS_DIR, RepoConfig
from tasks.base import GroundTruth, Mutation, Task

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "dev",
    "GIT_AUTHOR_EMAIL": "dev@test.com",
    "GIT_COMMITTER_NAME": "dev",
    "GIT_COMMITTER_EMAIL": "dev@test.com",
}


@dataclass
class JsonMutationTask(Task):
    """A fixed-mutation task loaded from a mutation-shape JSON file."""

    id: str
    family: str
    split: str
    capability_: str
    prompt_: str
    repo_name: str
    mutations_: list[Mutation]
    test_command_: list[str]
    ground_truth_: GroundTruth

    @property
    def name(self) -> str:
        return self.id

    @property
    def prompt(self) -> str:
        return self.prompt_

    @property
    def ground_truth(self) -> GroundTruth:
        return self.ground_truth_

    @property
    def task_type(self) -> str:
        return "edit"

    @property
    def repo(self) -> str:
        return self.repo_name

    @property
    def capability(self) -> str:
        return self.capability_

    @property
    def mutations(self) -> list[Mutation]:
        return self.mutations_

    @property
    def test_command(self) -> list[str]:
        return self.test_command_


def _test_patch_paths(patch_text: str) -> list[str]:
    """Return the `b/`-side paths a unified diff touches."""
    return [
        line[len("+++ b/"):].rstrip("\n")
        for line in patch_text.splitlines()
        if line.startswith("+++ b/")
    ]


def _git_show(repo_dir: Path, revision: str, rel_path: str) -> Optional[str]:
    """Return a path's content at `revision`, or None if it did not exist there."""
    result = subprocess.run(
        ["git", "show", f"{revision}:{rel_path}"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    return result.stdout if result.returncode == 0 else None


@dataclass
class JsonMinedTask(Task):
    """A SHA-pinned mined fix task loaded from a mined-shape JSON file."""

    id: str
    family: str
    split: str
    capability_: str
    prompt_: str
    repo_name: str
    base_sha: str
    head_sha: str
    test_patch: str
    test_command_: list[str]
    reference_changed_lines_: int
    lint_command_: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.id

    @property
    def prompt(self) -> str:
        return self.prompt_

    @property
    def ground_truth(self) -> GroundTruth:
        return GroundTruth()

    @property
    def task_type(self) -> str:
        return "edit"

    @property
    def repo(self) -> str:
        return self.repo_name

    @property
    def capability(self) -> str:
        return self.capability_

    @property
    def test_command(self) -> list[str]:
        return self.test_command_

    @property
    def reference_changed_lines(self) -> int:
        return self.reference_changed_lines_

    @property
    def lint_command(self) -> list[str]:
        return self.lint_command_

    @property
    def hide_git(self) -> bool:
        # The agent must never see `head_sha`, the reference-fix commit, or
        # any other post-base history: the workspace gets a `.git`-free copy.
        return True

    def check_correctness(self, result_text: str, repo_path: str) -> tuple[bool, str]:
        """Reset `test_patch`'s paths to `base_sha`, apply it, then grade.

        Resetting first means an agent that edited or deleted the new test
        cannot win: grading always runs the PR's own regression test against
        whatever fix code the agent left in place, taken from `base_sha` in
        the source clone's git history (not from the `.git`-free workspace).
        """
        source_dir = REPOS[self.repo_name].path
        workspace = Path(repo_path)
        for rel_path in _test_patch_paths(self.test_patch):
            base_content = _git_show(source_dir, self.base_sha, rel_path)
            dest_file = workspace / rel_path
            if base_content is None:
                if dest_file.exists():
                    dest_file.unlink()
                continue
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            dest_file.write_text(base_content)

        subprocess.run(
            ["git", "apply", "-"],
            input=self.test_patch, cwd=str(workspace),
            check=True, capture_output=True, text=True,
        )

        if not self.test_command_:
            return False, "mined task requires a nonempty test_command"
        result = subprocess.run(
            self.test_command_, cwd=str(workspace), capture_output=True, text=True,
            timeout=300,
        )
        if result.returncode != 0:
            return False, f"Test failed: {shlex.join(self.test_command_)}"
        return True, "Test passed"


def _register_mutation_repo(json_path: Path, repo_rel: str) -> str:
    """Copy the fixture directory named by a mutation-shape `repo` key into
    `REPOS_DIR` and git-bootstrap the copy. Never writes a `.git` into the
    source tree."""
    source_dir = (json_path.parent / repo_rel).resolve()
    repo_name = f"json-mutation:{json_path.stem}:{repo_rel}"
    repo_dir = REPOS_DIR / repo_name.replace(":", "_").replace(os.sep, "_")
    if not repo_dir.exists():
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_dir, repo_dir, ignore=shutil.ignore_patterns(".git"))
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True, env=_GIT_ENV)
        subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True, capture_output=True, env=_GIT_ENV)
        subprocess.run(
            ["git", "commit", "-m", "chore: bootstrap fixture repo"],
            cwd=repo_dir, check=True, capture_output=True, env=_GIT_ENV,
        )
    REPOS[repo_name] = RepoConfig(
        name=repo_name,
        url=str(repo_dir),
        commit_sha="HEAD",
        language="",
        description="json fixture repo",
        path_override=repo_dir,
    )
    return repo_name


def _repo_slug(repo_url: str) -> str:
    """Return a filesystem-friendly repo name from a clone URL."""
    name = repo_url.rstrip("/").rsplit("/", 1)[-1]
    return name[: -len(".git")] if name.endswith(".git") else name


def _register_mined_repo(repo_url: str, base_sha: str) -> str:
    """Register (without cloning) the on-demand repo for a `(repo_url,
    base_sha)` pair. Naming by repo+base (not task id) means mined tasks that
    share a repo and base commit share one clone."""
    repo_name = f"{_repo_slug(repo_url)}-{base_sha[:12]}"
    if repo_name not in REPOS:
        REPOS[repo_name] = RepoConfig(
            name=repo_name,
            url=repo_url,
            commit_sha=base_sha,
            language="",
            description=f"mined fixture repo for {repo_url}@{base_sha[:12]}",
            on_demand_clone=True,
        )
    return repo_name


def ensure_mined_repo_cloned(repo_cfg: RepoConfig) -> None:
    """Clone and check out an on-demand repo the first time its path is needed.

    Clones into a sibling staging directory and renames into place on success,
    so a clone that fails partway never leaves a half-cloned path behind for
    the next call to mistake as already done.
    """
    path = repo_cfg.path
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(path.parent)) as staging:
        clone_path = Path(staging) / path.name
        subprocess.run(
            ["git", "clone", repo_cfg.url, str(clone_path)],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "checkout", repo_cfg.commit_sha],
            cwd=str(clone_path), check=True, capture_output=True, text=True,
        )
        os.rename(str(clone_path), str(path))


def _build_mutation_task(raw: dict, repo_name: str) -> JsonMutationTask:
    mutations = [Mutation(**m) for m in raw.get("mutations", [])]
    ground_truth = GroundTruth(
        forbidden_strings=raw.get("forbidden_strings", GroundTruth().forbidden_strings),
    )
    return JsonMutationTask(
        id=raw["id"],
        family=raw.get("family", raw["id"]),
        split=raw.get("split", "train"),
        capability_=raw.get("capability", "fix"),
        prompt_=raw["prompt"],
        repo_name=repo_name,
        mutations_=mutations,
        test_command_=raw.get("test_command", []),
        ground_truth_=ground_truth,
    )


def _build_mined_task(raw: dict) -> JsonMinedTask:
    repo_name = _register_mined_repo(raw["repo_url"], raw["base_sha"])
    return JsonMinedTask(
        id=raw["id"],
        family=raw.get("family", raw["id"]),
        split=raw.get("split", "train"),
        capability_=raw.get("capability", "fix"),
        prompt_=raw["prompt"],
        repo_name=repo_name,
        base_sha=raw["base_sha"],
        head_sha=raw["head_sha"],
        test_patch=raw.get("test_patch", ""),
        test_command_=raw.get("test_command", []),
        reference_changed_lines_=raw.get("reference_changed_lines", 0),
        lint_command_=raw.get("lint_command") or [],
    )


def load_json_tasks(path: Path) -> dict[str, Task]:
    """Load a JSON task file, returning `{task_id: Task}`.

    A top-level `repo` key selects the mutation shape; its absence selects
    the mined shape.
    """
    path = Path(path)
    data = json.loads(path.read_text())
    raw_tasks = data.get("tasks", [])
    if "repo" in data:
        repo_name = _register_mutation_repo(path, data["repo"])
        return {raw["id"]: _build_mutation_task(raw, repo_name) for raw in raw_tasks}
    return {raw["id"]: _build_mined_task(raw) for raw in raw_tasks}