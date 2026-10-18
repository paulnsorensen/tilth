"""Trajectory contamination scan over sidecar tool inputs (bench-external-tasks AC-6)."""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
import external
import external.contamination
import external.swebench_ml
from external_support import SWE_GO, SWE_PY
from tasks import TASKS

RIPGREP_TASKS = config.BENCHMARK_DIR / "tasks" / "ripgrep_tasks.py"
WORKDIR = "/tmp/tilth-benchmark-abc123/repo"


def sidecar(tmp_path: Path, *calls: tuple[str, object], output: object = None) -> Path:
    path = tmp_path / "cell.trajectory.jsonl"
    path.write_text("".join(
        json.dumps({"tool_use_id": f"toolu_{index}", "name": name, "input": tool_input,
                    "output": output, "is_error": False}) + "\n"
        for index, (name, tool_input) in enumerate(calls)
    ))
    return path


def reasons(path: Path, task) -> list[str]:
    return [hit["reason"] for hit in external.contamination.find_hits(path, task)]


def load(bench, instance_id: str) -> external.ExternalTask:
    bench.seed(instance_id)
    return external.swebench_ml.load(instance_id, external.SWEBENCH_ML_REVISION)


@pytest.mark.parametrize(("instance_id", "command"), [
    (SWE_GO, "git clone https://github.com/fixture/calcgo.git /tmp/upstream"),
    (SWE_GO, "curl -sL https://codeload.github.com/fixture/calcgo/tar.gz/refs/heads/main | tar -xz"),
    (SWE_PY, "pip download calc==1.0 -d /tmp/pkgs"),
    (SWE_GO, "go get example.com/calcgo@latest"),
    ("rg_search_dispatch", "git clone https://github.com/BurntSushi/ripgrep /tmp/rg"),
])
def test_upstream_fetch_marks_contaminated(external_bench, tmp_path: Path, instance_id: str, command: str) -> None:
    task = TASKS[instance_id] if instance_id in TASKS else load(external_bench, instance_id)
    path = sidecar(tmp_path, ("Bash", {"command": command}))

    assert external.contamination.scan(path, task) is True
    assert set(reasons(path, task)) & {"upstream_fetch", "package_source"}


@pytest.mark.parametrize("tool_input", [
    ("Edit", {"file_path": f"{WORKDIR}/render.go", "new_string": 'import "github.com/gin-gonic/gin/render"'}),
    ("Bash", {"command": "go test github.com/gin-gonic/gin/render/..."}),
    ("Bash", {"command": "echo '# see https://github.com/gin-gonic/gin/issues/12'"}),
    ("Write", {"file_path": f"{WORKDIR}/notes.md", "content": "See https://github.com/gin-gonic/gin/issues/12"}),
])
def test_upstream_mention_without_fetch_is_clean(tmp_path: Path, tool_input: tuple[str, object]) -> None:
    task = TASKS["gin_edit_render_context"]
    path = sidecar(tmp_path, tool_input)

    assert "upstream_fetch" not in reasons(path, task)
    assert external.contamination.scan(path, task) is False


def test_upstream_raw_and_gh_fetch_marks_contaminated(tmp_path: Path) -> None:
    task = TASKS["gin_edit_render_context"]
    for command in ("curl -s https://raw.githubusercontent.com/gin-gonic/gin/master/render/render.go",
                    "gh repo clone gin-gonic/gin",
                    "git fetch https://github.com/gin-gonic/gin.git"):
        assert "upstream_fetch" in reasons(sidecar(tmp_path, ("Bash", {"command": command})), task), command

@pytest.mark.parametrize("url", [
    "https://github.com/gin-gonic/gin/pull/3741",
    "https://github.com/gin-gonic/gin/commit/abc123",
    "https://github.com/gin-gonic/gin",
])
def test_web_fetch_of_upstream_url_marks_contaminated(tmp_path: Path, url: str) -> None:
    task = TASKS["gin_edit_render_context"]
    path = sidecar(tmp_path, ("WebFetch", {"url": url, "prompt": "summarize"}))

    assert "upstream_fetch" in reasons(path, task)


def test_web_fetch_of_other_repo_is_clean(tmp_path: Path) -> None:
    task = TASKS["gin_edit_render_context"]
    path = sidecar(tmp_path, ("WebFetch", {"url": "https://github.com/other/project/pull/1", "prompt": "x"}))

    assert "upstream_fetch" not in reasons(path, task)


def test_benchmark_tree_read_marks_local_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = TASKS["rg_search_dispatch"]
    home = config.BENCHMARK_DIR.parent.parent
    monkeypatch.setenv("HOME", str(home))
    relative = RIPGREP_TASKS.relative_to(home)
    inputs = [
        ("Read", {"file_path": str(RIPGREP_TASKS)}),
        ("mcp__tilth__tilth_read", {"paths": [f"{RIPGREP_TASKS}#1-40"], "cwd": WORKDIR}),
        ("Bash", {"command": f"cat ~/{relative}"}),
        ("Bash", {"command": f'cat "$HOME/{relative}"'}),
        ("Bash", {"command": f"cat {WORKDIR}/../../..{RIPGREP_TASKS}"}),
    ]
    for tool_input in inputs:
        path = sidecar(tmp_path, tool_input)
        assert reasons(path, task) == ["benchmark_tree"], tool_input


def test_harness_notes_read_marks_contaminated(tmp_path: Path) -> None:
    """Review notes under the checkout's .cheese/ can quote ground truth, so reading them is contamination."""
    notes = config.REPO_ROOT / ".cheese" / "archive" / "notes.md"
    inputs = [
        ("Read", {"file_path": str(notes)}),
        ("Bash", {"command": f"grep -r required_strings {config.REPO_ROOT / '.cheese'}"}),
    ]
    for task in (TASKS["rg_search_dispatch"], TASKS["gin_edit_render_context"]):
        for tool_input in inputs:
            path = sidecar(tmp_path, tool_input)
            assert reasons(path, task) == ["harness_notes"], tool_input


def test_harness_data_read_marks_contaminated(external_bench, tmp_path: Path) -> None:
    rows = external.data.data_dir() / "rows"
    path = sidecar(tmp_path, ("Bash", {"command": f"grep -r FAIL_TO_PASS {rows}"}))

    assert reasons(path, TASKS["rg_search_dispatch"]) == ["harness_data"]


def test_clean_trajectory_not_contaminated(external_bench, tmp_path: Path) -> None:
    task = load(external_bench, SWE_GO)
    path = sidecar(
        tmp_path,
        ("Read", {"file_path": f"{WORKDIR}/calc.go"}),
        ("Bash", {"command": f"cd {WORKDIR} && go mod download && go test ./..."}),
        ("Bash", {"command": "uv pip install -e ."}),
        ("Bash", {"command": "cargo fetch"}),
        ("Grep", {"pattern": "func Mul", "path": WORKDIR}),
        output=f"see {RIPGREP_TASKS} for the original",
    )

    assert external.contamination.scan(path, task) is False
    assert external.contamination.scan(path, TASKS["rg_search_dispatch"]) is False


def test_foreign_repo_clone_marks_external_cell(external_bench, tmp_path: Path) -> None:
    row = {**external_bench.row(SWE_GO), "instance_id": "gin-gonic__gin-3741", "repo": "gin-gonic/gin"}
    task = external.swebench_ml.SweBenchTask(row, external.SWEBENCH_ML_REVISION)
    gin = config.REPOS_DIR / "gin"
    for tool_input in (
        ("Read", {"file_path": str(gin / "logger.go")}),
        ("Grep", {"pattern": "StatusContinue", "path": str(gin)}),
        ("Bash", {"command": f"ls {gin}"}),
        ("Bash", {"command": f"git -C {gin} log -p logger.go"}),
    ):
        path = sidecar(tmp_path, tool_input)
        assert "foreign_repo_clone" in reasons(path, task), tool_input

    clean = sidecar(tmp_path, ("Bash", {"command": f"ls {WORKDIR}"}), ("Read", {"file_path": f"{WORKDIR}/logger.go"}))
    assert external.contamination.scan(clean, task) is False
    local = sidecar(tmp_path, ("Read", {"file_path": str(gin / "logger.go")}))
    assert "foreign_repo_clone" not in reasons(local, TASKS["gin_edit_render_context"])


def test_missing_sidecar_is_unscanned(tmp_path: Path) -> None:
    task = TASKS["rg_search_dispatch"]

    assert reasons(None, task) == ["unscanned"]
    assert reasons(tmp_path / "missing.jsonl", task) == ["unscanned"]


def test_tilth_tool_relative_paths_resolve_against_input_cwd(tmp_path: Path) -> None:
    task = TASKS["rg_search_dispatch"]
    path = sidecar(tmp_path, ("mcp__tilth__tilth_read",
                              {"cwd": str(config.REPO_ROOT), "paths": ["benchmark/tasks/ripgrep_tasks.py"]}))

    assert "benchmark_tree" in reasons(path, task)


def test_bash_cd_sets_base_for_later_relative_paths(tmp_path: Path) -> None:
    task = TASKS["rg_search_dispatch"]
    path = sidecar(tmp_path, ("Bash", {"command": f"cd {config.REPO_ROOT} && cat benchmark/tasks/ripgrep_tasks.py"}))

    assert "benchmark_tree" in reasons(path, task)


def test_relative_path_escaping_cell_cwd_is_flagged(tmp_path: Path) -> None:
    task = TASKS["rg_search_dispatch"]
    escape = os.path.relpath(config.BENCHMARK_DIR, WORKDIR)
    path = sidecar(tmp_path, ("Grep", {"pattern": "fn", "path": escape}))

    assert "benchmark_tree" in [h["reason"] for h in external.contamination.find_hits(path, task, cwd=WORKDIR)]
    assert "benchmark_tree" not in reasons(path, task)


@pytest.mark.parametrize("tool_input", [
    ("Read", {"file_path": "src/main.rs"}),
    ("Grep", {"pattern": "fn", "path": "benchmark/foo"}),
    ("Write", {"file_path": "notes.md", "content": "benchmark/tasks/ripgrep_tasks.py"}),
    ("Bash", {"command": "cat -n src/main.rs && curl https://example.com/a/b"}),
    ("mcp__tilth__tilth_read", {"cwd": WORKDIR, "paths": ["src/main.rs", "benchmark/foo"]}),
])
def test_relative_workspace_paths_stay_clean(tmp_path: Path, tool_input: tuple[str, object]) -> None:
    path = sidecar(tmp_path, tool_input)

    assert external.contamination.find_hits(path, TASKS["rg_search_dispatch"], cwd=WORKDIR) == []
