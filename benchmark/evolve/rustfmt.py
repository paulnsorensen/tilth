"""Format a candidate's Rust tree so that `just check`'s `cargo fmt --check` cannot fail on formatting alone."""

import subprocess
from pathlib import Path

FMT_TIMEOUT_SECONDS = 120


def format_tree(root: Path) -> bool:
    """Run ``cargo fmt`` in ``root`` with the tree's own rustfmt config; False when it fails or cannot run.

    A failure (a parse error, no cargo) leaves the tree as rustfmt left it. `just check` then reports the real error,
    so callers do not act on the result.
    """
    try:
        done = subprocess.run(["cargo", "fmt"], cwd=root, capture_output=True, text=True, timeout=FMT_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0
