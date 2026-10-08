"""The one subprocess entry point for loading, preparing, admitting, and grading external tasks."""

import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

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


def run(
    argv: Sequence[str],
    *,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    input: str | bytes | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """Run ``argv`` to completion, capturing output; never raises on a nonzero exit."""
    return subprocess.run(
        list(argv), cwd=cwd, env=None if env is None else dict(env), timeout=timeout,
        input=input, text=text, capture_output=True, check=False,
    )
