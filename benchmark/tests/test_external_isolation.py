"""Grading and environment builds keep harness credentials and row paths contained."""

import os
import shutil
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
from external_support import SWE_PY, SWE_RS


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


def _is_private_grade_venv(venv: str | None) -> bool:
    """The host may export its own VIRTUAL_ENV, so key presence does not mark a grading env."""
    if not venv:
        return False
    path = Path(venv)
    return path.name == "checkout-venv" and path.parent.name.startswith("tilth-external-grade-")


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
    graded = [env for env in envs if _is_private_grade_venv(env.get("VIRTUAL_ENV"))]
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


def test_setsid_grandchild_holding_the_pipe_cannot_block_a_timeout(tmp_path: Path) -> None:
    pidfile = tmp_path / "grandchild.pid"
    script = f"setsid sleep 30 & echo $! > {pidfile}; sleep 100"
    started = time.monotonic()

    try:
        with pytest.raises(subprocess.TimeoutExpired):
            external.proc.run(["bash", "-c", script], timeout=1)
    finally:
        if pidfile.exists():
            os.kill(int(pidfile.read_text()), signal.SIGKILL)

    assert time.monotonic() - started < 15


def test_isolate_caches_starts_the_go_build_cache_empty(tmp_path: Path) -> None:
    gomod, gocache = tmp_path / "gomod", tmp_path / "gocache"
    gomod.mkdir()
    gocache.mkdir()
    (gocache / "object").write_text("build output")
    env = {"GOMODCACHE": str(gomod), "GOCACHE": str(gocache), "GOPATH": str(tmp_path / "gopath")}

    external.proc.isolate_caches(env, tmp_path / "grade", "go")

    assert list(Path(env["GOCACHE"]).iterdir()) == []
    assert (gocache / "object").read_text() == "build output"


def test_build_env_runs_uv_in_copy_link_mode(external_bench, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    external_bench.seed(SWE_PY)
    task = external.swebench_ml.load(SWE_PY, external.data.SWEBENCH_ML_REVISION)
    envs: list[dict] = []
    recording_run = external.proc.run

    def capture(argv, **kwargs):
        if argv[0] == "uv":
            envs.append(dict(kwargs["env"]))
        return recording_run(argv, **kwargs)

    monkeypatch.setattr(external.proc, "run", capture)
    task.prepare(tmp_path / "workdir")
    task.grade_venv()

    assert envs and all(env.get("UV_LINK_MODE") == "copy" for env in envs)


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo is not on PATH")
def test_refresh_sources_makes_cargo_rebuild_an_edited_source_after_a_seed_copy(tmp_path: Path) -> None:
    crate = tmp_path / "seed-tree"
    (crate / "src").mkdir(parents=True)
    (crate / "Cargo.toml").write_text('[package]\nname = "calc"\nversion = "0.1.0"\nedition = "2021"\n')
    main = crate / "src" / "main.rs"
    main.write_text('fn main() { println!("base"); }\n')
    env = {key: value for key, value in os.environ.items() if key != "CARGO_TARGET_DIR"}
    build = ["cargo", "run", "--offline", "--quiet"]
    assert subprocess.run(build, cwd=crate, env=env, capture_output=True, text=True).stdout.strip() == "base"

    checkout = tmp_path / "checkout"
    shutil.copytree(crate, checkout, symlinks=True)
    edited = checkout / "src" / "main.rs"
    edited.write_text('fn main() { println!("gold"); }\n')
    old = main.stat().st_mtime - 100
    os.utime(edited, (old, old))
    external.swebench_ml.refresh_sources(checkout)

    result = subprocess.run(build, cwd=checkout, env=env, capture_output=True, text=True)
    assert result.stdout.strip() == "gold", result.stderr


def test_failed_cargo_seed_is_not_retried(external_bench, monkeypatch: pytest.MonkeyPatch) -> None:
    external_bench.seed(SWE_RS)
    task = external.swebench_ml.load(SWE_RS, external.data.SWEBENCH_ML_REVISION)
    calls: list[list[str]] = []
    real_run = external.proc.run

    def failing(argv, **kwargs):
        if argv[0] != "cargo":
            return real_run(argv, **kwargs)
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 101, "", "build failed")

    monkeypatch.setattr(external.proc, "run", failing)

    assert task.cargo_seed() is None
    assert task.cargo_seed() is None
    assert calls == [["cargo", "test", "--no-run"]]
