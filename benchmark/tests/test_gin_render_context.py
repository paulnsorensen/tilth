import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
# The benchmark.* imports below resolve from the repository root.
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from benchmark.tasks import TASKS
from benchmark.tasks.gin_render_context_tasks import FIXTURE, SOURCE_FILES, ROOT_TEST_PATTERN, _grade_commands
from benchmark.tasks.gin_render_context_fixtures.preflight import main as preflight


def test_render_context_task_contract():
    task = TASKS["gin_edit_render_context"]
    assert task.repo == "gin"
    assert task.task_type == "edit"
    assert task.capability == "fix"
    assert task.mutations == []
    assert "context.Context" in task.prompt
    assert "nomsgpack" in task.prompt
    assert "binding" in task.prompt


def test_reference_file_scope():
    assert len(SOURCE_FILES) == 14
    assert "context.go" in SOURCE_FILES
    assert "render/render.go" in SOURCE_FILES
    assert "render/msgpack.go" in SOURCE_FILES
    assert "render/bson.go" in SOURCE_FILES


def test_grader_commands_keep_full_render_and_scoped_root():
    assert ROOT_TEST_PATTERN == "^(TestContext|TestMiddleware|TestHeldOut)"
    assert _grade_commands([]) == [
        ["go", "test", "./render"],
        ["go", "test", "-run", ROOT_TEST_PATTERN, "."],
        ["go", "test", "./binding"],
        ["go", "test", "-run", "^$", "./..."],
    ]
    assert _grade_commands(["-tags=nomsgpack"]) == [
        ["go", "test", "-tags=nomsgpack", "./render"],
        ["go", "test", "-tags=nomsgpack", "-run", ROOT_TEST_PATTERN, "."],
        ["go", "test", "-tags=nomsgpack", "./binding"],
        ["go", "test", "-tags=nomsgpack", "-run", "^$", "./..."],
    ]

@pytest.mark.skipif(not FIXTURE.is_dir(), reason="pinned Gin fixture is not installed")
def test_grader_reference_and_negative_controls():
    preflight()


def test_grader_timeout_is_inconclusive_not_a_test_failure(tmp_path, monkeypatch):
    import subprocess

    from benchmark.tasks import gin_render_context_tasks as module

    fixture = tmp_path / "fixture"
    candidate = tmp_path / "candidate"
    for root in (fixture, candidate):
        root.mkdir()
        (root / "context.go").write_text("package gin\n")
    monkeypatch.setattr(module, "FIXTURE", fixture)
    monkeypatch.setattr(module, "_restore_tests", lambda *args, **kwargs: None)

    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs.get("timeout")))
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0, stdout=" M context.go\n", stderr="")
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    ok, reason = module.GinRenderContextTask().check_correctness("", str(candidate))

    assert not ok
    assert reason.startswith("Grader timeout"), reason
    assert "tests failed" not in reason
    go_calls = [call for call in calls if call[0][0] == "go"]
    assert go_calls[0][0] == ["go", "test", "-run", "^$", "./..."], "build is warmed before graded commands"
    assert all(timeout >= 900 for _, timeout in go_calls)


def test_grading_stops_at_one_total_deadline(tmp_path, monkeypatch):
    import subprocess

    from benchmark.tasks import gin_render_context_tasks as module

    fixture = tmp_path / "fixture"
    candidate = tmp_path / "candidate"
    for root in (fixture, candidate):
        root.mkdir()
        (root / "context.go").write_text("package gin\n")
    monkeypatch.setattr(module, "FIXTURE", fixture)
    monkeypatch.setattr(module, "_restore_tests", lambda *args, **kwargs: None)
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    go_timeouts = []

    def fake_run(command, **kwargs):
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0, stdout=" M context.go\n", stderr="")
        go_timeouts.append((kwargs["timeout"], module.GRADE_DEADLINE_S - clock[0]))
        clock[0] += 800  # every go command passes, slowly
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    ok, reason = module.GinRenderContextTask().check_correctness("", str(candidate))

    assert not ok
    assert reason.startswith(module.TIMEOUT_REASON), reason
    assert "deadline" in reason
    assert all(0 < timeout <= remaining for timeout, remaining in go_timeouts), go_timeouts
    assert len(go_timeouts) < 20, "grading stopped before every command ran"
