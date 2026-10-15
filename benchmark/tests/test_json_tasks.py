"""Tests for tasks/json_tasks.py: mutation-shape and mined-shape JSON task
sources (AC-3's `load_json_tasks` contract)."""
from __future__ import annotations

import json
import os
import shutil
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
    (source / "valmod.py").write_text("value = 1\n")
    env = {
        **__import__("os").environ,
        "GIT_AUTHOR_NAME": "dev", "GIT_AUTHOR_EMAIL": "dev@test.com",
        "GIT_COMMITTER_NAME": "dev", "GIT_COMMITTER_EMAIL": "dev@test.com",
    }
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "base"], check=True, env=env)
    (source / "valmod.py").write_text("value = 2\n")
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
        id="acme-hide", capability_="fix",
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


# --- AC-4: JSON task content feeds task_digest through identity_inputs (D-9) ---

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "dev", "GIT_AUTHOR_EMAIL": "dev@test.com",
    "GIT_COMMITTER_NAME": "dev", "GIT_COMMITTER_EMAIL": "dev@test.com",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=GIT_ENV,
    ).stdout.strip()


def _digest(task) -> str:
    import run as run_module
    return run_module._cell_task_digest(task)


def _mined_task(**overrides):
    fields = dict(
        id="acme-digest", capability_="fix", prompt_="Fix acme.",
        repo_name="acme-digest-repo", base_sha="a" * 40, head_sha="b" * 40,
        test_patch="diff --git a/t.py b/t.py\n", test_command_=["python3", "-m", "pytest", "t.py"],
        reference_changed_lines_=3, lint_command_=[],
    )
    json_tasks.REPOS["acme-digest-repo"] = json_tasks.RepoConfig(
        name="acme-digest-repo", url="https://example.com/acme.git", commit_sha="a" * 40, language="",
        description="mined", on_demand_clone=True,
    )
    return json_tasks.JsonMinedTask(**{**fields, **overrides})


def test_ac4_every_json_task_type_implements_identity_inputs() -> None:
    mutation = json_tasks.load_json_tasks(MUTATION_FIXTURE)["greet-missing-comma"]

    for task in (mutation, _mined_task()):
        inputs = task.identity_inputs()
        assert isinstance(inputs, dict) and inputs
        assert inputs["prompt"] == task.prompt


@pytest.mark.parametrize("change", [
    {"prompt_": "Fix acme differently."},
    {"test_command_": ["python3", "-m", "pytest", "other.py"]},
    {"test_patch": "diff --git a/t.py b/t.py\n+x\n"},
    {"base_sha": "c" * 40},
    {"head_sha": "d" * 40},
    {"lint_command_": ["ruff", "check"]},
    {"reference_changed_lines_": 9},
])
def test_ac4_mined_task_content_changes_the_task_digest(change) -> None:
    assert _digest(_mined_task()) == _digest(_mined_task())
    assert _digest(_mined_task(**change)) != _digest(_mined_task())


def test_ac4_mutation_task_content_changes_the_task_digest(tmp_path) -> None:
    from dataclasses import replace

    fixture = tmp_path / "tasks"
    shutil.copytree(MUTATION_FIXTURE.parent, fixture)
    task = json_tasks.load_json_tasks(fixture / "agent_tasks.json")["greet-missing-comma"]
    before = _digest(task)

    assert _digest(replace(task, prompt_="Fix the greeting.")) != before
    assert _digest(replace(task, test_command_=["python3", "-c", "pass"])) != before
    assert _digest(replace(task, mutations_=[])) != before
    # The fixture repo's files feed the digest, so editing one moves it.
    (fixture / "repo" / "greet.py").write_text("def greet(name):\n    return name\n")
    assert _digest(task) != before


# --- AC-7: mined clones live under $TILTH_BENCH_DATA; the workdir hides the test patch and history (D-5) ---


def _fixture_upstream(tmp_path: Path) -> tuple[Path, str, str, str]:
    """A local git repo whose head commit fixes valmod.py and adds tests/test_valmod.py."""
    source = tmp_path / "upstream"
    (source / "tests").mkdir(parents=True)
    (source / "valmod.py").write_text("value = 1\n")
    _git(source.parent, "init", "-q", str(source))
    _git(source, "add", "-A")
    _git(source, "commit", "-qm", "base")
    base = _git(source, "rev-parse", "HEAD")
    (source / "valmod.py").write_text("value = 2\n")
    (source / "tests" / "test_valmod.py").write_text("import valmod as c\n\n\ndef test_value():\n    assert c.value == 2\n")
    _git(source, "add", "-A")
    _git(source, "commit", "-qm", "fix: reference commit")
    head = _git(source, "rev-parse", "HEAD")
    test_patch = subprocess.run(
        ["git", "-C", str(source), "diff", base, head, "--", "tests/test_valmod.py"],
        check=True, capture_output=True, text=True,
    ).stdout
    return source, base, head, test_patch


def test_ac7_mined_clone_lands_under_bench_data_and_workdir_hides_patch_and_history(
    monkeypatch, tmp_path,
) -> None:
    import config as config_module
    import run as run_module

    data = tmp_path / "bench-data"
    monkeypatch.setenv("TILTH_BENCH_DATA", str(data))
    source, base, head, test_patch = _fixture_upstream(tmp_path)
    mined_json = tmp_path / "mined.json"
    mined_json.write_text(json.dumps({"tasks": [{
        "id": "local-fix", "prompt": "Make value equal 2.", "repo_url": str(source), "base_sha": base,
        "head_sha": head, "test_patch": test_patch,
        "test_command": [sys.executable, "-B", "-m", "pytest", "tests/test_valmod.py", "-q", "-p", "no:cacheprovider"],
    }]}))

    task = json_tasks.load_json_tasks(mined_json)["local-fix"]
    repo_cfg = json_tasks.REPOS[task.repo]
    assert repo_cfg.path.is_relative_to(data.resolve() / "mined")
    assert not repo_cfg.path.exists()  # loading never clones

    monkeypatch.setattr(run_module, "REPOS", json_tasks.REPOS)
    clone = run_module.get_repo_path(task.repo)

    assert clone == repo_cfg.path
    assert not clone.is_relative_to(config_module.REPO_ROOT)
    assert _git(clone, "rev-parse", "HEAD") == base
    with run_module._agent_repo(clone, task.hide_git) as workspace:
        assert not workspace.is_relative_to(data.resolve())
        assert not (workspace / ".git").exists()
        assert not (workspace / "tests" / "test_valmod.py").exists()
        assert not any(path.suffix in {".patch", ".diff"} for path in workspace.rglob("*"))
        assert head not in "".join(path.read_text() for path in workspace.rglob("*") if path.is_file())
        # Grading applies the PR's test: the unfixed workspace fails it, the fixed one passes.
        assert task.check_correctness("", str(workspace))[0] is False
        (workspace / "valmod.py").write_text("value = 2\n")
        assert task.check_correctness("", str(workspace)) == (True, "Test passed")


# --- loader validation and registration ---

_GOOD_MUTATION = json.loads(MUTATION_FIXTURE.read_text())["tasks"][0]
_GOOD_MINED = {
    "id": "acme-mined", "prompt": "Fix acme.", "repo_url": "https://example.com/acme.git",
    "base_sha": "a" * 40, "head_sha": "b" * 40, "test_patch": "",
    "test_command": ["python3", "-m", "pytest", "t.py"],
}


def _write_mutation_file(tmp_path: Path, *tasks: dict) -> Path:
    shutil.copytree(MUTATION_FIXTURE.parent / "repo", tmp_path / "repo")
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps({"version": 1, "repo": "repo", "tasks": list(tasks)}))
    return path


def _write_mined_file(tmp_path: Path, *tasks: dict) -> Path:
    path = tmp_path / "mined.json"
    path.write_text(json.dumps({"version": 1, "tasks": list(tasks)}))
    return path


def _without(raw: dict, key: str) -> dict:
    return {k: v for k, v in raw.items() if k != key}


@pytest.mark.parametrize("raw, field_name", [
    (_without(_GOOD_MUTATION, "test_command"), "test_command"),
    ({**_GOOD_MUTATION, "test_command": []}, "test_command"),
    ({**_GOOD_MUTATION, "test_command": "pytest"}, "test_command"),
    ({**_GOOD_MUTATION, "test_command": ["pytest", 3]}, "test_command"),
    (_without(_GOOD_MUTATION, "mutations"), "mutations"),
    ({**_GOOD_MUTATION, "mutations": []}, "mutations"),
    ({**_GOOD_MUTATION, "mutations": ["greet.py"]}, "mutations[0]"),
    ({**_GOOD_MUTATION, "mutations": [{"file_path": "greet.py"}]}, "mutations[0]"),
    (_without(_GOOD_MUTATION, "prompt"), "prompt"),
    (_without(_GOOD_MUTATION, "id"), "id"),
    ("not-an-object", "<entry>"),
])
def test_mutation_task_without_a_grader_or_mutation_is_refused(tmp_path, raw, field_name) -> None:
    path = _write_mutation_file(tmp_path, raw)

    with pytest.raises(ValueError) as refused:
        json_tasks.load_json_tasks(path)

    message = str(refused.value)
    assert str(path) in message and "entry 0" in message and repr(field_name) in message


def test_mutation_task_with_no_test_command_cannot_grade_anything_correct(tmp_path) -> None:
    # Before the fix such a task loaded and Task.check_correctness fell through to
    # (True, 'All checks passed') for any output.
    path = _write_mutation_file(tmp_path, _without(_GOOD_MUTATION, "test_command"))

    with pytest.raises(ValueError, match="test_command"):
        json_tasks.load_json_tasks(path)


@pytest.mark.parametrize("raw, field_name", [
    (_without(_GOOD_MINED, "test_command"), "test_command"),
    ({**_GOOD_MINED, "test_command": []}, "test_command"),
    (_without(_GOOD_MINED, "repo_url"), "repo_url"),
    (_without(_GOOD_MINED, "base_sha"), "base_sha"),
    ({**_GOOD_MINED, "reference_changed_lines": "3"}, "reference_changed_lines"),
    ({**_GOOD_MINED, "lint_command": "ruff"}, "lint_command"),
])
def test_mined_task_with_a_bad_field_is_refused(tmp_path, raw, field_name) -> None:
    path = _write_mined_file(tmp_path, _GOOD_MINED, raw)

    with pytest.raises(ValueError) as refused:
        json_tasks.load_json_tasks(path)

    message = str(refused.value)
    assert str(path) in message and "entry 1" in message and repr(field_name) in message


def test_a_valid_mined_task_still_loads(tmp_path) -> None:
    tasks = json_tasks.load_json_tasks(_write_mined_file(tmp_path, _GOOD_MINED))

    assert tasks["acme-mined"].test_command == ["python3", "-m", "pytest", "t.py"]


def test_a_duplicate_id_inside_one_file_is_refused(tmp_path) -> None:
    path = _write_mined_file(tmp_path, _GOOD_MINED, _GOOD_MINED)

    with pytest.raises(ValueError, match="duplicates an earlier entry"):
        json_tasks.load_json_tasks(path)


@pytest.mark.parametrize("body", ['[]', '{"tasks": "x"}', '{"repo": 3, "tasks": []}'])
def test_a_malformed_top_level_is_a_value_error(tmp_path, body) -> None:
    path = tmp_path / "bad.json"
    path.write_text(body)

    with pytest.raises(ValueError, match="bad.json"):
        json_tasks.load_json_tasks(path)


def test_register_json_tasks_adds_to_the_registry(tmp_path) -> None:
    registry: dict = {"existing": object()}

    loaded = json_tasks.register_json_tasks(_write_mined_file(tmp_path, _GOOD_MINED), registry)

    assert set(loaded) == {"acme-mined"}
    assert set(registry) == {"existing", "acme-mined"}


def test_register_json_tasks_refuses_an_empty_file(tmp_path) -> None:
    registry: dict = {}

    with pytest.raises(ValueError, match="holds no tasks"):
        json_tasks.register_json_tasks(_write_mined_file(tmp_path), registry)


def test_register_json_tasks_refuses_an_id_that_collides_and_leaves_registry_alone(tmp_path) -> None:
    sentinel = object()
    registry: dict = {"acme-mined": sentinel}

    with pytest.raises(ValueError, match="collides with an existing task: acme-mined"):
        json_tasks.register_json_tasks(_write_mined_file(tmp_path, _GOOD_MINED), registry)

    assert registry == {"acme-mined": sentinel}


def test_run_main_reports_a_bad_tasks_json_as_a_usage_error(capsys, tmp_path) -> None:
    import run as run_module

    path = _write_mutation_file(tmp_path, _without(_GOOD_MUTATION, "test_command"))

    with pytest.raises(SystemExit) as exit_info:
        run_module.main(["--tasks-json", str(path)])

    assert exit_info.value.code == 2
    assert "test_command" in capsys.readouterr().err


def test_run_main_refuses_a_tasks_json_id_that_collides_with_a_static_task(capsys, tmp_path) -> None:
    import run as run_module

    static_id = next(iter(run_module.TASKS))
    path = _write_mined_file(tmp_path, {**_GOOD_MINED, "id": static_id})

    with pytest.raises(SystemExit) as exit_info:
        run_module.main(["--tasks-json", str(path)])

    assert exit_info.value.code == 2
    assert "collides with an existing task" in capsys.readouterr().err
