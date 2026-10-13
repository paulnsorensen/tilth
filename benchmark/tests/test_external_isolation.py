"""Grading and environment builds keep harness credentials and row paths contained."""

import os
import signal
import subprocess
import sys
import time
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


def test_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    script = f"sleep 100 & echo $! > {pidfile}; sleep 100"

    with pytest.raises(subprocess.TimeoutExpired):
        external.proc.run(["bash", "-c", script], timeout=1)

    child = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    os.kill(child, signal.SIGKILL)
    pytest.fail("the background child outlived the timeout")


def test_grade_cannot_persist_into_the_shared_grade_venv(external_bench, tmp_path: Path) -> None:
    external_bench.seed(SWE_PY)
    task = external.swebench_ml.load(SWE_PY, external.data.SWEBENCH_ML_REVISION)
    workdir = tmp_path / "workdir"
    task.prepare(workdir)
    external.task.apply_patch(workdir, task.gold_patch)
    ran = tmp_path / "agent-code-ran"
    with (workdir / "calc" / "ops.py").open("a") as source:
        source.write(
            "\nimport pathlib, sysconfig\n"
            "pathlib.Path(sysconfig.get_path('purelib'), 'poison.pth').write_text('import sys\\n')\n"
            f"pathlib.Path({str(ran)!r}).write_text('ran')\n"
        )

    correct, reason = task.check_correctness("", str(workdir))

    assert correct, reason
    assert ran.read_text() == "ran"
    assert not list(task.grade_venv().rglob("poison.pth"))


def test_isolate_caches_copies_seeds_and_drops_credentials(tmp_path: Path) -> None:
    cargo, gomod, gocache = tmp_path / "cargo", tmp_path / "gomod", tmp_path / "gocache"
    (cargo / "registry").mkdir(parents=True)
    (cargo / "registry" / "crate.txt").write_text("crate")
    (cargo / "credentials.toml").write_text("token")
    gomod.mkdir()
    (gomod / "mod.txt").write_text("mod")
    gocache.mkdir()

    rust_env = {"CARGO_HOME": str(cargo)}
    external.proc.isolate_caches(rust_env, tmp_path / "rust-grade", "rust")
    go_env = {"GOMODCACHE": str(gomod), "GOCACHE": str(gocache), "GOPATH": str(tmp_path / "gopath")}
    external.proc.isolate_caches(go_env, tmp_path / "go-grade", "go")
    (Path(rust_env["CARGO_HOME"]) / "registry" / "crate.txt").write_text("poisoned")
    (Path(go_env["GOMODCACHE"]) / "mod.txt").write_text("poisoned")

    assert rust_env["CARGO_HOME"] != str(cargo) and go_env["GOMODCACHE"] != str(gomod)
    assert not (Path(rust_env["CARGO_HOME"]) / "credentials.toml").exists()
    assert (cargo / "registry" / "crate.txt").read_text() == "crate"
    assert (gomod / "mod.txt").read_text() == "mod"
    assert go_env["GOCACHE"] != str(gocache) and go_env["GOPATH"] != str(tmp_path / "gopath")
