from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import check_task
from tasks.base import Mutation, TaskSource


class FakeTask:
    def __init__(
        self,
        name: str = "fixture_task",
        *,
        repo: str = "synthetic",
        mutations: list[Mutation] | None = None,
        test_command: list[str] | None = None,
        apply: Mock | None = None,
        capability: str = "fix",
    ) -> None:
        self.name = name
        self.repo = repo
        self.mutations = mutations if mutations is not None else [
            Mutation("broken.py", "good", "bad")
        ]
        self.test_command = test_command if test_command is not None else ["pytest", "-q"]
        self.apply = apply or Mock()
        self.capability = capability
        self.source = TaskSource()

    def apply_mutations(self, repo_path: str) -> None:
        self.apply(repo_path)


def _completed(command: list[str], returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=stderr)


def _install_task(monkeypatch, task: FakeTask) -> None:
    monkeypatch.setattr(check_task, "TASKS", {task.name: task})


def test_mutation_task_requires_pass_before_and_fail_after_then_cleans_up(
    monkeypatch, tmp_path, capsys
):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    task = FakeTask()
    _install_task(monkeypatch, task)
    events: list[object] = []
    task.apply.side_effect = lambda path: events.append(("apply", path))
    monkeypatch.setattr(check_task, "get_repo_path", lambda repo: events.append(("path", repo)) or repo_path)
    monkeypatch.setattr(check_task, "reset_repo", lambda: events.append("reset"))

    outcomes = iter([_completed(task.test_command, 0), _completed(task.test_command, 1)])

    def run(command, *, cwd, capture_output, text):
        events.append(("run", command, cwd, capture_output, text))
        return next(outcomes)

    monkeypatch.setattr(check_task.subprocess, "run", run)
    assert check_task.main([task.name]) == 0
    assert events == [
        ("path", "synthetic"),
        "reset",
        ("run", task.test_command, repo_path, True, True),
        ("apply", str(repo_path)),
        ("run", task.test_command, repo_path, True, True),
        "reset",
    ]
    task.apply.assert_called_once_with(str(repo_path))
    assert "PASS fixture_task: baseline passed; mutation failed" in capsys.readouterr().out

def test_mutation_task_requires_nonempty_test_command(monkeypatch, tmp_path, capsys):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    task = FakeTask(test_command=[])
    _install_task(monkeypatch, task)
    events: list[str] = []
    monkeypatch.setattr(check_task, "get_repo_path", lambda repo: repo_path)
    monkeypatch.setattr(check_task, "reset_repo", lambda: events.append("reset"))
    run = Mock()
    monkeypatch.setattr(check_task.subprocess, "run", run)

    assert check_task.main([task.name]) == 1
    assert events == ["reset", "reset"]
    run.assert_not_called()
    task.apply.assert_not_called()
    assert capsys.readouterr().out == (
        "FAIL fixture_task: mutation task requires a nonempty test_command\n"
    )



def test_preexisting_failure_rejects_without_applying_mutations(monkeypatch, tmp_path, capsys):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    task = FakeTask()
    _install_task(monkeypatch, task)
    events: list[str] = []
    monkeypatch.setattr(check_task, "get_repo_path", lambda repo: repo_path)
    monkeypatch.setattr(check_task, "reset_repo", lambda: events.append("reset"))
    monkeypatch.setattr(
        check_task.subprocess,
        "run",
        lambda *args, **kwargs: (events.append("run") or _completed(task.test_command, 1, "before out\n", "before err\n")),
    )

    assert check_task.main([task.name]) == 1
    assert events == ["reset", "run", "reset"]
    task.apply.assert_not_called()
    captured = capsys.readouterr()
    assert "FAIL fixture_task: pre-mutation test failed (exit code 1)" in captured.out
    assert "before out" in captured.out
    assert "before err" in captured.err


def test_mutation_that_does_not_break_tests_is_rejected(monkeypatch, tmp_path, capsys):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    task = FakeTask()
    _install_task(monkeypatch, task)
    events: list[str] = []
    monkeypatch.setattr(check_task, "get_repo_path", lambda repo: repo_path)
    monkeypatch.setattr(check_task, "reset_repo", lambda: events.append("reset"))
    outcomes = iter([_completed(task.test_command, 0), _completed(task.test_command, 0)])
    monkeypatch.setattr(
        check_task.subprocess,
        "run",
        lambda *args, **kwargs: (events.append("run") or next(outcomes)),
    )

    assert check_task.main([task.name]) == 1
    assert events == ["reset", "run", "run", "reset"]
    task.apply.assert_called_once_with(str(repo_path))
    assert "FAIL fixture_task: mutation did not make test_command fail" in capsys.readouterr().out


def test_unknown_task_is_rejected_before_repo_or_subprocess_work(monkeypatch, capsys):
    monkeypatch.setattr(check_task, "TASKS", {})
    reset = Mock()
    run = Mock()
    monkeypatch.setattr(check_task, "reset_repo", reset)
    monkeypatch.setattr(check_task.subprocess, "run", run)

    assert check_task.main(["missing_task"]) == 2
    assert capsys.readouterr().err == "Unknown task: missing_task\n"
    reset.assert_not_called()
    run.assert_not_called()


def test_cleanup_runs_when_mutation_application_raises(monkeypatch, tmp_path, capsys):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    apply = Mock(side_effect=RuntimeError("mutation exploded"))
    task = FakeTask(apply=apply)
    _install_task(monkeypatch, task)
    events: list[str] = []
    monkeypatch.setattr(check_task, "get_repo_path", lambda repo: repo_path)
    monkeypatch.setattr(check_task, "reset_repo", lambda: events.append("reset"))
    monkeypatch.setattr(
        check_task.subprocess,
        "run",
        lambda *args, **kwargs: (events.append("run") or _completed(task.test_command, 0)),
    )

    assert check_task.main([task.name]) == 1
    assert events == ["reset", "run", "reset"]
    assert "FAIL fixture_task: mutation application failed: mutation exploded" in capsys.readouterr().out


def test_cleanup_runs_when_subprocess_raises_and_preserves_exception_diagnostics(
    monkeypatch, tmp_path, capsys
):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    task = FakeTask()
    _install_task(monkeypatch, task)
    events: list[str] = []
    monkeypatch.setattr(check_task, "get_repo_path", lambda repo: repo_path)
    monkeypatch.setattr(check_task, "reset_repo", lambda: events.append("reset"))
    error = subprocess.CalledProcessError(
        7,
        task.test_command,
        output="raised out\n",
        stderr="raised err\n",
    )
    monkeypatch.setattr(
        check_task.subprocess,
        "run",
        lambda *args, **kwargs: (events.append("run") or (_ for _ in ()).throw(error)),
    )

    assert check_task.main([task.name]) == 1
    assert events == ["reset", "run", "reset"]
    captured = capsys.readouterr()
    assert "FAIL fixture_task: pre-mutation test command raised CalledProcessError" in captured.out
    assert "raised out" in captured.out
    assert "raised err" in captured.err


def test_cleanup_failure_is_reported_after_otherwise_valid_preflight(monkeypatch, tmp_path, capsys):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    task = FakeTask()
    _install_task(monkeypatch, task)
    monkeypatch.setattr(check_task, "get_repo_path", lambda repo: repo_path)
    reset = Mock(side_effect=[None, RuntimeError("cleanup exploded")])
    monkeypatch.setattr(check_task, "reset_repo", reset)
    outcomes = iter([_completed(task.test_command, 0), _completed(task.test_command, 1)])
    monkeypatch.setattr(check_task.subprocess, "run", lambda *args, **kwargs: next(outcomes))

    assert check_task.main([task.name]) == 1
    assert reset.call_count == 2
    assert capsys.readouterr().out == (
        "FAIL fixture_task: cleanup failed: RuntimeError: cleanup exploded\n"
    )


def test_metadata_validator_rejects_invalid_capability_and_provenance():
    task = FakeTask(capability="unknown")

    assert check_task.validate_task_metadata(task) == (
        "invalid capability 'unknown'; expected one of control, debug, fix, locate, trace"
    )

    task.capability = "fix"
    task.source = TaskSource(origin="")
    assert check_task.validate_task_metadata(task) == "source.origin must be a nonempty string"

    task.repo = "clone"
    task.source = TaskSource()
    assert (
        check_task.validate_task_metadata(task)
        == "external task source must identify its pinned repository"
    )

def test_cloned_repo_resets_to_configured_revision(monkeypatch, tmp_path):
    repo_path = tmp_path / "clone"
    repo_path.mkdir()
    task = FakeTask(repo="clone")
    task.source = TaskSource(origin="clone", commit_or_tag="pinned-revision")
    _install_task(monkeypatch, task)
    events: list[object] = []
    monkeypatch.setattr(check_task, "get_repo_path", lambda repo: events.append(("path", repo)) or repo_path)
    monkeypatch.setattr(
        check_task,
        "REPOS",
        {"clone": SimpleNamespace(commit_sha="pinned-revision")},
    )
    monkeypatch.setattr(
        check_task,
        "ensure_repo_clean",
        lambda path, revision: events.append(("clean", path, revision)),
    )
    outcomes = iter([_completed(task.test_command, 0), _completed(task.test_command, 1)])
    monkeypatch.setattr(check_task.subprocess, "run", lambda *args, **kwargs: next(outcomes))

    assert check_task.main([task.name]) == 0
    assert events == [
        ("path", "clone"),
        ("clean", repo_path, "pinned-revision"),
        ("clean", repo_path, "pinned-revision"),
    ]


def test_nonmutation_task_reports_no_preflight_and_succeeds(monkeypatch, capsys):
    task = FakeTask(mutations=[], test_command=[])
    _install_task(monkeypatch, task)
    reset = Mock()
    monkeypatch.setattr(check_task, "reset_repo", reset)

    assert check_task.main([task.name]) == 0
    assert capsys.readouterr().out == "PASS fixture_task: no mutation preflight needed\n"
    reset.assert_not_called()


def test_tasks_json_restricts_universe_and_routes_mined_tasks(monkeypatch, tmp_path):
    mined_task = FakeTask(name="mined_task", mutations=[], test_command=[])
    mined_task.base_sha = "a" * 40
    mined_task.head_sha = "b" * 40

    def fake_load_json_tasks(path):
        return {"mined_task": mined_task}

    monkeypatch.setattr(check_task, "load_json_tasks", fake_load_json_tasks)
    monkeypatch.setattr(check_task, "TASKS", {"other_task": FakeTask(name="other_task")})

    calls: list[str] = []
    monkeypatch.setattr(
        check_task, "check_mined_task",
        lambda name: calls.append(name) or True,
    )
    monkeypatch.setattr(
        check_task, "check_task",
        lambda name: calls.append(name) or True,
    )

    assert check_task.main(["--tasks-json", str(tmp_path / "tasks.json")]) == 0
    assert calls == ["mined_task"]


def test_routing_uses_isinstance_not_duck_typing(monkeypatch, tmp_path):
    """A plain task that happens to carry a `base_sha` attribute must still
    route through `check_task`, not `check_mined_task` (regression for
    hasattr-based routing)."""
    plain_task = FakeTask(name="plain_task", mutations=[], test_command=[])
    plain_task.base_sha = "c" * 40  # duck-typing bait; not a JsonMinedTask
    monkeypatch.setattr(check_task, "TASKS", {"plain_task": plain_task})

    calls: list[str] = []
    monkeypatch.setattr(
        check_task, "check_mined_task",
        lambda name: calls.append(("mined", name)) or True,
    )
    monkeypatch.setattr(
        check_task, "check_task",
        lambda name: calls.append(("plain", name)) or True,
    )

    assert check_task.main(["plain_task"]) == 0
    assert calls == [("plain", "plain_task")]


def _git(*args, cwd, env=None):
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=full_env,
    )


_GIT_ENV = {
    "GIT_AUTHOR_NAME": "dev", "GIT_AUTHOR_EMAIL": "dev@test.com",
    "GIT_COMMITTER_NAME": "dev", "GIT_COMMITTER_EMAIL": "dev@test.com",
}

_TEST_PATCH = (
    "diff --git a/test_arithmetic.py b/test_arithmetic.py\n"
    "--- a/test_arithmetic.py\n"
    "+++ b/test_arithmetic.py\n"
    "@@ -0,0 +1,3 @@\n"
    "+def test_add():\n"
    "+    from arithmetic import add\n"
    "+    assert add(2, 3) == 5\n"
)
_TEST_COMMAND = ["python3", "-m", "pytest", "test_arithmetic.py", "-q"]


def _build_local_mined_repo(tmp_path):
    """Build a bare repo with a buggy base commit and a fixing head commit.

    Returns `(bare_url, base_sha, head_sha)`; `_TEST_PATCH`/`_TEST_COMMAND`
    describe the regression test the patch adds at grading time.
    """
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", str(work), cwd=str(tmp_path))
    (work / "arithmetic.py").write_text("def add(a, b):\n    return a - b\n")
    (work / "test_arithmetic.py").write_text("")
    _git("add", "-A", cwd=str(work), env=_GIT_ENV)
    _git("commit", "-qm", "base", cwd=str(work), env=_GIT_ENV)
    base_sha = _git("rev-parse", "HEAD", cwd=str(work)).stdout.strip()

    (work / "arithmetic.py").write_text("def add(a, b):\n    return a + b\n")
    _git("add", "-A", cwd=str(work), env=_GIT_ENV)
    _git("commit", "-qm", "fix: add instead of subtract", cwd=str(work), env=_GIT_ENV)
    head_sha = _git("rev-parse", "HEAD", cwd=str(work)).stdout.strip()

    bare = tmp_path / "bare.git"
    _git("clone", "-q", "--bare", str(work), str(bare), cwd=str(tmp_path))
    return str(bare), base_sha, head_sha


def _install_mined_task(monkeypatch, tmp_path, task_id, base_sha, head_sha, bare_url, clone_name):
    """Register a `JsonMinedTask` built directly from real shas, cloned fresh."""
    from config import RepoConfig
    from tasks import base as base_module
    from tasks.json_tasks import JsonMinedTask
    from tasks import json_tasks as json_tasks_module

    repo_name = "acme-local"
    task = JsonMinedTask(
        id=task_id, family=task_id, split="train", capability_="fix",
        prompt_="Fix add().", repo_name=repo_name, base_sha=base_sha,
        head_sha=head_sha, test_patch=_TEST_PATCH,
        test_command_=_TEST_COMMAND, reference_changed_lines_=1,
    )
    clone_path = tmp_path / clone_name
    _git("clone", "-q", bare_url, str(clone_path), cwd=str(tmp_path))
    repo_config = RepoConfig(
        name=repo_name, url=bare_url, commit_sha=head_sha,
        language="python", description="test fixture",
        path_override=clone_path,
    )

    monkeypatch.setattr(check_task, "TASKS", {task.name: task})
    monkeypatch.setattr(check_task, "get_repo_path", lambda repo: clone_path)
    monkeypatch.setattr(base_module, "REPOS", {repo_name: repo_config})
    monkeypatch.setattr(json_tasks_module, "REPOS", {repo_name: repo_config})
    return task


def test_check_mined_task_passes_when_base_fails_and_head_passes(monkeypatch, tmp_path):
    bare_url, base_sha, head_sha = _build_local_mined_repo(tmp_path)
    _install_mined_task(monkeypatch, tmp_path, "acme-42", base_sha, head_sha, bare_url, "clone")

    assert check_task.main(["acme-42"]) == 0


def test_check_mined_task_fails_when_test_passes_at_base(monkeypatch, tmp_path, capsys):
    """An already-fixed "base" (base_sha == head_sha) passes the regression
    test immediately, so `check_mined_task` must fail before checking head."""
    bare_url, _base_sha, head_sha = _build_local_mined_repo(tmp_path)
    _install_mined_task(monkeypatch, tmp_path, "acme-43", head_sha, head_sha, bare_url, "clone2")

    assert check_task.main(["acme-43"]) == 1
    assert "passed at base_sha" in capsys.readouterr().out


def test_check_mined_task_fails_when_test_still_fails_at_head(monkeypatch, tmp_path, capsys):
    """A "head" that never applied the fix (head_sha == base_sha) still
    fails the regression test, so `check_mined_task` must fail."""
    bare_url, base_sha, _head_sha = _build_local_mined_repo(tmp_path)
    _install_mined_task(monkeypatch, tmp_path, "acme-44", base_sha, base_sha, bare_url, "clone3")

    assert check_task.main(["acme-44"]) == 1
    assert "failed at head_sha" in capsys.readouterr().out