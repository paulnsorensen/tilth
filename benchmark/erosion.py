"""Erosion-guard row fields: how much the agent changed, and whether the result lints.

These are row fields only. They stay out of the run key, ``CELL_KEY_FIELDS`` and
``harness_digest``, so stored baselines keep matching.

The workspace diff compares the live tree to a snapshot taken after ``task.prepare``
and before the agent ran. A snapshot works for mined tasks too, whose ``.git`` is hidden.
Both measures run before grading, which restores and patches test files.
"""
from __future__ import annotations

import functools
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Sequence

# Bound at import so a test that fakes ``subprocess.run`` for the agent leaves these alone.
_RUN = subprocess.run

LINT_TIMEOUT_S = 300
GIT_TIMEOUT_S = 60

# Harness-managed or tool-generated paths that are never the agent's work.
EXCLUDED_NAMES = (".git", ".git_hidden", "__pycache__", ".pytest_cache")

# Row fields a failed, timed-out or reused-less row carries when nothing was measured.
UNMEASURED_FIELDS = {
    "workspace_diff_lines": None,
    "reference_changed_lines": None,
    "lint_ok": None,
    "lint_reason": None,
}


@contextmanager
def snapshot(workdir: Path) -> Iterator[Path]:
    """Copy ``workdir`` (minus harness-managed paths) to a temp dir for the run's duration."""
    with tempfile.TemporaryDirectory(prefix="tilth-baseline-") as temp:
        baseline = Path(temp) / "tree"
        shutil.copytree(
            workdir, baseline, symlinks=True, ignore=shutil.ignore_patterns(*EXCLUDED_NAMES),
        )
        yield baseline


def _files(root: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for current, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_NAMES]
        for name in names:
            path = Path(current) / name
            if path.is_symlink() or not path.is_file():
                continue
            found[path.relative_to(root).as_posix()] = path
    return found


def _numstat(before: Path, after: Path) -> int:
    done = _RUN(
        ["git", "diff", "--no-index", "--numstat", "--no-renames", "--", str(before), str(after)],
        capture_output=True, text=True, timeout=GIT_TIMEOUT_S, stdin=subprocess.DEVNULL,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
    )
    if done.returncode not in (0, 1):
        raise RuntimeError(f"git diff --no-index failed: {done.stderr.strip()}")
    total = 0
    for line in done.stdout.splitlines():
        added, removed, _ = line.split("\t", 2)
        if added != "-":  # a binary change reports "-" and counts as 0 lines
            total += int(added) + int(removed)
    return total


def workspace_diff_lines(baseline: Path, workdir: Path) -> Optional[int]:
    """Added plus removed lines between ``baseline`` and ``workdir``; None when not computable."""
    try:
        before, after = _files(baseline), _files(workdir)
        total = 0
        for rel in sorted(before.keys() | after.keys()):
            old, new = before.get(rel), after.get(rel)
            if old is not None and new is not None and old.read_bytes() == new.read_bytes():
                continue
            total += _numstat(old or Path(os.devnull), new or Path(os.devnull))
        return total
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        return None


@functools.cache
def _offline_prefix() -> tuple[str, ...]:
    """A network-less namespace prefix when the host allows one; empty otherwise."""
    try:
        probe = _RUN(["unshare", "-rn", "true"], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ()
    return ("unshare", "-rn") if probe.returncode == 0 else ()


def lint_status(
    command: Sequence[str], workdir: Path, *, timeout_s: float = LINT_TIMEOUT_S,
) -> tuple[Optional[bool], Optional[str]]:
    """Run ``command`` on a throwaway copy of ``workdir``: (ok, reason).

    ``ok`` is None with a reason when the lint could not give a verdict: no command is
    (None, None), a timeout is (None, "timeout"), an unrunnable command is (None, "unavailable").
    The copy keeps a lint that writes files from changing what grading sees.
    """
    if not command:
        return None, None
    with tempfile.TemporaryDirectory(prefix="tilth-lint-") as temp:
        copy = Path(temp) / "tree"
        try:
            shutil.copytree(
                workdir, copy, symlinks=True,
                ignore=shutil.ignore_patterns(".git", ".git_hidden"),
            )
            env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL")}
            env["HOME"] = temp
            if shutil.which(command[0], path=env.get("PATH")) is None:
                return None, "unavailable"
            done = _RUN(
                [*_offline_prefix(), *command], cwd=str(copy), env=env, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            return None, "timeout"
        except (OSError, shutil.Error):
            return None, "unavailable"
    return done.returncode == 0, None
