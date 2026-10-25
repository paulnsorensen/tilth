"""External edit tasks tell the agent to build and test, and a Python agent cell runs with its own venv first."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import external
import external.featurebench
import external.swebench_ml
import run
from external_support import FB_LV1, SWE_GO, SWE_PY, SWE_RS, rows

BUILD_AND_TEST = "Before you finish, build the code and run the existing tests that cover your change."
PYTHON_HINT = "Dependencies are installed in `.venv`; run tests with `.venv/bin/python -m pytest`."
ENDINGS = {
    SWE_PY: f"{BUILD_AND_TEST} {PYTHON_HINT}",
    FB_LV1: f"{BUILD_AND_TEST} {PYTHON_HINT}",
    SWE_RS: f"{BUILD_AND_TEST} Run tests with `cargo test`.",
    SWE_GO: f"{BUILD_AND_TEST} Run tests with `go test ./<pkg>/...` for the packages you change.",
}
AMBIENT = {"PATH": "/usr/bin", "HOME": "/home/fixture"}


def load(instance_id: str) -> external.ExternalTask:
    if instance_id in rows("featurebench"):
        return external.featurebench.load(instance_id, external.FEATUREBENCH_REVISION)
    return external.swebench_ml.load(instance_id, external.SWEBENCH_ML_REVISION)


def cell_env(task, workdir: Path) -> dict[str, str]:
    return run.build_runner_env(
        "claude", ambient=AMBIENT, tilth_bin=None, language=task.language,
        agent_venv=run._agent_venv(task, workdir),
    )


@pytest.mark.parametrize("instance_id", list(ENDINGS))
def test_prompt_ends_with_the_language_instruction_and_hides_heldout_tests(external_bench, instance_id: str) -> None:
    external_bench.seed(instance_id)
    task = load(instance_id)

    assert task.prompt.endswith(ENDINGS[instance_id])
    heldout = [*task.fail_to_pass, *task.pass_to_pass]
    assert heldout
    for identifier in heldout:
        assert identifier not in task.prompt
        assert Path(identifier.split("::")[0]).name not in ENDINGS[instance_id]


def test_python_cell_env_puts_the_workdir_venv_first(external_bench, tmp_path: Path) -> None:
    external_bench.seed(SWE_PY)
    task = load(SWE_PY)
    workdir = tmp_path / "workdir"
    task.prepare(workdir)

    env = cell_env(task, workdir)

    assert env["VIRTUAL_ENV"] == str(workdir / ".venv")
    assert env["PATH"].split(os.pathsep)[0] == str(workdir / ".venv" / "bin")
    assert (workdir / ".venv" / "bin" / "python").exists()


def fake_runner(directory: Path, label: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "claude"
    script.write_text(f"#!/bin/sh\necho {label}\n")
    script.chmod(0o755)


def test_task_venv_console_script_does_not_replace_the_runner(tmp_path: Path) -> None:
    host = tmp_path / "host"
    venv = tmp_path / "workdir" / ".venv"
    fake_runner(host, "host")
    fake_runner(venv / "bin", "venv")
    env = {"PATH": os.pathsep.join([str(venv / "bin"), str(host), "/usr/bin", "/bin"])}

    argv = run._host_runner_argv(["claude", "-p"], env, venv)

    assert argv == [str(host / "claude"), "-p"]
    assert subprocess.run(argv[:1], env=env, capture_output=True, text=True, check=True).stdout == "host\n"
    assert run._host_runner_argv(["claude", "-p"], env, None) == ["claude", "-p"]


def test_runner_only_in_the_task_venv_fails_closed(tmp_path: Path) -> None:
    venv = tmp_path / "workdir" / ".venv"
    fake_runner(venv / "bin", "venv")
    env = {"PATH": os.pathsep.join([str(venv / "bin"), str(tmp_path / "empty")])}

    with pytest.raises(FileNotFoundError, match="claude"):
        run._host_runner_argv(["claude", "-p"], env, venv)


def test_grading_venv_stays_separate_from_the_agent_tree(external_bench, tmp_path: Path) -> None:
    external_bench.seed(SWE_PY)
    task = load(SWE_PY)
    workdir = tmp_path / "workdir"
    task.prepare(workdir)
    agent_env = cell_env(task, workdir)

    grade_venv = task.grade_venv()
    checkout = tmp_path / "checkout"
    task.export_prepared(checkout)
    grading = task.grading_env(checkout)

    assert workdir not in grade_venv.parents and grade_venv != workdir / ".venv"
    assert agent_env["VIRTUAL_ENV"] != str(grade_venv)
    assert grading["VIRTUAL_ENV"] == str(checkout.with_name(f"{checkout.name}-venv"))
    assert str(workdir / ".venv") not in grading["PATH"]


def test_other_cells_keep_their_env(external_bench, tmp_path: Path) -> None:
    external_bench.seed(SWE_GO)
    task = load(SWE_GO)
    workdir = tmp_path / "workdir"
    (workdir / ".venv" / "bin").mkdir(parents=True)

    assert run._agent_venv(task, workdir) is None
    assert cell_env(task, workdir) == run.build_runner_env(
        "claude", ambient=AMBIENT, tilth_bin=None, language=task.language)

    local = next(task for task in run.TASKS.values() if not isinstance(task, external.ExternalTask))
    assert run._agent_venv(local, workdir) is None
    env = run.build_runner_env("claude", ambient=AMBIENT, tilth_bin=None, language=local.language)
    assert "VIRTUAL_ENV" not in env and ".venv" not in env["PATH"]
