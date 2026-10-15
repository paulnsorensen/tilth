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


def _upstream(tmp_path) -> tuple[Path, str, str]:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream.parent, "init", "-q", str(upstream))
    (upstream / "mod.py").write_text("value = 1\n")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-qm", "first")
    first = _git(upstream, "rev-parse", "HEAD")
    (upstream / "mod.py").write_text("value = 2\n")
    _git(upstream, "commit", "-qam", "second")
    return upstream, first, _git(upstream, "rev-parse", "HEAD")


def _mined_cfg(tmp_path, upstream, sha, name):
    return json_tasks.RepoConfig(
        name=name, url=str(upstream), commit_sha=sha, language="", description="mined",
        on_demand_clone=True, path_override=tmp_path / "mined" / name,
    )


def test_repo_config_clones_on_first_access_and_shares_one_clone_per_url(tmp_path) -> None:
    upstream, first, second = _upstream(tmp_path)
    one = _mined_cfg(tmp_path, upstream, first, "up-first")
    two = _mined_cfg(tmp_path, upstream, second, "up-second")
    assert not one.path.exists()  # nothing is cloned until a path is needed

    assert one.resolved_path() == one.path
    assert two.resolved_path() == two.path

    assert _git(one.path, "rev-parse", "HEAD") == first
    assert _git(two.path, "rev-parse", "HEAD") == second
    shared = list((tmp_path / "mined" / ".shared").iterdir())
    assert len(shared) == 1  # the url was cloned once
    assert _git(one.path, "remote", "get-url", "origin") == str(shared[0])
    assert (two.path / "mod.py").read_text() == "value = 2\n"


def test_repo_config_ensure_cloned_is_a_noop_when_present_or_not_on_demand(tmp_path) -> None:
    upstream, first, _ = _upstream(tmp_path)
    cfg = _mined_cfg(tmp_path, upstream, first, "up-first")
    cfg.ensure_cloned()
    marker = cfg.path / "marker"
    marker.write_text("x")
    cfg.ensure_cloned()
    assert marker.exists()

    local = json_tasks.RepoConfig(
        name="plain", url=str(upstream), commit_sha=first, language="", description="",
        path_override=tmp_path / "plain",
    )
    local.ensure_cloned()
    assert not local.path.exists()


def test_repo_config_failed_clone_leaves_no_path_behind(tmp_path) -> None:
    upstream, _, _ = _upstream(tmp_path)
    cfg = _mined_cfg(tmp_path, upstream, "f" * 40, "up-missing")
    with pytest.raises(RuntimeError, match="f{40}"):
        cfg.ensure_cloned()
    assert not cfg.path.exists()
    assert not list(cfg.path.parent.glob("tmp*"))


def test_repo_config_fetches_a_commit_newer_than_the_shared_clone(tmp_path) -> None:
    upstream, first, _ = _upstream(tmp_path)
    _mined_cfg(tmp_path, upstream, first, "up-first").ensure_cloned()  # shared clone exists now
    (upstream / "mod.py").write_text("value = 3\n")
    _git(upstream, "commit", "-qam", "third")
    third = _git(upstream, "rev-parse", "HEAD")
    late = _mined_cfg(tmp_path, upstream, third, "up-third")

    late.ensure_cloned()

    assert _git(late.path, "rev-parse", "HEAD") == third
    assert (late.path / "mod.py").read_text() == "value = 3\n"


def test_repo_config_falls_back_to_a_full_fetch_when_fetch_by_sha_is_refused(tmp_path, monkeypatch) -> None:
    upstream, first, _ = _upstream(tmp_path)
    _mined_cfg(tmp_path, upstream, first, "up-first").ensure_cloned()
    (upstream / "mod.py").write_text("value = 3\n")
    _git(upstream, "commit", "-qam", "third")
    third = _git(upstream, "rev-parse", "HEAD")
    real_run = subprocess.run
    seen: list[list[str]] = []

    def refuse_sha_fetch(cmd, *args, **kwargs):
        seen.append(list(cmd))
        if cmd[:3] == ["git", "fetch", "origin"] and cmd[3] == third:
            return subprocess.CompletedProcess(cmd, 128, "", "refused")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", refuse_sha_fetch)
    late = _mined_cfg(tmp_path, upstream, third, "up-third")

    late.ensure_cloned()

    assert _git(late.path, "rev-parse", "HEAD") == third
    assert any(cmd[:3] == ["git", "fetch", "origin"] and cmd[3] != third for cmd in seen)


def _refuse_sha_fetches(monkeypatch):
    real_run = subprocess.run

    def run(cmd, *args, **kwargs):
        if cmd[:3] == ["git", "fetch", "origin"] and len(cmd[3]) == 40:
            return subprocess.CompletedProcess(cmd, 128, "", "fatal: server refuses sha")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)


def _pull_head_commit(upstream) -> str:
    """A commit reachable only from refs/pull/1/head, as a GitHub PR head is."""
    branch = _git(upstream, "rev-parse", "--abbrev-ref", "HEAD")
    _git(upstream, "checkout", "-q", "-b", "pr-tmp")
    (upstream / "mod.py").write_text("value = 9\n")
    _git(upstream, "commit", "-qam", "pr head")
    head = _git(upstream, "rev-parse", "HEAD")
    _git(upstream, "update-ref", "refs/pull/1/head", head)
    _git(upstream, "checkout", "-q", branch)
    _git(upstream, "branch", "-q", "-D", "pr-tmp")
    return head


def test_ensure_cloned_fetches_a_head_commit_that_only_a_pull_ref_holds(tmp_path, monkeypatch) -> None:
    upstream, first, second = _upstream(tmp_path)
    _mined_cfg(tmp_path, upstream, first, "up-first").ensure_cloned()  # shared clone exists now
    head = _pull_head_commit(upstream)
    _refuse_sha_fetches(monkeypatch)
    cfg = _mined_cfg(tmp_path, upstream, second, "up-base")

    cfg.ensure_cloned([head])

    assert _git(cfg.path, "rev-parse", "HEAD") == second
    _git(cfg.path, "checkout", "-q", head, "--")  # the mined preflight checks out head_sha
    assert (cfg.path / "mod.py").read_text() == "value = 9\n"


def test_ensure_cloned_adds_a_late_head_commit_to_an_existing_per_commit_repo(tmp_path) -> None:
    upstream, first, _ = _upstream(tmp_path)
    cfg = _mined_cfg(tmp_path, upstream, first, "up-first")
    cfg.ensure_cloned()  # per-commit repo cloned before head exists
    head = _pull_head_commit(upstream)

    cfg.ensure_cloned([head])

    _git(cfg.path, "checkout", "-q", head, "--")
    assert (cfg.path / "mod.py").read_text() == "value = 9\n"


def test_a_missing_commit_error_carries_gits_stderr_tail(tmp_path, monkeypatch) -> None:
    upstream, first, _ = _upstream(tmp_path)
    cfg = _mined_cfg(tmp_path, upstream, first, "up-first")
    cfg.ensure_cloned()
    _refuse_sha_fetches(monkeypatch)

    with pytest.raises(RuntimeError, match="server refuses sha"):
        cfg.ensure_cloned(["e" * 40])


def test_repo_config_git_calls_never_prompt_for_credentials(tmp_path, monkeypatch) -> None:
    upstream, first, _ = _upstream(tmp_path)
    real_run = subprocess.run
    envs: list[object] = []

    def spy(cmd, *args, **kwargs):
        if cmd[:1] == ["git"]:
            envs.append((kwargs.get("env") or {}).get("GIT_TERMINAL_PROMPT"))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    _mined_cfg(tmp_path, upstream, first, "up-first").ensure_cloned()

    assert envs and set(envs) == {"0"}


def test_repo_config_network_git_calls_are_bounded_by_a_timeout(tmp_path, monkeypatch) -> None:
    upstream, first, _ = _upstream(tmp_path)
    real_run = subprocess.run
    timeouts: list[object] = []

    def spy(cmd, *args, **kwargs):
        if cmd[:2] == ["git", "clone"]:
            timeouts.append(kwargs.get("timeout"))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    _mined_cfg(tmp_path, upstream, first, "up-first").ensure_cloned()

    assert timeouts and all(isinstance(value, (int, float)) and value > 0 for value in timeouts)


def test_repo_config_a_hung_clone_times_out_as_an_oserror(tmp_path, monkeypatch) -> None:
    upstream, first, _ = _upstream(tmp_path)

    def hang(cmd, *args, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", hang)
    cfg = _mined_cfg(tmp_path, upstream, first, "up-first")

    with pytest.raises(OSError, match="timed out"):
        cfg.ensure_cloned()
    assert not cfg.path.exists()


def test_repo_config_losing_the_rename_race_to_a_valid_clone_succeeds(tmp_path, monkeypatch) -> None:
    upstream, first, _ = _upstream(tmp_path)
    cfg = _mined_cfg(tmp_path, upstream, first, "up-first")
    real_rename = os.rename

    def lose_race(src, dst):
        if Path(dst) == cfg.path:
            shutil.copytree(src, dst)  # the winner's clone lands first
            raise OSError(39, "Directory not empty")
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", lose_race)
    cfg.ensure_cloned()

    assert _git(cfg.path, "rev-parse", "HEAD") == first
    assert not list(cfg.path.parent.glob("tmp*"))


def test_repo_config_a_failed_rename_without_a_valid_clone_still_fails(tmp_path, monkeypatch) -> None:
    upstream, first, _ = _upstream(tmp_path)
    cfg = _mined_cfg(tmp_path, upstream, first, "up-first")
    real_rename = os.rename

    def broken(src, dst):
        if Path(dst) == cfg.path:
            raise OSError(13, "Permission denied")
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", broken)

    with pytest.raises(OSError, match="Permission denied"):
        cfg.ensure_cloned()
    assert not list(cfg.path.parent.glob("tmp*"))


def test_loading_a_mined_json_file_clones_nothing_and_fixture_path_clones_on_demand(tmp_path, monkeypatch) -> None:
    import tasks.base as base_module
    monkeypatch.setattr(base_module, "REPOS", json_tasks.REPOS)
    upstream, first, _ = _upstream(tmp_path)
    mined_json = tmp_path / "mined.json"
    mined_json.write_text(json.dumps({"tasks": [{
        "id": "up-1", "prompt": "fix", "test_command": ["python3", "-m", "pytest"],
        "repo_url": str(upstream), "base_sha": first, "head_sha": first,
        "test_patch": "diff --git a/t.py b/t.py\n",
    }]}))
    task = json_tasks.load_json_tasks(mined_json)["up-1"]
    cfg = json_tasks.REPOS[task.repo]
    cfg.path_override = tmp_path / "mined" / "up-1"
    assert not cfg.path.exists()

    assert task.fixture_path == cfg.path
    assert (cfg.path / "mod.py").exists()

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
    "base_sha": "a" * 40, "head_sha": "b" * 40,
    "test_patch": "diff --git a/t.py b/t.py\n",
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


@pytest.mark.parametrize("raw, field_name", [
    ({**_GOOD_MINED, "base_sha": "-" + "a" * 39}, "base_sha"),
    ({**_GOOD_MINED, "base_sha": "abc123"}, "base_sha"),
    ({**_GOOD_MINED, "base_sha": "../" + "a" * 37}, "base_sha"),
    ({**_GOOD_MINED, "head_sha": "A" * 40}, "head_sha"),
    ({**_GOOD_MINED, "repo_url": "--upload-pack=touch /tmp/x"}, "repo_url"),
    ({**_GOOD_MINED, "repo_url": "ext::sh -c id"}, "repo_url"),
    ({**_GOOD_MINED, "repo_url": "relative/path"}, "repo_url"),
    ({**_GOOD_MINED, "language": "cobol"}, "language"),
])
def test_mined_task_refuses_unsafe_urls_shas_and_unknown_languages(tmp_path, raw, field_name) -> None:
    with pytest.raises(ValueError) as refused:
        json_tasks.load_json_tasks(_write_mined_file(tmp_path, raw))

    assert repr(field_name) in str(refused.value)


@pytest.mark.parametrize("url", [
    "https://github.com/acme/widgets.git", "ssh://git@github.com/acme/widgets.git",
    "git@github.com:acme/widgets.git", "file:///srv/widgets", "/srv/widgets",
])
def test_mined_task_accepts_allowlisted_repo_urls(tmp_path, url) -> None:
    assert json_tasks.load_json_tasks(_write_mined_file(tmp_path, {**_GOOD_MINED, "repo_url": url}))


def test_json_repos_default_to_python_and_honor_a_declared_language(tmp_path) -> None:
    mined = json_tasks.load_json_tasks(_write_mined_file(tmp_path, _GOOD_MINED))["acme-mined"]
    go = json_tasks.load_json_tasks(
        _write_mined_file(tmp_path, {**_GOOD_MINED, "id": "acme-go", "language": "go", "base_sha": "b" * 40})
    )["acme-go"]
    mutation = json_tasks.load_json_tasks(_write_mutation_file(tmp_path, _GOOD_MUTATION))[_GOOD_MUTATION["id"]]

    assert json_tasks.REPOS[mined.repo].language == "python"
    assert json_tasks.REPOS[go.repo].language == "go"
    assert json_tasks.REPOS[mutation.repo].language == "python"


def test_one_mined_clone_cannot_carry_two_languages(tmp_path) -> None:
    json_tasks.load_json_tasks(_write_mined_file(tmp_path, _GOOD_MINED))

    with pytest.raises(ValueError, match="declared as both 'python' and 'go'"):
        json_tasks.load_json_tasks(
            _write_mined_file(tmp_path, {**_GOOD_MINED, "id": "acme-go-same-base", "language": "go"})
        )


def test_the_two_language_error_names_the_file_and_entry_index(tmp_path) -> None:
    json_tasks.load_json_tasks(_write_mined_file(tmp_path, _GOOD_MINED))
    second = _write_mined_file(tmp_path, {**_GOOD_MINED, "id": "acme-go-same-base", "language": "go"})

    with pytest.raises(ValueError) as caught:
        json_tasks.load_json_tasks(second)

    assert str(second) in str(caught.value)
    assert "entry 0" in str(caught.value)
    assert "'language'" in str(caught.value)

def test_mutation_file_with_an_unknown_language_is_refused(tmp_path) -> None:
    path = _write_mutation_file(tmp_path, _GOOD_MUTATION)
    path.write_text(json.dumps({**json.loads(path.read_text()), "language": "cobol"}))

    with pytest.raises(ValueError, match="language"):
        json_tasks.load_json_tasks(path)


def test_a_failed_fixture_bootstrap_is_a_value_error_naming_the_fixture(tmp_path, monkeypatch) -> None:
    path = _write_mutation_file(tmp_path, _GOOD_MUTATION)
    real_run = subprocess.run

    def fail_commit(cmd, *args, **kwargs):
        if cmd[:2] == ["git", "commit"]:
            raise subprocess.CalledProcessError(1, cmd, "", "Author identity unknown")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fail_commit)

    with pytest.raises(ValueError, match=r"cannot bootstrap fixture .*repo.*Author identity unknown"):
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


def test_grading_resets_the_old_path_of_a_pure_rename_patch(tmp_path) -> None:
    old = _numbered("pure")

    def change(source: Path) -> None:
        (source / "tests" / "test_old.py").rename(source / "tests" / "test_new.py")

    task, workspace = _mined_case(tmp_path, {"tests/test_old.py": old}, change)
    assert "@@" not in task.test_patch and "rename from tests/test_old.py" in task.test_patch
    (workspace / "tests" / "test_old.py").write_text("def test_old():\n    assert False\n")

    assert task.check_correctness("", str(workspace)) == (True, "Test passed")
    assert (workspace / "tests" / "test_new.py").read_bytes() == old


def test_mined_task_refuses_a_test_command_that_does_not_invoke_pytest(tmp_path) -> None:
    mined_json = tmp_path / "mined.json"
    mined_json.write_text(json.dumps({"tasks": [{
        "id": "no-pytest", "prompt": "fix", "test_command": ["make", "test"],
        "repo_url": "https://example.com/a.git", "base_sha": "a" * 40, "head_sha": "b" * 40,
        "test_patch": "diff --git a/t.py b/t.py\n",
    }]}))

    with pytest.raises(ValueError, match="test_command.*pytest"):
        json_tasks.load_json_tasks(mined_json)


# --- mutation fixture copies: content-addressed, staged, pinned ---


def _fixture_file(root: Path, body: str, *, subdir: str = "") -> Path:
    fixture = root / subdir / "repo" if subdir else root / "repo"
    fixture.mkdir(parents=True, exist_ok=True)
    (fixture / "greet.py").write_text(body)
    tasks_json = fixture.parent / "tasks.json"
    tasks_json.write_text(json.dumps({"repo": "repo", "tasks": [{
        "id": f"t-{subdir or 'x'}", "prompt": "fix", "test_command": ["python3", "-c", "pass"],
        "mutations": [{"file_path": "greet.py", "original": "1", "mutated": "2"}],
    }]}))
    return tasks_json


def _copy_of(tasks_json: Path) -> Path:
    return json_tasks.REPOS[next(iter(json_tasks.load_json_tasks(tasks_json).values())).repo].path


def test_editing_the_fixture_produces_a_fresh_copy(tmp_path) -> None:
    tasks_json = _fixture_file(tmp_path, "x = 1\n")
    first = _copy_of(tasks_json)
    (tasks_json.parent / "repo" / "greet.py").write_text("x = 99\n")

    second = _copy_of(tasks_json)

    assert second != first
    assert (second / "greet.py").read_text() == "x = 99\n"
    assert not (tasks_json.parent / "repo" / ".git").exists()


def test_same_stem_task_files_do_not_share_a_copy(tmp_path) -> None:
    one = _fixture_file(tmp_path, "x = 1\n", subdir="a")
    two = _fixture_file(tmp_path, "x = 1\n", subdir="b")

    assert one.name == two.name
    assert _copy_of(one) != _copy_of(two)


def test_an_unchanged_fixture_reuses_its_copy_without_rebuilding(tmp_path) -> None:
    tasks_json = _fixture_file(tmp_path, "x = 1\n")
    copy = _copy_of(tasks_json)
    (copy / "marker").write_text("kept")

    assert _copy_of(tasks_json) == copy
    assert (copy / "marker").read_text() == "kept"


def test_a_failed_copy_leaves_no_staging_directory(tmp_path, monkeypatch) -> None:
    tasks_json = _fixture_file(tmp_path, "x = 1\n")
    real_run = subprocess.run

    def failing(cmd, *args, **kwargs):
        if cmd[:2] == ["git", "commit"]:
            raise subprocess.CalledProcessError(1, cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(json_tasks.subprocess, "run", failing)
    with pytest.raises(ValueError, match="cannot bootstrap fixture"):
        json_tasks.load_json_tasks(tasks_json)

    assert list((tmp_path / "repos").iterdir()) == []


def test_the_registered_commit_is_the_bootstrap_commit_not_head(tmp_path) -> None:
    from fixtures.reset import ensure_repo_clean

    tasks_json = _fixture_file(tmp_path, "x = 1\n")
    task = next(iter(json_tasks.load_json_tasks(tasks_json).values()))
    copy = json_tasks.REPOS[task.repo].path
    (copy / "greet.py").write_text("x = 2\n")
    _git(copy, "commit", "-qam", "mutation")

    task = next(iter(json_tasks.load_json_tasks(tasks_json).values()))
    cfg = json_tasks.REPOS[task.repo]

    assert cfg.commit_sha != "HEAD"
    assert cfg.commit_sha != _git(copy, "rev-parse", "HEAD")
    ensure_repo_clean(copy, cfg.commit_sha)
    assert (copy / "greet.py").read_text() == "x = 1\n"


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
        tmp_path, {"tests/test_ok.py": PASSING}, lambda source: (source / "data.txt").write_text("x\n"),
        test_command=[sys.executable, "-c", "pass"],
    )

    passed, reason = task.check_correctness("", str(workspace))

    assert passed is False and "report missing" in reason


def test_grading_times_out_with_a_reason(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(json_tasks, "GRADER_TIMEOUT_SECONDS", 1)
    task, workspace = _mined_case(
        tmp_path, {"tests/test_ok.py": PASSING}, lambda source: (source / "data.txt").write_text("x\n"),
        test_command=[sys.executable, "-c", "import time; time.sleep(30)"],
    )

    passed, reason = task.check_correctness("", str(workspace))

    assert passed is False and reason.startswith("Grader timed out after 1s:")


def test_a_mined_task_without_a_test_patch_is_refused_at_load(tmp_path) -> None:
    for patch in (None, "", "  \n"):
        entry = {**_GOOD_MINED, "id": "no-patch", "test_command": ["python3", "-m", "pytest", "t.py"]}
        entry.pop("test_patch")
        if patch is not None:
            entry["test_patch"] = patch
        mined_json = tmp_path / "mined.json"
        mined_json.write_text(json.dumps({"tasks": [entry]}))

        with pytest.raises(ValueError, match=r"mined\.json.*entry 0.*no-patch.*test_patch"):
            json_tasks.load_json_tasks(mined_json)


def test_a_noop_workspace_does_not_grade_correct_with_an_empty_test_patch(tmp_path) -> None:
    task, workspace = _mined_case(
        tmp_path, {"tests/test_ok.py": PASSING}, lambda source: (source / "data.txt").write_text("x\n"),
    )
    assert task.check_correctness("", str(workspace)) == (True, "Test passed")  # base already passes
    task.test_patch = ""

    passed, reason = task.check_correctness("", str(workspace))

    assert passed is False and "names no path" in reason
