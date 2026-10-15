"""Tests for tasks/json_tasks.py: mutation-shape and mined-shape JSON task
sources (AC-3's `load_json_tasks` contract)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tasks import json_tasks
from tasks.base import Mutation

MUTATION_FIXTURE = Path(__file__).parent / "fixtures" / "json_tasks_mutation" / "agent_tasks.json"


@pytest.fixture(autouse=True)
def isolated_repos(monkeypatch, tmp_path):
    """Give every test its own REPOS registry and REPOS_DIR so ids and
    on-disk bootstrap copies never collide across tests."""
    monkeypatch.setattr(json_tasks, "REPOS", {})
    monkeypatch.setattr(json_tasks, "REPOS_DIR", tmp_path / "repos")


def test_load_json_tasks_detects_mutation_shape_and_bootstraps_git(tmp_path) -> None:
    tasks = json_tasks.load_json_tasks(MUTATION_FIXTURE)

    assert set(tasks) == {"greet-missing-comma"}
    task = tasks["greet-missing-comma"]
    assert task.name == "greet-missing-comma"
    assert task.capability == "fix"
    assert task.task_type == "edit"
    assert task.mutations == [
        Mutation("greet.py", 'return f"hi, {name}"', 'return f"hi {name}"')
    ]
    assert task.test_command == [
        "python3", "-c", "import greet; assert greet.greet('x') == 'hi, x'",
    ]

    source_dir = (MUTATION_FIXTURE.parent / "repo").resolve()
    repo_cfg = json_tasks.REPOS[task.repo]
    assert repo_cfg.path != source_dir
    assert (repo_cfg.path / ".git").exists()
    assert (repo_cfg.path / "greet.py").exists()
    # Bootstrap never writes into the source fixture tree.
    assert not (source_dir / ".git").exists()


def test_load_json_tasks_mutation_shape_is_idempotent(tmp_path) -> None:
    json_tasks.load_json_tasks(MUTATION_FIXTURE)
    # A second load must not fail even though the fixture repo is already a
    # git repository (bootstrap must be a no-op the second time).
    tasks = json_tasks.load_json_tasks(MUTATION_FIXTURE)
    assert "greet-missing-comma" in tasks


def test_load_json_tasks_detects_mined_shape_without_cloning(monkeypatch, tmp_path) -> None:
    mined_json = tmp_path / "mined.json"
    mined_json.write_text(json.dumps({
        "version": 1,
        "tasks": [{
            "id": "acme-42",
            "family": "acme-42",
            "split": "train",
            "capability": "fix",
            "prompt": "Fix the off-by-one in acme.",
            "test_command": ["python3", "-m", "pytest", "tests/test_acme.py", "-q"],
            "repo_url": "https://example.com/acme/acme.git",
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "test_patch": "diff --git a/tests/test_acme.py b/tests/test_acme.py\n",
            "reference_changed_lines": 3,
        }],
    }))

    run_calls: list[list[str]] = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, *a, **k: run_calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )

    tasks = json_tasks.load_json_tasks(mined_json)

    assert not run_calls  # no clone or any subprocess call at load time
    assert set(tasks) == {"acme-42"}
    task = tasks["acme-42"]
    assert task.name == "acme-42"
    assert task.capability == "fix"
    assert task.task_type == "edit"
    assert task.test_command == ["python3", "-m", "pytest", "tests/test_acme.py", "-q"]
    assert task.reference_changed_lines == 3
    assert task.lint_command == []
    # The agent must not see head_sha or later history; the workspace hides
    # `.git` for mined tasks.
    assert task.hide_git is True

    repo_cfg = json_tasks.REPOS[task.repo]
    assert repo_cfg.on_demand_clone is True
    assert repo_cfg.url == "https://example.com/acme/acme.git"
    assert repo_cfg.commit_sha == "a" * 40


def test_ensure_mined_repo_cloned_is_deferred_to_first_access(monkeypatch, tmp_path) -> None:
    import config as config_module
    monkeypatch.setattr(config_module, "REPOS_DIR", tmp_path)
    repo_cfg = json_tasks.RepoConfig(
        name="json-mined-acme-42",
        url="https://example.com/acme/acme.git",
        commit_sha="a" * 40,
        language="",
        description="mined fixture",
        on_demand_clone=True,
    )
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["git", "clone"]:
            Path(cmd[3]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(json_tasks.subprocess, "run", fake_run)

    json_tasks.ensure_mined_repo_cloned(repo_cfg)

    assert calls  # clone was attempted once path was actually needed
    assert calls[0][:2] == ["git", "clone"]
    assert calls[0][2] == repo_cfg.url
    assert calls[1][:2] == ["git", "checkout"]
    assert calls[1][2] == repo_cfg.commit_sha
    assert repo_cfg.path.exists()

    calls.clear()
    json_tasks.ensure_mined_repo_cloned(repo_cfg)
    assert not calls  # already cloned; second call is a no-op


def test_mined_task_hidden_workspace_lacks_head_sha(tmp_path) -> None:
    """The agent's workspace must not expose head_sha or later history."""
    sys.path.insert(0, str(Path(__file__).parent.parent))
    import run as run_module

    source = tmp_path / "source"
    source.mkdir()
    (source / "code.py").write_text("value = 1\n")
    env = {
        **__import__("os").environ,
        "GIT_AUTHOR_NAME": "dev", "GIT_AUTHOR_EMAIL": "dev@test.com",
        "GIT_COMMITTER_NAME": "dev", "GIT_COMMITTER_EMAIL": "dev@test.com",
    }
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "base"], check=True, env=env)
    (source / "code.py").write_text("value = 2\n")
    subprocess.run(["git", "-C", str(source), "add", "-A"], check=True, env=env)
    head = subprocess.run(
        ["git", "-C", str(source), "commit", "-qm", "fix: reference commit"],
        check=True, env=env,
    )
    head_sha = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()

    task = json_tasks.JsonMinedTask(
        id="acme-hide", family="acme-hide", split="train", capability_="fix",
        prompt_="fix it", repo_name="acme-hide-repo", base_sha="a" * 40,
        head_sha=head_sha, test_patch="", test_command_=[],
        reference_changed_lines_=1,
    )
    assert task.hide_git is True

    with run_module._agent_repo(source, task.hide_git) as workspace:
        assert not (workspace / ".git").exists()
        log = subprocess.run(
            ["git", "log"], cwd=str(workspace), capture_output=True, text=True,
        )
        assert head_sha not in log.stdout
