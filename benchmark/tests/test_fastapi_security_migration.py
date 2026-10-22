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


def _interpreter_ready() -> bool:
    try:
        module.grading_python()
    except RuntimeError:
        return False
    return True


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
    not module.FIXTURE.is_dir() or not _interpreter_ready(),
    reason="pinned FastAPI fixture or its Python dependencies are not installed",
)
def test_grader_reference_and_negative_controls():
    preflight()


def test_grader_timeout_is_inconclusive_not_a_test_failure(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate"
    (candidate / "fastapi" / "security").mkdir(parents=True)
    (candidate / "fastapi" / "security" / "base.py").write_text("")
    monkeypatch.setattr(module, "affected_modules", lambda root: [])
    monkeypatch.setattr(module, "_restore_tests", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "grading_python", lambda: "python")
    monkeypatch.setattr(module, "FIXTURE", tmp_path / "fixture")
    (tmp_path / "fixture" / "fastapi" / "security").mkdir(parents=True)

    def fake_run(command, **kwargs):
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0, stdout=" M fastapi/security/base.py\n", stderr="")
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    ok, reason = module.FastAPISecurityMigrationTask().check_correctness("", str(candidate))

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
