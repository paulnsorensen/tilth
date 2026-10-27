"""FastAPI security-scheme keyword migration with trusted, external grading tests."""

import glob
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .base import GroundTruth, Task


FIXTURE = Path("/tmp/tilth_bench/repos/fastapi")
HELD_OUT = Path(__file__).parent / "fastapi_security_migration_fixtures"
HELD_OUT_SOURCE = HELD_OUT / "heldout_security_raise_on_missing.py"
HELD_OUT_TARGET = "tests/test_security_raise_on_missing_heldout.py"
SECURITY_SOURCE_DIR = "fastapi/security"
EXTRA_MODULES = (
    "tests/test_dependency_security_overrides.py",
    "tests/test_webhooks_security.py",
    "tests/test_top_level_security_scheme_in_openapi.py",
)
# pytest runs in one process, which is within the four-worker cap for graders.
GRADE_TIMEOUT_S = 900
# One deadline covers the trusted and the candidate run.
GRADE_DEADLINE_S = 1800
TIMEOUT_REASON = "Grader timeout (inconclusive, not a test failure)"
PYTEST_COMMAND = ["uv", "run", "pytest", "-q", "-p", "no:cacheprovider"]
# The agent venv: locked versions of the tests group plus the extras the security tests need
# (python-multipart, argon2 through pwdlib). uv's wheel cache keeps a rebuild to seconds.
VENV_SYNC_COMMAND = ["uv", "sync", "--frozen", "--no-dev", "--group", "tests", "--extra", "all"]
VENV_BUILD_TIMEOUT_S = 900


def affected_modules(root: Path) -> list[str]:
    """Existing test modules that exercise the security schemes."""
    found = sorted(Path(p).relative_to(root).as_posix() for p in glob.glob(str(root / "tests" / "test_security_*.py")))
    return found + [m for m in EXTRA_MODULES if (root / m).is_file()]


def auto_error_modules() -> list[str]:
    """Original test modules that pass the old keyword and need mechanical migration."""
    return [m for m in affected_modules(FIXTURE) if "auto_error" in (FIXTURE / m).read_text()]


def _run(command: list[str], cwd: Path, timeout: int = GRADE_TIMEOUT_S) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)


def _grade(directory: Path, label: str, modules: list[str], deadline: float) -> str | None:
    """Run the affected modules; return a failure reason or None."""
    command = [*PYTEST_COMMAND, *modules]
    remaining = deadline - time.monotonic()
    try:
        if remaining <= 0:
            raise subprocess.TimeoutExpired(command, 0)
        result = _run(command, directory, min(GRADE_TIMEOUT_S, remaining))
    except subprocess.TimeoutExpired:
        return f"{TIMEOUT_REASON}: {label} tests timed out or hit the grading deadline: pytest {len(modules)} modules"
    except OSError as error:
        return f"{label} tests could not start: {error}"
    if result.returncode:
        detail = (result.stdout + result.stderr)[-1500:]
        return f"{label} tests failed: pytest {len(modules)} modules\n{detail}"
    return None


def _restore_tests(target: Path, *, held_out: bool = True) -> None:
    """Use pinned assertions, with only the keyword rename needed to avoid the deprecation."""
    for relative in affected_modules(FIXTURE):
        text = (FIXTURE / relative).read_text()
        text, count = re.subn(r"\bauto_error=", "raise_on_missing=", text)
        if count:
            assert count == 1, f"{relative} passes auto_error more than once"
        (target / relative).write_text(text)
    if held_out:
        shutil.copy2(HELD_OUT_SOURCE, target / HELD_OUT_TARGET)


def _exclude_venv(workdir: Path) -> None:
    """Keep ``.venv/`` out of ``git status``, so the grader does not count it as an agent change."""
    result = subprocess.run(
        ["git", "rev-parse", "--git-path", "info/exclude"],
        cwd=workdir, capture_output=True, text=True,
    )
    if result.returncode:
        return
    exclude = workdir / result.stdout.strip()
    existing = exclude.read_text() if exclude.is_file() else ""
    if ".venv/" in existing.splitlines():
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a") as handle:
        handle.write(("\n" if existing and not existing.endswith("\n") else "") + ".venv/\n")


def build_agent_venv(workdir: Path) -> None:
    """Create ``<workdir>/.venv`` with fastapi editable and the locked test dependencies."""
    venv = workdir / ".venv"
    if (venv / "bin").is_dir():
        return
    # uv hardlinks venv files to its cache by default, so copy: an in-place edit must not reach other venvs.
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(venv), "UV_LINK_MODE": "copy"}
    env.pop("VIRTUAL_ENV", None)
    result = subprocess.run(
        VENV_SYNC_COMMAND, cwd=workdir, env=env, capture_output=True, text=True, timeout=VENV_BUILD_TIMEOUT_S,
    )
    if result.returncode:
        shutil.rmtree(venv, ignore_errors=True)
        raise RuntimeError(f"agent venv build failed ({result.returncode}): {(result.stderr or result.stdout)[-800:]}")


class FastAPISecurityMigrationTask(Task):
    capability = "fix"

    def prepare(self, workdir: Path) -> None:
        super().prepare(workdir)
        _exclude_venv(workdir)
        build_agent_venv(workdir)

    @property
    def name(self) -> str:
        return "fastapi_edit_security_raise_on_missing"

    @property
    def repo(self) -> str:
        return "fastapi"

    @property
    def task_type(self) -> str:
        return "edit"

    @property
    def ground_truth(self) -> GroundTruth:
        return GroundTruth()

    @property
    def prompt(self) -> str:
        return (
            "Migrate FastAPI's security schemes from the boolean keyword auto_error to "
            "a new keyword raise_on_missing, with the same meaning and a default of "
            "True. This is a forward edit, not a repair: do not revert it. Every scheme "
            "that accepts auto_error must accept raise_on_missing instead: APIKeyQuery, "
            "APIKeyHeader, APIKeyCookie, HTTPBase, HTTPBasic, HTTPBearer, HTTPDigest, "
            "OAuth2, OAuth2PasswordBearer, OAuth2AuthorizationCodeBearer, and "
            "OpenIdConnect. All of their missing-credential branches must read the "
            "new setting, and the parameter docs must describe it under its new name. "
            "Keep auto_error as an optional keyword so existing callers keep working. "
            "Passing it must emit exactly one DeprecationWarning that names both "
            "auto_error and raise_on_missing and points at the code that constructed "
            "the scheme, then behave exactly like raise_on_missing with the same value, "
            "including when a scheme forwards its settings to a parent class. Passing "
            "both keywords must raise TypeError, even when the two values are equal. "
            "The auto_error attribute must stay readable and writable on every scheme "
            "without any warning, and must stay in sync with raise_on_missing, because "
            "third-party subclasses read and assign it. Do not change status codes, "
            "error details, challenge headers, or the generated OpenAPI. Update the "
            "existing tests that pass auto_error so they use the new keyword; the "
            "project turns warnings into errors. Do not weaken tests. The existing "
            "security test modules must pass. Leave changes uncommitted."
        )

    def check_correctness(self, result_text: str, repo_path: str) -> tuple[bool, str]:
        candidate = Path(repo_path)
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=candidate, capture_output=True, text=True,
        )
        if status.returncode:
            return False, "Cannot inspect candidate changes"
        changes = [line[3:] for line in status.stdout.splitlines()]
        if not changes:
            return False, "No source changes"
        original_tests = set(affected_modules(FIXTURE))
        source_changes = set()
        for relative in changes:
            path = Path(relative)
            parts = path.parts
            source = len(parts) == 3 and "/".join(parts[:2]) == SECURITY_SOURCE_DIR and path.suffix == ".py"
            new_test = (
                len(parts) == 2 and parts[0] == "tests" and path.name.startswith("test_")
                and path.suffix == ".py" and not (FIXTURE / relative).exists()
            )
            test = relative in original_tests or new_test
            entry = candidate / relative
            if not (source or test) or entry.is_symlink() or not entry.is_file():
                return False, f"Unsafe changed path: {relative}"
            if source:
                source_changes.add(relative)
        if not source_changes:
            return False, "No security scheme changes"
        deadline = time.monotonic() + GRADE_DEADLINE_S
        with tempfile.TemporaryDirectory(prefix="fastapi-security-grade-") as temp:
            target = Path(temp)
            shutil.copytree(FIXTURE, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git", "__pycache__"))
            for relative in source_changes:
                shutil.copy2(candidate / relative, target / relative)
            _restore_tests(target)
            modules = [*affected_modules(target), HELD_OUT_TARGET]
            failure = _grade(target, "Trusted", modules, deadline)
            if failure:
                return False, failure
        failure = _grade(candidate, "Candidate", affected_modules(candidate), deadline)
        if failure:
            return False, failure
        return True, "Trusted and candidate tests pass"
