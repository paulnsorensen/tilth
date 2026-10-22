import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
# The benchmark.* imports below resolve from the repository root.
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from benchmark.tasks import TASKS
from benchmark.tasks.express_example_start_tasks import FIXTURE, HELD_OUT_TARGET, MOCHA, _classify, _grade_commands
from benchmark.tasks.express_example_start_fixtures.preflight import main as preflight


def test_example_start_task_contract():
    task = TASKS["express_edit_example_start"]
    assert task.repo == "express"
    assert task.task_type == "edit"
    assert task.capability == "fix"
    assert task.mutations == []
    for needle in ("module.parent", "examples/start.js", "isMain", "quiet()", "start(entry, app, port)", "PORT"):
        assert needle in task.prompt
    assert "tilth" not in task.prompt.lower()


def test_grader_commands_add_the_held_out_file_only_for_trusted_grading():
    assert _grade_commands(True) == [[*MOCHA, "test/acceptance/", HELD_OUT_TARGET]]
    assert _grade_commands(False) == [[*MOCHA, "test/acceptance/"]]


def test_classify_allows_only_examples_and_new_top_level_tests(tmp_path, monkeypatch):
    from benchmark.tasks import express_example_start_tasks as module

    fixture = tmp_path / "fixture"
    candidate = tmp_path / "candidate"
    (fixture / "test").mkdir(parents=True)
    (fixture / "test" / "app.js").write_text("")
    for relative in ("examples/ejs/index.js", "examples/start.js", "test/app.js", "test/new.js", "lib/router.js", "test/sub/x.js", "examples/ejs/x.txt"):
        path = candidate / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    (candidate / "examples" / "link.js").symlink_to(candidate / "examples" / "start.js")
    monkeypatch.setattr(module, "FIXTURE", fixture)

    assert _classify("examples/ejs/index.js", candidate) == "source"
    assert _classify("examples/start.js", candidate) == "source"
    assert _classify("test/new.js", candidate) == "test"
    assert _classify("test/app.js", candidate) is None
    assert _classify("lib/router.js", candidate) is None
    assert _classify("test/sub/x.js", candidate) is None
    assert _classify("examples/ejs/x.txt", candidate) is None
    assert _classify("examples/link.js", candidate) is None


@pytest.mark.skipif(not (FIXTURE / "node_modules").is_dir(), reason="pinned Express fixture is not installed")
def test_grader_reference_and_negative_controls():
    preflight()


def _stub_candidate(tmp_path, monkeypatch):
    from benchmark.tasks import express_example_start_tasks as module

    fixture = tmp_path / "fixture"
    candidate = tmp_path / "candidate"
    for root in (fixture, candidate):
        (root / "examples").mkdir(parents=True)
    (candidate / "examples" / "start.js").write_text("")
    monkeypatch.setattr(module, "FIXTURE", fixture)
    monkeypatch.setattr(module, "_install_held_out", lambda target: None)
    return module, candidate


def test_grader_timeout_is_inconclusive_not_a_test_failure(tmp_path, monkeypatch):
    module, candidate = _stub_candidate(tmp_path, monkeypatch)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs.get("timeout")))
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0, stdout="?? examples/start.js\n", stderr="")
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    ok, reason = module.ExpressExampleStartTask().check_correctness("", str(candidate))

    assert not ok
    assert reason.startswith(module.TIMEOUT_REASON), reason
    assert "tests failed" not in reason
    mocha_calls = [call for call in calls if call[0][0] != "git"]
    assert len(mocha_calls) == 1, "grading stops at the first timeout"
    assert mocha_calls[0][0][-1] == HELD_OUT_TARGET


def test_grading_stops_at_one_total_deadline(tmp_path, monkeypatch):
    module, candidate = _stub_candidate(tmp_path, monkeypatch)
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    timeouts = []

    def fake_run(command, **kwargs):
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0, stdout="?? examples/start.js\n", stderr="")
        timeouts.append((kwargs["timeout"], module.GRADE_DEADLINE_S - clock[0]))
        clock[0] += module.GRADE_DEADLINE_S  # passes, but uses the whole budget
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    ok, reason = module.ExpressExampleStartTask().check_correctness("", str(candidate))

    assert not ok
    assert reason.startswith(module.TIMEOUT_REASON), reason
    assert "deadline" in reason
    assert all(0 < timeout <= remaining for timeout, remaining in timeouts), timeouts
