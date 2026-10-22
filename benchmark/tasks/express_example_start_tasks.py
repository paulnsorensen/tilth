"""Express example entry-point migration with trusted, external grading tests."""

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .base import GroundTruth, Task


FIXTURE = Path("/tmp/tilth_bench/repos/express")
HELD_OUT = Path(__file__).parent / "express_example_start_fixtures"
HELD_OUT_SOURCE = "examples_start_heldout.js"
HELD_OUT_TARGET = "test/examples.start.heldout.js"
START_FILE = "examples/start.js"
MOCHA = ["node_modules/.bin/mocha", "--require", "test/support/env", "--check-leaks", "--exit"]
# A timeout is inconclusive, not a test failure.
GRADE_TIMEOUT_S = 600
GRADE_DEADLINE_S = 1800
TIMEOUT_REASON = "Grader timeout (inconclusive, not a test failure)"


def _grade_commands(held_out: bool) -> list[list[str]]:
    targets = ["test/acceptance/"] + ([HELD_OUT_TARGET] if held_out else [])
    return [[*MOCHA, *targets]]


def _run(command: list[str], cwd: Path, timeout: int = GRADE_TIMEOUT_S) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def _bounded(command: list[str], cwd: Path, deadline: float) -> subprocess.CompletedProcess[str]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(command, 0)
    return _run(command, cwd, min(GRADE_TIMEOUT_S, remaining))


def _grade(directory: Path, label: str, deadline: float, *, held_out: bool) -> str | None:
    """Run the graded commands; return a failure reason or None.

    A timeout says nothing about the code under test, so its reason starts with
    TIMEOUT_REASON and never reads as "tests failed".
    """
    for command in _grade_commands(held_out):
        try:
            result = _bounded(command, directory, deadline)
        except subprocess.TimeoutExpired:
            return f"{TIMEOUT_REASON}: {label} tests timed out or hit the grading deadline: {' '.join(command)}"
        if result.returncode:
            detail = (result.stdout + result.stderr)[-1500:]
            return f"{label} tests failed: {' '.join(command)}\n{detail}"
    return None


def _install_held_out(target: Path) -> None:
    shutil.copy2(HELD_OUT / HELD_OUT_SOURCE, target / HELD_OUT_TARGET)


def _fresh_copy(target: Path) -> None:
    """Copy the pinned fixture without git history; share its installed dependencies."""
    shutil.copytree(FIXTURE, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git", "node_modules"))
    modules = FIXTURE / "node_modules"
    if modules.is_dir():
        os.symlink(modules, target / "node_modules")


def _classify(relative: str, candidate: Path) -> str | None:
    """Return "source" for an approved example change, "test" for a new test, else None."""
    path = Path(relative)
    entry = candidate / relative
    if entry.is_symlink() or not entry.is_file() or path.suffix != ".js":
        return None
    if path.parts[0] == "examples" and "node_modules" not in path.parts:
        return "source"
    if len(path.parts) == 2 and path.parts[0] == "test" and not (FIXTURE / relative).exists():
        return "test"
    return None


class ExpressExampleStartTask(Task):
    capability = "fix"

    @property
    def name(self) -> str:
        return "express_edit_example_start"

    @property
    def repo(self) -> str:
        return "express"

    @property
    def task_type(self) -> str:
        return "edit"

    @property
    def ground_truth(self) -> GroundTruth:
        return GroundTruth()

    @property
    def prompt(self) -> str:
        return (
            "The example apps under examples/ each decide whether to start a server with "
            "the deprecated `module.parent` check and a hard-coded app.listen(3000). "
            "Replace that with one shared helper. Create examples/start.js (CommonJS) "
            "exporting three functions. isMain(entry) returns true only when "
            "require.main === entry. quiet() returns true only when "
            "process.env.NODE_ENV === 'test'. start(entry, app, port) does nothing and "
            "returns undefined unless isMain(entry); otherwise it listens with app.listen "
            "and returns the server. It listens on the PORT environment variable when that "
            "is a string of decimal digits (0 is allowed), else on the port argument, else "
            "on 3000. Once listening it logs exactly 'Express started on port N' with the "
            "port the server actually bound. Then migrate every example: no file under "
            "examples/ except examples/start.js may mention module.parent, call .listen(, "
            "or read process.env.NODE_ENV in code. Each example that started itself calls "
            "start(module, app). Each example that used module.parent for any other reason "
            "(request logging, route listing, error output, a verbose flag) uses "
            "isMain(module) and keeps the same behavior: output only when the file is run "
            "directly, nothing when it is required by another module or a test. Each "
            "example that used NODE_ENV to stay quiet uses quiet() instead. Examples that "
            "listen without any guard are included. For "
            "examples/static-files, keep printing the three 'GET ...' hint lines under "
            "'try:' only when it is run directly. Do not change what any example serves, "
            "its default port, or its public exports. Do not edit existing files under "
            "test/ or any file outside examples/. The existing test/acceptance suite must "
            "still pass. Leave changes uncommitted."
        )

    def check_correctness(self, result_text: str, repo_path: str) -> tuple[bool, str]:
        candidate = Path(repo_path)
        status = _run(["git", "status", "--porcelain", "--untracked-files=all"], candidate)
        if status.returncode:
            return False, "Cannot inspect candidate changes"
        changes = [line[3:] for line in status.stdout.splitlines()]
        if not changes:
            return False, "No source changes"
        source_changes = set()
        for relative in changes:
            kind = _classify(relative, candidate)
            if kind is None:
                return False, f"Unsafe changed path: {relative}"
            if kind == "source":
                source_changes.add(relative)
        if START_FILE not in source_changes:
            return False, f"Missing {START_FILE}"
        deadline = time.monotonic() + GRADE_DEADLINE_S
        with tempfile.TemporaryDirectory(prefix="express-start-grade-") as temp:
            target = Path(temp)
            _fresh_copy(target)
            for relative in source_changes:
                (target / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(candidate / relative, target / relative)
            _install_held_out(target)
            failure = _grade(target, "Trusted", deadline, held_out=True)
            if failure:
                return False, failure
        failure = _grade(candidate, "Candidate", deadline, held_out=False)
        if failure:
            return False, failure
        return True, "Trusted and candidate tests pass"
