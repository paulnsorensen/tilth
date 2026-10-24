"""Grading and environment builds keep harness credentials and row paths contained."""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import external.data
import external.proc
import external.swebench_ml
import external.task
from external_support import SWE_PY, SWE_RS, write_tree


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


_LOCKFILE = """version = 3

[[package]]
name = "app"
version = "0.1.0"

[[package]]
name = "serde"
version = "1.0.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
"""


def _host_cargo_home(root: Path) -> Path:
    write_tree(root, {"registry/index/idx/config.json": "{}", "registry/cache/idx/serde-1.0.0.crate": "serde",
                      "registry/cache/idx/unused-9.9.9.crate": "unused", "registry/src/idx/serde-1.0.0/lib.rs": "src",
                      "git/db/dep/HEAD": "ref", "config.toml": "[net]\n", "credentials.toml": "token"})
    return root


def _files(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def test_isolate_caches_copies_only_the_locked_crates_and_drops_credentials(tmp_path: Path) -> None:
    cargo = _host_cargo_home(tmp_path / "cargo")
    project = tmp_path / "checkout"
    write_tree(project, {"Cargo.lock": _LOCKFILE})
    env = {"CARGO_HOME": str(cargo)}

    external.proc.isolate_caches(env, tmp_path / "grade", "rust", project)
    private = Path(env["CARGO_HOME"])
    (private / "registry" / "cache" / "idx" / "serde-1.0.0.crate").write_text("poisoned")

    assert private != cargo
    assert _files(private) == ["config.toml", "git/db/dep/HEAD", "registry/cache/idx/serde-1.0.0.crate",
                               "registry/index/idx/config.json"]
    assert (cargo / "registry" / "cache" / "idx" / "serde-1.0.0.crate").read_text() == "serde"


@pytest.mark.parametrize("lockfile", [None, "not [ toml"])
def test_isolate_caches_copies_every_crate_archive_without_a_readable_lockfile(tmp_path: Path,
                                                                              lockfile: str | None) -> None:
    cargo = _host_cargo_home(tmp_path / "cargo")
    project = tmp_path / "checkout"
    write_tree(project, {} if lockfile is None else {"Cargo.lock": lockfile})
    env = {"CARGO_HOME": str(cargo)}

    external.proc.isolate_caches(env, tmp_path / "grade", "rust", project)

    assert [name for name in _files(Path(env["CARGO_HOME"])) if name.startswith("registry/")] == [
        "registry/cache/idx/serde-1.0.0.crate", "registry/cache/idx/unused-9.9.9.crate",
        "registry/index/idx/config.json"]


@pytest.mark.parametrize(("inherited", "fallback"), [(None, "https://proxy.golang.org,direct"), ("off", "off")])
def test_isolate_caches_serves_go_modules_from_the_host_download_cache(tmp_path: Path, inherited: str | None,
                                                                       fallback: str) -> None:
    gomod, gocache = tmp_path / "gomod", tmp_path / "gocache"
    write_tree(gomod, {"cache/download/example.com/dep/@v/v1.0.0.zip": "zip", "example.com/dep@v1.0.0/dep.go": "src"})
    gocache.mkdir()
    env = {"GOMODCACHE": str(gomod), "GOCACHE": str(gocache), "GOPATH": str(tmp_path / "gopath")}
    if inherited:
        env["GOPROXY"] = inherited

    external.proc.isolate_caches(env, tmp_path / "grade", "go", tmp_path / "checkout")

    assert env["GOMODCACHE"] != str(gomod) and list(Path(env["GOMODCACHE"]).iterdir()) == []
    assert env["GOPROXY"] == f"{(gomod / 'cache' / 'download').as_uri()},{fallback}"
    assert env["GOCACHE"] != str(gocache) and env["GOPATH"] != str(tmp_path / "gopath")


def test_isolate_caches_keeps_goproxy_when_the_host_has_no_download_cache(tmp_path: Path) -> None:
    env = {"GOMODCACHE": str(tmp_path / "missing"), "GOCACHE": str(tmp_path / "gocache"), "GOPROXY": "off"}

    external.proc.isolate_caches(env, tmp_path / "grade", "go", tmp_path / "checkout")

    assert env["GOPROXY"] == "off"


def _host_go_module(downloads: Path, module: str, version: str, files: dict[str, str]) -> None:
    """Write ``module@version`` into a download cache in the layout that ``go mod download`` leaves."""
    versions = downloads / module / "@v"
    versions.mkdir(parents=True)
    go_mod = f"module {module}\n\ngo 1.21\n"
    (versions / f"{version}.mod").write_text(go_mod)
    (versions / f"{version}.info").write_text(json.dumps({"Version": version, "Time": "2026-01-01T00:00:00Z"}))
    with zipfile.ZipFile(versions / f"{version}.zip", "w") as archive:
        for name, content in {"go.mod": go_mod, **files}.items():
            archive.writestr(f"{module}@{version}/{name}", content)


@pytest.mark.skipif(shutil.which("go") is None, reason="go is not on PATH")
def test_a_go_grade_builds_offline_from_the_host_download_cache_and_leaves_it_alone(tmp_path: Path) -> None:
    host = tmp_path / "host-gomod"
    _host_go_module(host / "cache" / "download", "example.com/dep", "v1.0.0",
                    {"dep.go": "package dep\n\nconst Answer = 42\n"})
    before = _files(host)
    project = tmp_path / "checkout"
    write_tree(project, {"go.mod": "module example.com/app\n\ngo 1.21\n\nrequire example.com/dep v1.0.0\n",
                         "main.go": 'package main\n\nimport "example.com/dep"\n\nfunc main() { println(dep.Answer) }\n'})
    (tmp_path / "home").mkdir()
    base = {"PATH": os.environ["PATH"], "HOME": str(tmp_path / "home"), "GOMODCACHE": str(host), "GOPROXY": "off",
            "GOSUMDB": "off", "GOFLAGS": "-mod=mod -buildvcs=false", "GOTOOLCHAIN": "local"}
    control = {**base, "GOMODCACHE": str(tmp_path / "empty-gomod"), "GOCACHE": str(tmp_path / "control-gocache")}
    env = dict(base)
    external.proc.isolate_caches(env, tmp_path / "grade", "go", project)

    without_proxy = subprocess.run(["go", "build", "./..."], cwd=project, env=control, capture_output=True, text=True)
    built = subprocess.run(["go", "build", "./..."], cwd=project, env=env, capture_output=True, text=True)

    assert without_proxy.returncode != 0 and "GOPROXY=off" in without_proxy.stderr
    assert built.returncode == 0, built.stderr
    assert (Path(env["GOMODCACHE"]) / "example.com" / "dep@v1.0.0" / "dep.go").is_file()
    assert _files(host) == before


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

    external.proc.isolate_caches(env, tmp_path / "grade", "go", tmp_path / "checkout")

    assert list(Path(env["GOCACHE"]).iterdir()) == []
    assert (gocache / "object").read_text() == "build output"


_GRADE_UNTIL_SIGTERM = """
import sys
from pathlib import Path

benchmark, upstream_url, instance_id, workdir, pidfile = sys.argv[1:]
sys.path[:0] = [benchmark, f"{benchmark}/tests"]
import external.data, external.proc, external.swebench_ml
from external_support import REPO_LANGUAGES

external.data.REPO_LANGUAGES.update(REPO_LANGUAGES)
external.data.UPSTREAM_URL = upstream_url
task = external.swebench_ml.load(instance_id, external.data.SWEBENCH_ML_REVISION)


def test_output(checkout):
    print(checkout.parent, flush=True)
    external.proc.run(["bash", "-c", f"echo $$ > {pidfile}; exec sleep 60"])
    return "", ""


task.test_output = test_output
task.check_correctness("", workdir)
"""


def test_sigterm_during_a_grade_removes_its_scratch_and_kills_its_tests(external_bench, tmp_path: Path) -> None:
    external_bench.seed(SWE_PY)
    task = external.swebench_ml.load(SWE_PY, external.data.SWEBENCH_ML_REVISION)
    workdir, pidfile = tmp_path / "workdir", tmp_path / "tests.pid"
    task.prepare(workdir)
    benchmark = Path(__file__).resolve().parent.parent
    grader = subprocess.Popen(
        [sys.executable, "-c", _GRADE_UNTIL_SIGTERM, str(benchmark), f"file://{external_bench.upstream}/{{repo}}.git",
         SWE_PY, str(workdir), str(pidfile)],
        stdout=subprocess.PIPE, text=True)
    try:
        grade_dir = Path(grader.stdout.readline().strip())
        for _ in range(100):
            if pidfile.is_file() and pidfile.read_text().strip():
                break
            time.sleep(0.1)
        tests = int(pidfile.read_text())
        assert grade_dir.name.startswith("tilth-external-grade-") and grade_dir.is_dir()

        grader.send_signal(signal.SIGTERM)

        assert grader.wait(timeout=30) == 128 + signal.SIGTERM
    finally:
        grader.kill()
        grader.wait()
        grader.stdout.close()
    assert not grade_dir.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(tests, 0)

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


@pytest.mark.skipif(shutil.which("cc") is None, reason="needs a C compiler")
def test_rust_grade_env_compiles_k_and_r_prototypes(external_bench, tmp_path: Path) -> None:
    """GCC 15 refuses this C23-invalid call; the Rust grade env must still compile it, as onig_sys needs."""
    external_bench.seed(SWE_RS)
    task = external.swebench_ml.load(SWE_RS, external.data.SWEBENCH_ML_REVISION)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    env = task.grade_env(checkout)
    source = "int g();\nint f(void) { return g(1); }\n"

    result = subprocess.run(["bash", "-c", "cc $CFLAGS -x c -c -o /dev/null -"],
                            input=source, env=env, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_python_grade_env_keeps_the_compiler_default(external_bench, tmp_path: Path) -> None:
    external_bench.seed(SWE_PY)
    task = external.swebench_ml.load(SWE_PY, external.data.SWEBENCH_ML_REVISION)
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    assert "CFLAGS" not in task.grade_env(checkout)
