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
EXCLUDED_NAMES = (
    ".git", ".git_hidden", ".venv", "venv", ".tox", ".nox", ".mypy_cache", ".ruff_cache",
    ".pytest_cache", "__pycache__", "node_modules",
)
# A directory holding one of these files is a virtualenv or a tool cache, whatever its name.
EXCLUDED_DIR_MARKERS = ("pyvenv.cfg", "CACHEDIR.TAG")


def _excluded_dir(top: str, name: str) -> bool:
    if name in EXCLUDED_NAMES or name.endswith(".egg-info"):
        return True
    return any(os.path.isfile(os.path.join(top, name, marker)) for marker in EXCLUDED_DIR_MARKERS)

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


def _walk_files(root: Path) -> list[str]:
    """Regular, non-symlink files found by walking ``root``, minus harness paths and ``.git`` internals.

    The walk reads the real filesystem, so nothing the agent wrote into git config or ignore
    files changes what is listed. A nested repository's working files stay in. Tool folders
    drop out by fixed name or marker file (``pyvenv.cfg``, ``CACHEDIR.TAG``), never by an
    ignore file. Both sides of the diff use this one walk.
    """
    found = []
    for top, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if not _excluded_dir(top, d)]
        for name in names:
            path = Path(top) / name
            if name in EXCLUDED_NAMES or path.is_symlink() or not path.is_file():
                continue
            found.append(path.relative_to(root).as_posix())
    return sorted(found)


def _kept_files(baseline: Path, workdir: Path, scratch: Path) -> list[str]:
    """The ``workdir`` files to compare, judged only by the ignore rules the pre-agent ``baseline`` holds.

    An agent must not hide work by writing a ``.gitignore`` or a nested repository. The
    ``.gitignore`` files of the baseline are copied to a rules tree, and every workdir path is
    checked against them with ``git check-ignore --no-index``. A ``.gitignore`` that the workdir
    shows is counted unless a directory above it is already ignored by the baseline rules.
    """
    found = _walk_files(workdir)
    with tempfile.TemporaryDirectory(prefix="tilth-rules-") as temp:
        rules = Path(temp)
        for path in baseline.rglob(".gitignore"):
            rel = path.relative_to(baseline)
            (rules / rel).parent.mkdir(parents=True, exist_ok=True)
            (rules / rel).write_bytes(path.read_bytes())
        probes = set()
        for rel in found:
            parent = Path(rel).parent
            if Path(rel).name == ".gitignore":
                if parent != Path("."):
                    (rules / parent).mkdir(parents=True, exist_ok=True)
                    probes.add(parent.as_posix())
            else:
                probes.add(rel)
        env = _git_env(scratch / "git", rules, scratch / "index-rules")
        env.pop("GIT_LITERAL_PATHSPECS")  # check-ignore rejects it; the "./" prefix defuses pathspec magic
        done = _RUN(
            ["git", "check-ignore", "--no-index", "--stdin", "-z"], capture_output=True,
            timeout=GIT_TIMEOUT_S, input=b"\0".join(os.fsencode("./" + p) for p in sorted(probes)), env=env,
        )
    if done.returncode not in (0, 1):
        raise RuntimeError(f"git check-ignore failed: {os.fsdecode(done.stderr or b'').strip()}")
    ignored = {os.fsdecode(raw).removeprefix("./") for raw in done.stdout.split(b"\0") if raw}
    kept = []
    for rel in found:
        probe = rel if Path(rel).name != ".gitignore" else Path(rel).parent.as_posix()
        if probe not in ignored:
            kept.append(rel)
    return kept


def _tree(root: Path, scratch: Path, name: str, files: list[str]) -> str:
    """Write ``root``'s ``files`` into the scratch object store; return the tree id."""
    env = _git_env(scratch / "git", root, scratch / f"index-{name}")
    if files:
        # update-index, unlike add, also takes files that sit inside a nested repository.
        _git(["update-index", "--add", "-z", "--stdin"], env, b"\0".join(map(os.fsencode, files)) + b"\0")
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

    The copy uses the same walk and ignore rules as the after side. Tool folders and
    git-ignored output stay out of it.
    """
    with tempfile.TemporaryDirectory(prefix="tilth-baseline-") as temp:
        baseline: Optional[Path] = Path(temp) / "tree"
        try:
            baseline.mkdir()
            with _scratch() as scratch:
                for rel in _kept_files(workdir, workdir, scratch):
                    target = baseline / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(workdir / rel, target)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            baseline = None  # no snapshot: the cell still runs and its diff is unmeasured
        yield baseline


def workspace_diff_lines(baseline: Path, workdir: Path) -> Optional[int]:
    """Added plus removed lines between ``baseline`` and ``workdir``; None when not computable.

    Both trees are filtered by the baseline's git ignore rules and compared with one ``git diff``.
    """
    try:
        with _scratch() as scratch:
            before = _tree(baseline, scratch, "before", _walk_files(baseline))
            after = _tree(workdir, scratch, "after", _kept_files(baseline, workdir, scratch))
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
    (None, None), a timeout is (None, "timeout"), an unrunnable command is (None, "unavailable"),
    and a nonzero exit is (False, "failed (exit N)").
    A command with a path separator resolves against the copy it runs in.
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
            if os.sep in command[0]:
                runnable = (copy / command[0]).is_file() and os.access(copy / command[0], os.X_OK)
            else:
                runnable = shutil.which(command[0], path=env.get("PATH")) is not None
            if not runnable:
                return None, "unavailable"
            done = _RUN(
                [*_offline_prefix(), *command], cwd=str(copy), env=env, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            return None, "timeout"
        except (OSError, shutil.Error):
            return None, "unavailable"
    if done.returncode == 0:
        return True, None
    return False, f"failed (exit {done.returncode})"
