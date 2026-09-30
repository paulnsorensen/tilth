import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

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
