import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
# The benchmark.* imports below resolve from the repository root.
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from benchmark.tasks import TASKS
from benchmark.tasks import fastapi_security_migration_tasks as module
from benchmark.tasks.fastapi_security_migration_fixtures.preflight import main as preflight


def test_security_migration_task_contract():
    task = TASKS["fastapi_edit_security_raise_on_missing"]
    assert task.repo == "fastapi"
    assert task.task_type == "edit"
    assert task.capability == "fix"
    assert task.mutations == []
    for term in ("raise_on_missing", "auto_error", "DeprecationWarning", "TypeError", "OpenIdConnect"):
        assert term in task.prompt


def test_prompt_names_no_agent_tools_or_held_out_tests():
    prompt = TASKS["fastapi_edit_security_raise_on_missing"].prompt.lower()
    for forbidden in ("tilth", "heldout", "held-out", "grep", "claude"):
        assert forbidden not in prompt


def test_held_out_file_is_not_collected_by_the_benchmark_suite():
    assert module.HELD_OUT_SOURCE.is_file()
    assert not module.HELD_OUT_SOURCE.name.startswith("test_")
    assert not module.HELD_OUT_SOURCE.name.endswith("_test.py")


@pytest.mark.skipif(not module.FIXTURE.is_dir(), reason="pinned FastAPI fixture is not installed")
def test_affected_modules_cover_every_auto_error_test():
    affected = set(module.affected_modules(module.FIXTURE))
    assert len(module.auto_error_modules()) == 15
    assert set(module.auto_error_modules()) <= affected
    assert "tests/test_security_http_bearer.py" in affected


@pytest.mark.skipif(
    not module.FIXTURE.is_dir() or shutil.which("uv") is None,
    reason="pinned FastAPI fixture or uv is not installed",
)
def test_grader_reference_and_negative_controls():
    preflight()


def _stub_candidate(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate"
    (candidate / "fastapi" / "security").mkdir(parents=True)
    (candidate / "fastapi" / "security" / "base.py").write_text("")
    monkeypatch.setattr(module, "affected_modules", lambda root: [])
    monkeypatch.setattr(module, "_restore_tests", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "FIXTURE", tmp_path / "fixture")
    (tmp_path / "fixture" / "fastapi" / "security").mkdir(parents=True)
    return candidate


def test_grading_stops_at_one_total_deadline(tmp_path, monkeypatch):
    candidate = _stub_candidate(tmp_path, monkeypatch)
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    timeouts = []

    def fake_run(command, **kwargs):
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0, stdout=" M fastapi/security/base.py\n", stderr="")
        timeouts.append(kwargs["timeout"])
        clock[0] += module.GRADE_DEADLINE_S  # passes, but uses the whole budget
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    ok, reason = module.FastAPISecurityMigrationTask().check_correctness("", str(candidate))

    assert not ok
    assert reason.startswith(module.TIMEOUT_REASON) and "deadline" in reason, reason
    assert timeouts == [module.GRADE_TIMEOUT_S], "the candidate run never starts after the deadline"


def test_grader_timeout_is_inconclusive_not_a_test_failure(tmp_path, monkeypatch):
    candidate = _stub_candidate(tmp_path, monkeypatch)

    commands = []

    def fake_run(command, **kwargs):
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0, stdout=" M fastapi/security/base.py\n", stderr="")
        commands.append(command)
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    ok, reason = module.FastAPISecurityMigrationTask().check_correctness("", str(candidate))

    assert commands and commands[0][:3] == ["uv", "run", "pytest"]
    assert not ok
    assert reason.startswith(module.TIMEOUT_REASON), reason
    assert "tests failed" not in reason


def test_grader_rejects_unapproved_and_symlinked_paths(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate"
    (candidate / "fastapi" / "security").mkdir(parents=True)
    (candidate / "fastapi" / "utils.py").write_text("")
    (candidate / "fastapi" / "security" / "base.py").write_text("")
    monkeypatch.setattr(module, "FIXTURE", tmp_path / "fixture")
    (tmp_path / "fixture").mkdir()
    monkeypatch.setattr(module, "affected_modules", lambda root: [])

    def run_with(status: str):
        monkeypatch.setattr(
            module.subprocess, "run",
            lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout=status, stderr=""),
        )
        return module.FastAPISecurityMigrationTask().check_correctness("", str(candidate))

    ok, reason = run_with(" M fastapi/utils.py\n")
    assert not ok and reason.startswith("Unsafe changed path"), reason
    (candidate / "link.py").symlink_to(candidate / "fastapi" / "security" / "base.py")
    (candidate / "fastapi" / "security" / "alias.py").symlink_to(candidate / "fastapi" / "utils.py")
    ok, reason = run_with("?? fastapi/security/alias.py\n")
    assert not ok and reason.startswith("Unsafe changed path"), reason
    ok, reason = run_with(" M tests/test_security_http_bearer.py\n")
    assert not ok, reason


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _stub_build(monkeypatch):
    """Replace the uv sync with a fake that creates the venv; return the recorded calls."""
    calls = []
    real_run = subprocess.run

    def fake_run(command, **kwargs):
        if command[0] != "uv":
            return real_run(command, **kwargs)
        calls.append((command, kwargs["env"], kwargs["cwd"]))
        bin_dir = Path(kwargs["env"]["UV_PROJECT_ENVIRONMENT"]) / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "pytest").write_text("")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    return calls


def test_prepare_builds_an_excluded_agent_venv_once(tmp_path, monkeypatch):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    _git(workdir, "init", "-q")
    calls = _stub_build(monkeypatch)
    task = module.FastAPISecurityMigrationTask()

    task.prepare(workdir)
    task.prepare(workdir)  # a chain prepares a member once per cell; the venv is reused

    assert (workdir / ".venv" / "bin").is_dir()
    assert len(calls) == 1
    command, env, cwd = calls[0]
    assert command == module.VENV_SYNC_COMMAND and cwd == workdir
    assert {"--frozen", "--extra", "all", "tests"} <= set(command)
    assert env["UV_PROJECT_ENVIRONMENT"] == str(workdir / ".venv") and env["UV_LINK_MODE"] == "copy"
    exclude = (workdir / ".git" / "info" / "exclude").read_text().splitlines()
    assert exclude.count(".venv/") == 1


def test_grader_ignores_the_agent_venv_in_a_git_worktree(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "base")
    workdir = tmp_path / "workdir"
    _git(source, "worktree", "add", "--detach", str(workdir), "HEAD")
    _stub_build(monkeypatch)

    module.FastAPISecurityMigrationTask().prepare(workdir)
    (workdir / ".venv" / "lib").mkdir()
    (workdir / ".venv" / "lib" / "site.py").write_text("")

    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=workdir, capture_output=True, text=True,
    )
    assert status.stdout == ""
    ok, reason = module.FastAPISecurityMigrationTask().check_correctness("", str(workdir))
    assert not ok and reason == "No source changes"


def test_failed_venv_build_leaves_no_half_built_venv(tmp_path, monkeypatch):
    def failing_run(command, **kwargs):
        (Path(kwargs["env"]["UV_PROJECT_ENVIRONMENT"]) / "bin").mkdir(parents=True)
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="no network")

    monkeypatch.setattr(module.subprocess, "run", failing_run)
    with pytest.raises(RuntimeError, match="no network"):
        module.build_agent_venv(tmp_path)
    assert not (tmp_path / ".venv").exists()
