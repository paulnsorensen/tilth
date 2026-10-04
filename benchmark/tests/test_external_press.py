"""Adversarial contract tests for external tasks (Press, bench-external-tasks)."""

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import external
import external.contamination
import external.featurebench
import external.preflight
import external.swebench_ml
import external_support
from external_support import FB_LV1, SWE_GO, SWE_PY, git

CONTAINER_TOOLS = {"docker", "podman"}


def _named_programs(argv: list[str]) -> set[str]:
    """Program names an argv starts, including the commands inside a ``bash -c`` script."""
    words = list(argv)
    if argv[:2] == ["bash", "-c"]:
        words += shlex.split(argv[2].replace("&&", " ").replace(";", " ").replace("|", " "))
    return {Path(word).name for word in words}


def test_install_steps_never_start_a_container(external_bench, tmp_path: Path) -> None:
    row = external_bench.row(FB_LV1)
    settings = json.loads(row["repo_settings"])
    settings["pre_install"] = ["docker pull python:3.11 && podman info"]
    instance_id = "fixture__shapes.0a1b2c3d.test_area.d0c4e200.lv1"
    external_bench.seed_row({**row, "instance_id": instance_id, "repo_settings": json.dumps(settings)})
    task = external.featurebench.load(instance_id, external.FEATUREBENCH_REVISION)

    with pytest.raises(Exception):
        task.prepare(tmp_path / "workdir")
    verdict = external.preflight.admit(instance_id)

    assert verdict.admitted is False and verdict.reason == "env_build_failed"
    started = [argv for argv in external_bench.commands if _named_programs(argv) & CONTAINER_TOOLS]
    assert started == []


def test_agent_created_new_test_file_does_not_block_held_out_patch(external_bench, tmp_path: Path) -> None:
    row = external_bench.row(SWE_PY)
    extra = (
        "diff --git a/tests/test_extra.py b/tests/test_extra.py\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        "+++ b/tests/test_extra.py\n"
        "@@ -0,0 +1,5 @@\n"
        "+from calc import mul\n"
        "+\n"
        "+\n"
        "+def test_mul_zero():\n"
        "+    assert mul(0, 9) == 0\n"
    )
    instance_id = "fixture__calc-newfile"
    eval_script = row["eval_script"].replace("-rA tests/test_ops.py", "-rA tests/test_ops.py tests/test_extra.py")
    external_bench.seed_row({**row, "instance_id": instance_id, "test_patch": row["test_patch"] + extra,
                             "eval_script": eval_script,
                             "FAIL_TO_PASS": [*row["FAIL_TO_PASS"], "tests/test_extra.py::test_mul_zero"]})
    task = external.swebench_ml.load(instance_id, external.SWEBENCH_ML_REVISION)
    workdir = tmp_path / "workdir"
    task.prepare(workdir)
    subprocess.run(["git", "apply", "-"], cwd=workdir, input=task.gold_patch, text=True, check=True)
    (workdir / "tests" / "test_extra.py").write_text("def test_mul_zero():\n    pass\n")

    correct, reason = task.check_correctness("", str(workdir))

    assert correct, reason
    assert task.grade_details()["f2p_total"] == 3


def test_symlinked_test_directory_cannot_replace_held_out_tests(external_bench, tmp_path: Path) -> None:
    external_bench.seed(FB_LV1)
    task = external.featurebench.load(FB_LV1, external.FEATUREBENCH_REVISION)
    workdir = tmp_path / "workdir"
    task.prepare(workdir)
    fake = tmp_path / "fake_tests"
    fake.mkdir()
    for name in ("test_area.py", "test_misc.py"):
        (fake / name).write_text("def test_anything():\n    pass\n")
    for path in (workdir / "tests").iterdir():
        path.unlink()
    (workdir / "tests").rmdir()
    (workdir / "tests").symlink_to(fake, target_is_directory=True)

    correct, _ = task.check_correctness("", str(workdir))

    assert correct is False
    assert task.grade_details()["f2p_passed"] == 0


@pytest.mark.parametrize(("command", "contaminated"), [
    ("git clone git@github.com:fixture/calcgo.git", True),
    ("git fetch https://GitHub.com/Fixture/CalcGo refs/pull/2/head", True),
    ("GOFLAGS=-mod=mod go get example.com/calcgo/internal@v1.2.0", True),
    ("wget https://raw.githubusercontent.com/fixture/calcgo/main/calc.go", True),
    ("git clone https://github.com/fixture/calcgo-tools", False),
    ("go get example.com/calcgofork@latest", False),
    ("go mod tidy && go build ./...", False),
])
def test_go_upstream_and_module_patterns(external_bench, tmp_path: Path, command: str, contaminated: bool) -> None:
    external_bench.seed(SWE_GO)
    task = external.swebench_ml.load(SWE_GO, external.SWEBENCH_ML_REVISION)
    sidecar = tmp_path / "cell.trajectory.jsonl"
    sidecar.write_text(json.dumps({"name": "Bash", "input": {"command": command}, "output": None}) + "\n")

    assert external.contamination.scan(sidecar, task) is contaminated


@pytest.mark.parametrize(("command", "contaminated"), [
    ("pip install calc", True),
    ("python3 -m pip install --no-deps calc==0.1", True),
    ("uv pip install --index-url https://pypi.org/simple calc", True),
    ("curl -O https://files.pythonhosted.org/packages/aa/bb/calc-1.0.tar.gz", True),
    ("pip install -r requirements.txt", False),
    ("pip install -e .[dev]", False),
    ("pip install calculator pytest", False),
])
def test_python_package_patterns(external_bench, tmp_path: Path, command: str, contaminated: bool) -> None:
    external_bench.seed(SWE_PY)
    task = external.swebench_ml.load(SWE_PY, external.SWEBENCH_ML_REVISION)
    sidecar = tmp_path / "cell.trajectory.jsonl"
    sidecar.write_text(json.dumps({"name": "Bash", "input": {"command": command}, "output": None}) + "\n")

    assert external.contamination.scan(sidecar, task) is contaminated


def test_corrupt_cached_verdict_is_recomputed(external_bench, monkeypatch: pytest.MonkeyPatch) -> None:
    external_bench.seed(SWE_PY)
    task = external.swebench_ml.load(SWE_PY, external.SWEBENCH_ML_REVISION)
    fingerprint = external.preflight.env_fingerprint(task)
    path = external.preflight.verdict_path(SWE_PY, external.SWEBENCH_ML_REVISION, fingerprint)
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    calls: list[str] = []

    def round_trip(task, fingerprint):
        calls.append(fingerprint)
        return external.preflight.PreflightVerdict(
            instance_id=task.name, dataset=task.dataset, data_rev=task.data_rev, env_fingerprint=fingerprint,
            admitted=True, reason="admitted")

    monkeypatch.setattr(external.preflight, "round_trip", round_trip)

    assert external.preflight.admit(SWE_PY).admitted is True
    assert calls == [fingerprint]
    assert json.loads(path.read_text())["reason"] == "admitted"


def test_workdir_commit_does_not_hide_changes_from_grading(external_bench, tmp_path: Path) -> None:
    external_bench.seed(SWE_PY)
    task = external.swebench_ml.load(SWE_PY, external.SWEBENCH_ML_REVISION)
    workdir = tmp_path / "workdir"
    task.prepare(workdir)
    subprocess.run(["git", "apply", "-"], cwd=workdir, input=task.gold_patch, text=True, check=True)
    git("add", "-A", cwd=workdir)
    git("commit", "-q", "-m", "agent fix", cwd=workdir)
    git("tag", "prepared", "HEAD~1", cwd=workdir)

    correct, reason = task.check_correctness("", str(workdir))

    assert correct, reason
    assert external_support.REPO_LANGUAGES["fixture/calc"] == task.language
