"""The one subprocess entry point for loading, preparing, admitting, and grading external tasks."""

import os
import shutil
import signal
import subprocess
import threading
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

_DRAIN_TIMEOUT_S = 5.0
# The value of GOPROXY when nothing sets it.
_GO_DEFAULT_PROXY = "https://proxy.golang.org,direct"

# Harness variables that environment builds and grades keep: locale, terminal, network, index, and
# toolchain selection. Credentials (tokens, agent sockets, SSH and GPG agents) are not in it.
_KEPT_KEYS = frozenset({
    "PATH", "USER", "LOGNAME", "SHELL", "TMPDIR", "TERM", "LANG", "LANGUAGE",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
    "UV_INDEX_URL", "UV_EXTRA_INDEX_URL", "UV_DEFAULT_INDEX", "UV_INDEX", "UV_OFFLINE",
    "UV_PYTHON_PREFERENCE", "UV_PYTHON_DOWNLOADS",
    "GOFLAGS", "GOPROXY", "GONOSUMDB", "GOPRIVATE", "GOTOOLCHAIN", "GOROOT",
    "RUSTUP_TOOLCHAIN", "CARGO_NET_OFFLINE",
})


def _toolchain_dirs(source: Mapping[str, str]) -> dict[str, str]:
    """The host's toolchain and cache directories, so a throwaway HOME still finds its tools and caches."""
    home = Path(source.get("HOME") or Path.home())
    data = Path(source.get("XDG_DATA_HOME") or home / ".local" / "share")
    cache = Path(source.get("XDG_CACHE_HOME") or home / ".cache")
    config = Path(source.get("XDG_CONFIG_HOME") or home / ".config")
    state = Path(source.get("XDG_STATE_HOME") or home / ".local" / "state")
    gopath = source.get("GOPATH") or str(home / "go")
    defaults = {
        "MISE_DATA_DIR": data / "mise", "MISE_CONFIG_DIR": config / "mise", "MISE_CACHE_DIR": cache / "mise",
        "MISE_STATE_DIR": state / "mise",
        "UV_CACHE_DIR": cache / "uv", "UV_PYTHON_INSTALL_DIR": data / "uv" / "python",
        "CARGO_HOME": home / ".cargo", "RUSTUP_HOME": home / ".rustup",
        "GOPATH": gopath, "GOMODCACHE": Path(gopath.split(os.pathsep)[0]) / "pkg" / "mod",
        "GOCACHE": cache / "go-build",
    }
    return {key: source.get(key) or str(value) for key, value in defaults.items()}


def toolchain_env(home: Path, source: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment for environment builds and grading, which run dataset steps and the agent's code.

    It keeps only allowlisted harness variables, points the toolchain and cache
    directories at the host's, and sets ``HOME`` to ``home``. So no harness
    credential or real-home configuration reaches those processes by default.
    """
    source = os.environ if source is None else source
    env = {key: value for key, value in source.items() if key in _KEPT_KEYS or key.startswith("LC_")}
    env.update(_toolchain_dirs(source))
    env["HOME"] = str(home)
    return env


def c_build_env(language: str) -> dict[str, str]:
    """The C compiler settings that a build of a ``language`` project needs, for grades and agent cells alike.

    GCC 15 compiles C23 by default. C23 refuses the K&R prototypes in bundled C crates such as onig_sys.
    """
    return {"CFLAGS": "-std=gnu17"} if language == "rust" else {}


def isolate_caches(env: dict[str, str], root: Path, language: str, project: Path) -> None:
    """Give one grade of the project at ``project`` private caches under ``root``.

    No grade copies a whole host cache: on a file system without reflinks, that is gigabytes per grade.
    The Go module cache starts empty. ``GOPROXY`` serves modules first from the host download cache
    (``$GOMODCACHE/cache/download``) as a ``file://`` proxy. Go reads that proxy and never writes to it,
    and it checks each module against ``go.sum``. So the grade extracts only the modules it uses, with no network.
    The Cargo home gets copies of the registry index, the git checkouts, and ``config.toml``,
    and only the crate archives that ``project``'s ``Cargo.lock`` pins. Cargo extracts their sources again.
    Agent code that writes into a private cache cannot reach a later grade. Credentials files stay behind.
    The Go build cache starts empty, because it holds only build output and the build refills it.
    """
    if language == "rust":
        host, cargo_home = Path(env["CARGO_HOME"]), root / "cargo"
        cargo_home.mkdir(parents=True)
        for name in ("registry/index", "git", "config.toml"):
            _seed(host / name, cargo_home / name)
        _seed_crates(host / "registry" / "cache", cargo_home / "registry" / "cache", project / "Cargo.lock")
        env["CARGO_HOME"] = str(cargo_home)
    elif language == "go":
        downloads = Path(env["GOMODCACHE"]) / "cache" / "download"
        for name in ("gomodcache", "gocache", "gopath"):
            (root / name).mkdir(parents=True, exist_ok=True)
        env.update(GOMODCACHE=str(root / "gomodcache"), GOCACHE=str(root / "gocache"), GOPATH=str(root / "gopath"))
        if downloads.is_dir():
            # A comma falls through to the next proxy only on "not found", the answer for a module the host lacks.
            env["GOPROXY"] = f"{downloads.as_uri()},{env.get('GOPROXY') or _GO_DEFAULT_PROXY}"


def _locked_crates(lockfile: Path) -> set[str] | None:
    """The ``<name>-<version>.crate`` archive names of the registry packages in ``lockfile``; None without one."""
    try:
        lock = tomllib.loads(lockfile.read_text())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    packages = lock.get("package")
    if not isinstance(packages, list):
        return None
    registries = ("registry+", "sparse+")
    return {f"{package.get('name')}-{package.get('version')}.crate" for package in packages
            if isinstance(package, dict) and str(package.get("source", "")).startswith(registries)}


def _seed_crates(source: Path, target: Path, lockfile: Path) -> None:
    """Copy the archives in each registry directory of ``source`` that ``lockfile`` pins; all of them without one."""
    wanted = _locked_crates(lockfile)
    if wanted is None:
        _seed(source, target)
        return
    if not source.is_dir():
        return
    for registry in source.iterdir():
        if registry.is_dir():
            (target / registry.name).mkdir(parents=True, exist_ok=True)
            for archive in registry.iterdir():
                if archive.name in wanted and archive.is_file():
                    shutil.copy2(archive, target / registry.name / archive.name)


def _seed(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if not (source.is_dir() or source.is_file()):
        return
    try:
        subprocess.run(["cp", "-a", "--reflink=auto", "--", str(source), str(target)],
                       check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        shutil.rmtree(target, ignore_errors=True)
        if source.is_dir():
            shutil.copytree(source, target, symlinks=True)
        else:
            shutil.copy2(source, target)


@contextmanager
def unwind_on_sigterm() -> Iterator[None]:
    """Turn SIGTERM into ``SystemExit(143)`` while the block runs, so ``with`` cleanups still run.

    By default SIGTERM ends Python at once. Then a grade leaks its scratch directory and its test processes.
    The exception unwinds the main thread: ``run`` kills the child process group and the temporary directories go.
    A no-op off the main thread or when the process already handles SIGTERM. SIGKILL cannot be caught.
    """
    if threading.current_thread() is not threading.main_thread() or signal.getsignal(signal.SIGTERM) != signal.SIG_DFL:
        yield
        return

    def exit_now(signum: int, _frame) -> None:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, exit_now)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)


def run(
    argv: Sequence[str],
    *,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    input: str | bytes | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """Run ``argv`` to completion, capturing output; never raises on a nonzero exit.

    The child leads its own process group. On a timeout or an interrupt, the whole
    group is killed, so no background child outlives the call.
    """
    with subprocess.Popen(
        list(argv), cwd=cwd, env=None if env is None else dict(env), text=text,
        stdin=None if input is None else subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
        except BaseException:
            _kill_group(process)
            _reap(process)
            raise
        return subprocess.CompletedProcess(list(argv), process.returncode, stdout, stderr)


def _reap(process: subprocess.Popen) -> None:
    """Collect a killed child. A grandchild that left the group may still hold a pipe open, so drain with a limit."""
    try:
        process.communicate(timeout=_DRAIN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        process.wait()


def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
