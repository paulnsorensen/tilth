"""Forward ripgrep control-flow migration (bool to Flow) with trusted, external grading tests."""

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .base import GroundTruth, Task


FIXTURE = Path("/tmp/tilth_bench/repos/ripgrep")
FIXTURES = Path(__file__).parent / "rg_flow_migration_fixtures"
HELD_OUT = FIXTURES / "heldout"
# Crates the candidate may change. Cargo manifests and the lock file stay fixed.
SOURCE_CRATES = {"matcher", "regex", "pcre2", "searcher", "printer", "grep", "core", "cli"}
SOURCE_DIRS = {"src", "examples", "tests"}
# Pinned assertions: this integration test file is replaced by the trusted version.
TRUSTED_TESTS = {"crates/matcher/tests/test_matcher.rs": HELD_OUT / "test_matcher.rs"}
# Held-out integration tests, injected into a fresh copy of the fixture only.
INJECTED_TESTS = {
    "crates/searcher/tests/flow_heldout.rs": HELD_OUT / "searcher_flow.rs",
    "crates/printer/tests/flow_heldout.rs": HELD_OUT / "printer_flow.rs",
    "crates/regex/tests/flow_heldout.rs": HELD_OUT / "regex_flow.rs",
}
PACKAGES = ["grep-matcher", "grep-regex", "grep-pcre2", "grep-searcher", "grep-printer"]
JOBS = "4"
# One persistent target dir keeps third party crates compiled across grading calls.
TARGET_DIR = Path(os.environ.get("RG_FLOW_TARGET_DIR", "/tmp/tilth_bench/cargo-target-ripgrep-flow"))
# A cold Rust build on a loaded host is slow; a timeout is inconclusive, not a test failure.
GRADE_TIMEOUT_S = 900
WARM_TIMEOUT_S = 1200
# One deadline bounds all grading in one call, so a slow host cannot hold a cell for hours.
GRADE_DEADLINE_S = 3600
TIMEOUT_REASON = "Grader timeout (inconclusive, not a test failure)"


def _package_flags() -> list[str]:
    return [flag for package in PACKAGES for flag in ("-p", package)]


def _warm_command() -> list[str]:
    return ["cargo", "test", "-j" + JOBS, "--no-run", *_package_flags()]


def _grade_commands() -> list[list[str]]:
    return [
        ["cargo", "check", "-j" + JOBS, "--workspace", "--lib", "--bins", "--tests", "--examples"],
        ["cargo", "test", "-j" + JOBS, *_package_flags()],
    ]


def _environment() -> dict[str, str]:
    return {**os.environ, "CARGO_BUILD_JOBS": JOBS, "CARGO_TARGET_DIR": str(TARGET_DIR), "RUST_TEST_THREADS": JOBS}


def _run(command: list[str], cwd: Path, timeout: int = GRADE_TIMEOUT_S) -> subprocess.CompletedProcess[str]:
    if command[0] == "cargo":
        return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=_environment())
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def _bounded(command: list[str], cwd: Path, per_command: int, deadline: float) -> subprocess.CompletedProcess[str]:
    """``_run`` with the smaller of ``per_command`` and the time left before ``deadline``."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(command, 0)
    return _run(command, cwd, min(per_command, remaining))


def _refresh_sources(directory: Path) -> None:
    """Set the mtime of every file outside target/ and .git/ to now.

    Cargo judges a workspace crate fresh when no source is newer than its dep-info in the shared
    target dir. A copied tree keeps old mtimes, so without this a grade could reuse the compiled
    crates of an earlier grade. Third party crates live in the registry and stay cached.
    """
    for current, names, files in os.walk(directory):
        if Path(current) == directory:
            names[:] = [name for name in names if name not in {"target", ".git"}]
        for name in files:
            try:
                os.utime(Path(current, name), follow_symlinks=False)
            except OSError:
                pass


def _grade(directory: Path, label: str, deadline: float) -> str | None:
    """Run the graded commands; return a failure reason or None.

    A timeout says nothing about the code under test, so its reason starts with
    TIMEOUT_REASON and never reads as "tests failed".
    """
    _refresh_sources(directory)
    warm = _warm_command()
    try:
        result = _bounded(warm, directory, WARM_TIMEOUT_S, deadline)
    except subprocess.TimeoutExpired:
        return f"{TIMEOUT_REASON}: {label} build warm-up timed out or hit the grading deadline: {' '.join(warm)}"
    if result.returncode:
        return f"{label} tests failed: {' '.join(warm)}\n{(result.stderr + result.stdout)[-1200:]}"
    for command in _grade_commands():
        try:
            result = _bounded(command, directory, GRADE_TIMEOUT_S, deadline)
        except subprocess.TimeoutExpired:
            return f"{TIMEOUT_REASON}: {label} tests timed out or hit the grading deadline: {' '.join(command)}"
        if result.returncode:
            detail = (result.stderr + result.stdout)[-1200:]
            return f"{label} tests failed: {' '.join(command)}\n{detail}"
    return None


def _inject_tests(target: Path) -> None:
    """Replace the pinned integration test and add the held-out tests."""
    for relative, source in {**TRUSTED_TESTS, **INJECTED_TESTS}.items():
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _classify(candidate: Path, relative: str) -> str | None:
    """Return "source", "test", or None for a path the task does not allow."""
    path = Path(relative)
    parts = path.parts
    entry = candidate / relative
    if path.suffix != ".rs" or len(parts) < 4 or parts[0] != "crates" or ".." in parts:
        return None
    if parts[1] not in SOURCE_CRATES or parts[2] not in SOURCE_DIRS:
        return None
    if entry.is_symlink() or not entry.is_file():
        return None
    exists = (FIXTURE / relative).exists()
    if parts[2] == "tests":
        return "test" if exists or len(parts) == 4 else None
    return "source" if exists or parts[2] == "src" else None


class RgFlowMigrationTask(Task):
    capability = "fix"

    @property
    def name(self) -> str:
        return "rg_edit_flow_migration"

    @property
    def repo(self) -> str:
        return "ripgrep"

    @property
    def task_type(self) -> str:
        return "edit"

    @property
    def ground_truth(self) -> GroundTruth:
        return GroundTruth()

    @property
    def prompt(self) -> str:
        return (
            "Migrate ripgrep's iteration control flow from bool to a new enum. Add "
            "`pub enum Flow { Continue, Stop }` to the grep-matcher crate, exported from the crate "
            "root and deriving Clone, Copy, Debug, Eq, Hash, and PartialEq. `Flow::Continue` replaces "
            "`true` (keep going) and `Flow::Stop` replaces `false` (halt now). "
            "In grep-matcher, every callback of the Matcher trait iteration and replacement methods "
            "(find_iter, find_iter_at, try_find_iter, try_find_iter_at, captures_iter, "
            "captures_iter_at, try_captures_iter, try_captures_iter_at, replace, "
            "replace_with_captures, replace_with_captures_at) returns `Flow`, or `Result<Flow, E>` for "
            "the try_ variants, instead of `bool` or `Result<bool, E>`. The blanket `&M` impl follows. "
            "In grep-searcher, the Sink methods matched, context, context_break, binary_data, and "
            "begin return `Result<Flow, Self::Error>`; the default implementations continue. The "
            "`&mut S` and `Box<S>` impls, the sinks::UTF8, sinks::Lossy, and sinks::Bytes closures "
            "(now `Result<Flow, io::Error>`), and the internal searcher plumbing that forwards these "
            "values follow. Every Sink implementation in grep-printer (Standard, JSON, Summary) and "
            "every Matcher implementation and caller in grep-regex and grep-pcre2 follows too. "
            "Update the in-crate tests, examples, and doc comments so nothing still describes or "
            "returns `bool` for these signals. "
            "This is a type migration, not a behavior change. A site becomes `Flow` only when its "
            "value is a keep-going signal that reaches a callback or Sink. A plain predicate that "
            "answers a question, for example whether a line matches or whether binary detection "
            "says to quit, stays `bool`. Every `true` that meant continue must become Continue, and "
            "every `false` that meant stop must become Stop, including inverted forms such as "
            "`!x` or early returns. A search must still stop at the same point and call finish the "
            "same way. Output of the printers and the command line must not change. "
            "Do not weaken tests, and do not change Cargo manifests or the lock file. The five crates "
            "grep-matcher, grep-regex, grep-pcre2, grep-searcher, and grep-printer must pass their "
            "tests, and the whole workspace must compile including tests and examples. Leave "
            "changes uncommitted."
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
            kind = _classify(candidate, relative)
            if kind is None:
                return False, f"Unsafe changed path: {relative}"
            if kind == "source":
                source_changes.add(relative)
        if not source_changes:
            return False, "No Rust source changes"
        deadline = time.monotonic() + GRADE_DEADLINE_S
        with tempfile.TemporaryDirectory(prefix="rg-flow-grade-") as temp:
            target = Path(temp)
            shutil.copytree(FIXTURE, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git"))
            for relative in source_changes:
                shutil.copy2(candidate / relative, target / relative)
            _inject_tests(target)
            failure = _grade(target, "Trusted", deadline)
            if failure:
                return False, failure
        failure = _grade(candidate, "Candidate", deadline)
        if failure:
            return False, failure
        return True, "Trusted and candidate tests pass"
