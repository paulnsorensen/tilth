"""Format a candidate's Rust tree so that `just check`'s `cargo fmt --check` cannot fail on formatting alone."""

import subprocess
import tomllib
from pathlib import Path

FMT_TIMEOUT_SECONDS = 120
DEFAULT_EDITION = "2021"


def _edition(root: Path) -> str:
    try:
        return str(tomllib.loads((root / "Cargo.toml").read_text())["package"]["edition"])
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
        return DEFAULT_EDITION


def format_tree(root: Path) -> bool:
    """Run ``rustfmt`` on every ``src/**/*.rs`` in ``root``; False when it fails or cannot run.

    It does not call ``cargo fmt``: the proposer export holds no ``tests/`` directory, and cargo exits 1 when
    ``Cargo.toml`` declares a target (``[[test]]``) whose file is missing. rustfmt reads the tree's own
    ``rustfmt.toml`` and takes the edition from ``Cargo.toml``.

    A failure (a parse error, no rustfmt) leaves the tree as rustfmt left it. `just check` then reports the real error,
    so callers do not act on the result.
    """
    files = sorted(str(path) for path in (root / "src").rglob("*.rs"))
    if not files:
        return True
    try:
        done = subprocess.run(["rustfmt", "--edition", _edition(root), *files], cwd=root, capture_output=True,
                              text=True, timeout=FMT_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0
