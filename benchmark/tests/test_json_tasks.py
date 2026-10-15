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
    import run as run_module

    source = tmp_path / "source"
    source.mkdir()
    (source / "valmod.py").write_text("value = 1\n")
    _git(source.parent, "init", "-q", str(source))
    _git(source, "add", "-A")
    _git(source, "commit", "-qm", "base")
    (source / "valmod.py").write_text("value = 2\n")
    _git(source, "add", "-A")
    _git(source, "commit", "-qm", "fix: reference commit")
    head_sha = _git(source, "rev-parse", "HEAD")

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
            env={**GIT_ENV, "GIT_CEILING_DIRECTORIES": str(workspace.parent)},
        )
        assert log.returncode != 0 and "not a git repository" in log.stderr
        assert head_sha not in "".join(p.read_text() for p in workspace.rglob("*") if p.is_file())


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


# --- mined-task grading: held-out restore, JUnit verdict, timeout ---

PYTEST = [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]
PASSING = b"def test_ok():\n    assert True\n"


def _mined_case(tmp_path, base_files, change, test_command=None):
    """Build a source repo (base commit, then `change(source)` as head), a base-tree workspace and the task."""
    import io
    import tarfile

    source = tmp_path / "w" / "source"
    source.mkdir(parents=True)
    for rel, data in base_files.items():
        (source / rel).parent.mkdir(parents=True, exist_ok=True)
        (source / rel).write_bytes(data)
    _git(source.parent, "init", "-q", str(source))
    _git(source, "add", "-A")
    _git(source, "commit", "-qm", "base")
    base = _git(source, "rev-parse", "HEAD")
    change(source)
    _git(source, "add", "-A")
    _git(source, "commit", "-qm", "head", "--allow-empty")
    head = _git(source, "rev-parse", "HEAD")
    test_patch = subprocess.run(
        ["git", "-C", str(source), "diff", "-M", base, head], check=True, capture_output=True,
    ).stdout.decode()
    workspace = tmp_path / "w" / "ws"
    workspace.mkdir()
    archive = subprocess.run(["git", "-C", str(source), "archive", base], check=True, capture_output=True).stdout
    tarfile.open(fileobj=io.BytesIO(archive)).extractall(workspace, filter="data")
    json_tasks.REPOS["mined-grade"] = json_tasks.RepoConfig(
        name="mined-grade", url=str(source), commit_sha=base, language="", description="", path_override=source,
    )
    task = json_tasks.JsonMinedTask(
        id="grade", capability_="fix", prompt_="p", repo_name="mined-grade", base_sha=base, head_sha=head,
        test_patch=test_patch, test_command_=test_command or PYTEST,
        reference_changed_lines_=0,
    )
    return task, workspace


def _numbered(name: str, extra: str = "") -> bytes:
    body = "".join(f"# filler {n}\n" for n in range(8))
    return f"{body}def test_{name}():\n    assert True{extra}\n".encode()


def test_grading_restores_a_renamed_test_file_at_both_paths(tmp_path) -> None:
    old = _numbered("v")

    def change(source: Path) -> None:
        (source / "tests" / "test_old.py").rename(source / "tests" / "test_new.py")
        (source / "tests" / "test_new.py").write_bytes(old.replace(b"assert True", b"assert 1 == 1"))

    task, workspace = _mined_case(tmp_path, {"tests/test_old.py": old}, change)
    (workspace / "tests" / "test_old.py").write_text("def test_old():\n    assert False\n")

    assert task.check_correctness("", str(workspace)) == (True, "Test passed")
    assert not (workspace / "tests" / "test_old.py").exists()
    assert (workspace / "tests" / "test_new.py").exists()


def test_grading_restores_a_test_file_the_patch_deletes(tmp_path) -> None:
    gone = _numbered("gone")
    change = lambda source: (source / "tests" / "test_gone.py").unlink()  # noqa: E731
    task, workspace = _mined_case(
        tmp_path, {"tests/test_gone.py": gone, "tests/test_ok.py": PASSING}, change,
    )
    (workspace / "tests" / "test_gone.py").write_text("tampered\n")

    assert task.check_correctness("", str(workspace)) == (True, "Test passed")
    assert not (workspace / "tests" / "test_gone.py").exists()


def test_grading_round_trips_crlf_test_files(tmp_path) -> None:
    base = _numbered("crlf").replace(b"\n", b"\r\n")
    head = base.replace(b"assert True", b"assert 2 == 2")
    task, workspace = _mined_case(
        tmp_path, {"tests/test_crlf.py": base},
        lambda source: (source / "tests" / "test_crlf.py").write_bytes(head),
    )
    (workspace / "tests" / "test_crlf.py").write_bytes(b"tampered\n")

    assert task.check_correctness("", str(workspace)) == (True, "Test passed")
    assert (workspace / "tests" / "test_crlf.py").read_bytes() == head


def test_grading_round_trips_non_utf8_test_files(tmp_path) -> None:
    base = b"# -*- coding: latin-1 -*-\n# caf\xe9 \xff\xfe\n" + _numbered("enc")
    head = base.replace(b"assert True", b"assert 3 == 3")
    task, workspace = _mined_case(
        tmp_path, {"tests/test_enc.py": base},
        lambda source: (source / "tests" / "test_enc.py").write_bytes(head),
    )
    (workspace / "tests" / "test_enc.py").write_bytes(b"tampered\n")

    assert task.check_correctness("", str(workspace)) == (True, "Test passed")
    assert (workspace / "tests" / "test_enc.py").read_bytes() == head


def test_grading_ignores_a_content_line_that_looks_like_a_patch_header(tmp_path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me\n")

    def change(source: Path) -> None:
        (source / "data.txt").write_text("++ b/../../victim.txt\n")

    task, workspace = _mined_case(tmp_path, {"tests/test_ok.py": PASSING}, change)
    assert "+++ b/../../victim.txt" in task.test_patch

    assert task.check_correctness("", str(workspace)) == (True, "Test passed")
    assert victim.read_text() == "keep me\n"


def test_grading_refuses_a_header_path_outside_the_workspace(tmp_path) -> None:
    task, workspace = _mined_case(tmp_path, {"tests/test_ok.py": PASSING}, lambda source: None)
    victim = tmp_path / "w" / "evil"
    victim.write_text("keep me\n")
    task.test_patch = (
        "diff --git a/../evil b/../evil\n--- a/../evil\n+++ b/../evil\n@@ -1 +1 @@\n-keep me\n+x\n"
    )

    passed, reason = task.check_correctness("", str(workspace))

    assert passed is False and "unusable" in reason and "unsafe path" in reason
    assert victim.read_text() == "keep me\n"


def test_grading_fails_closed_when_the_test_patch_names_no_path(tmp_path) -> None:
    task, workspace = _mined_case(tmp_path, {"tests/test_ok.py": PASSING}, lambda source: None)
    task.test_patch = "this is not a diff\n"

    passed, reason = task.check_correctness("", str(workspace))

    assert passed is False and "names no path" in reason


def test_grading_does_not_count_an_all_skipped_report(tmp_path) -> None:
    skipped = b"import pytest\n\n\ndef test_s():\n    pytest.skip('nope')\n"
    task, workspace = _mined_case(
        tmp_path, {"valmod.py": b"value = 1\n"},
        lambda source: (source / "tests").mkdir() or (source / "tests" / "test_skip.py").write_bytes(skipped),
    )

    passed, reason = task.check_correctness("", str(workspace))

    assert passed is False and "zero executed passing tests" in reason


def test_grading_reports_failed_tests_with_an_output_tail(tmp_path) -> None:
    failing = b"def test_f():\n    assert 1 == 2, 'marker-from-the-grader'\n"
    task, workspace = _mined_case(
        tmp_path, {"valmod.py": b"value = 1\n"},
        lambda source: (source / "tests").mkdir() or (source / "tests" / "test_f.py").write_bytes(failing),
    )

    passed, reason = task.check_correctness("", str(workspace))

    assert passed is False and reason.startswith("Test failed:") and "marker-from-the-grader" in reason


def test_grading_reports_a_missing_report(tmp_path) -> None:
    task, workspace = _mined_case(
        tmp_path, {"tests/test_ok.py": PASSING}, lambda source: None,
        test_command=[sys.executable, "-c", "pass"],
    )

    passed, reason = task.check_correctness("", str(workspace))

    assert passed is False and "report missing" in reason


def test_grading_times_out_with_a_reason(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(json_tasks, "GRADER_TIMEOUT_SECONDS", 1)
    task, workspace = _mined_case(
        tmp_path, {"tests/test_ok.py": PASSING}, lambda source: None,
        test_command=[sys.executable, "-c", "import time; time.sleep(30)"],
    )

    passed, reason = task.check_correctness("", str(workspace))

    assert passed is False and reason.startswith("Grader timed out after 1s:")
