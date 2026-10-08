"""Grading and environment builds keep harness credentials and row paths contained."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import external.data
import external.proc
import external.swebench_ml
import external.task
from external_support import SWE_PY


def test_toolchain_env_drops_credentials_and_swaps_home(tmp_path: Path) -> None:
    source = {"PATH": "/bin", "HOME": "/home/real", "GH_TOKEN": "secret", "SSH_AUTH_SOCK": "/agent",
              "CLAUDE_CODE_OAUTH_TOKEN": "secret", "LC_ALL": "C.UTF-8", "CARGO_HOME": "/opt/cargo"}

    env = external.proc.toolchain_env(tmp_path, source)

    assert env["HOME"] == str(tmp_path)
    assert env["PATH"] == "/bin" and env["LC_ALL"] == "C.UTF-8"
    assert not {"GH_TOKEN", "SSH_AUTH_SOCK", "CLAUDE_CODE_OAUTH_TOKEN"} & env.keys()
    assert env["CARGO_HOME"] == "/opt/cargo"
    assert env["RUSTUP_HOME"] == "/home/real/.rustup"
    assert env["UV_PYTHON_INSTALL_DIR"] == "/home/real/.local/share/uv/python"


@pytest.mark.parametrize("relative", ["../escape.py", "/etc/escape.py", "tests/../../escape.py", ""])
def test_row_paths_outside_the_checkout_are_refused(tmp_path: Path, relative: str) -> None:
    with pytest.raises(external.task.PrepareError, match="unsafe path"):
        external.task.write_or_remove(tmp_path / "checkout", relative, b"x = 1\n")
    assert not (tmp_path / "escape.py").exists()


def test_restore_does_not_follow_an_agent_directory_symlink(tmp_path: Path) -> None:
    checkout, outside = tmp_path / "checkout", tmp_path / "outside"
    checkout.mkdir()
    outside.mkdir()
    (checkout / "tests").symlink_to(outside, target_is_directory=True)

    external.task.write_or_remove(checkout, "tests/test_ops.py", b"def test_ok(): pass\n")

    assert not list(outside.iterdir())
    assert not (checkout / "tests").is_symlink()
    assert (checkout / "tests" / "test_ops.py").read_bytes() == b"def test_ok(): pass\n"


def test_grading_runs_without_harness_credentials(external_bench, monkeypatch: pytest.MonkeyPatch,
                                                  tmp_path: Path) -> None:
    external_bench.seed(SWE_PY)
    task = external.swebench_ml.load(SWE_PY, external.data.SWEBENCH_ML_REVISION)
    workdir = tmp_path / "workdir"
    task.prepare(workdir)
    monkeypatch.setenv("GH_TOKEN", "secret")
    envs: list[dict] = []
    recording_run = external.proc.run

    def capture(argv, **kwargs):
        if kwargs.get("env") is not None:
            envs.append(dict(kwargs["env"]))
        return recording_run(argv, **kwargs)

    monkeypatch.setattr(external.proc, "run", capture)
    external.task.apply_patch(workdir, task.gold_patch)

    correct, reason = task.check_correctness("", str(workdir))

    assert correct, reason
    graded = [env for env in envs if "VIRTUAL_ENV" in env]
    assert graded
    assert all("GH_TOKEN" not in env and env["HOME"] != os.environ["HOME"] for env in graded)
