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

# Bound at import so a test that fakes ``subprocess.run`` or ``Popen`` for the agent leaves these alone.
_POPEN = subprocess.Popen


def _RUN(cmd, *, input=None, capture_output=False, text=False, timeout=None, **kwargs):
    """``subprocess.run`` for the subset used here, on the import-time ``Popen``."""
    pipe = subprocess.PIPE if capture_output else None
    stdin = kwargs.pop("stdin", subprocess.PIPE if input is not None else None)
    with _POPEN(cmd, stdin=stdin, stdout=pipe, stderr=pipe, text=text, **kwargs) as proc:
        try:
            out, err = proc.communicate(input, timeout=timeout)
        except BaseException:
            proc.kill()
            raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)

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


def _git_env(git_dir: Path, work_tree: Path, index: Path) -> dict[str, str]:
    # Isolate from user/system config so only the tree's own ignore files apply.
    return {
        **os.environ, "GIT_DIR": str(git_dir), "GIT_WORK_TREE": str(work_tree),
        "GIT_INDEX_FILE": str(index), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "XDG_CONFIG_HOME": str(index.parent), "GIT_TERMINAL_PROMPT": "0", "GIT_LITERAL_PATHSPECS": "1",
    }


def _git(args: list[str], env: dict[str, str], stdin: bytes = b"") -> bytes:
    done = _RUN(
        ["git", *args], capture_output=True, timeout=GIT_TIMEOUT_S, input=stdin, env=env,
    )
    if done.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {os.fsdecode(done.stderr or b'').strip()}")
    return done.stdout


def _files(root: Path, env: dict[str, str]) -> list[str]:
    """Regular, non-symlink files git would not ignore under ``root``, minus harness paths."""
    listed = _git(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], env)
    found = []
    for raw in listed.split(b"\0"):
        if not raw:
            continue
        rel = os.fsdecode(raw)
        path = root / rel
        if set(Path(rel).parts) & set(EXCLUDED_NAMES):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        found.append(rel)
    return sorted(set(found))


def _tree(root: Path, scratch: Path, name: str) -> str:
    """Write ``root``'s filtered files into the scratch object store; return the tree id."""
    env = _git_env(scratch / "git", root, scratch / f"index-{name}")
    files = _files(root, env)
    if files:
        _git(["add", "--pathspec-from-file=-", "--pathspec-file-nul"], env, b"\0".join(map(os.fsencode, files)))
    return _git(["write-tree"], env).decode().strip()


@contextmanager
def _scratch() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="tilth-erosion-") as temp:
        scratch = Path(temp)
        _git(["init", "-q", "--bare", str(scratch / "git")], {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull})
        yield scratch


@contextmanager
def snapshot(workdir: Path) -> Iterator[Path]:
    """Copy ``workdir``'s non-ignored files (minus harness-managed paths) for the run's duration.

    Git-ignored tool output such as caches and egg-info stays out of the copy.
    """
    with tempfile.TemporaryDirectory(prefix="tilth-baseline-") as temp:
        baseline = Path(temp) / "tree"
        baseline.mkdir()
        with _scratch() as scratch:
            env = _git_env(scratch / "git", workdir, scratch / "index-snapshot")
            for rel in _files(workdir, env):
                target = baseline / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(workdir / rel, target)
        yield baseline


def workspace_diff_lines(baseline: Path, workdir: Path) -> Optional[int]:
    """Added plus removed lines between ``baseline`` and ``workdir``; None when not computable.

    Both trees are filtered by git's ignore rules and compared with one ``git diff``.
    """
    try:
        with _scratch() as scratch:
            before = _tree(baseline, scratch, "before")
            after = _tree(workdir, scratch, "after")
            env = _git_env(scratch / "git", workdir, scratch / "index-diff")
            out = _git(["diff", "--numstat", "--no-renames", "-z", before, after], env)
        total = 0
        for entry in out.decode(errors="replace").split("\0"):
            if not entry:
                continue
            added, removed, _ = entry.split("\t", 2)
            if added != "-":  # a binary change reports "-" and counts as 0 lines
                total += int(added) + int(removed)
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
