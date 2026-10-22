"""FastAPI security-scheme keyword migration with trusted, external grading tests."""

import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
TIMEOUT_REASON = "Grader timeout (inconclusive, not a test failure)"
PYTHON_ENV = "FASTAPI_BENCH_PYTHON"


def _imports_fastapi_stack(python: str) -> bool:
    probe = "import starlette, pydantic, httpx, pytest, annotated_doc, multipart"
    try:
        return subprocess.run([python, "-c", probe], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def grading_python() -> str:
    """Pick an interpreter that has the FastAPI test dependencies installed."""
    candidates = [os.environ.get(PYTHON_ENV), sys.executable, shutil.which("python3"), "/usr/bin/python3"]
    for candidate in candidates:
        if candidate and _imports_fastapi_stack(candidate):
            return candidate
    raise RuntimeError(f"No interpreter with FastAPI test dependencies; set {PYTHON_ENV}")


def affected_modules(root: Path) -> list[str]:
    """Existing test modules that exercise the security schemes."""
    found = sorted(Path(p).relative_to(root).as_posix() for p in glob.glob(str(root / "tests" / "test_security_*.py")))
    return found + [m for m in EXTRA_MODULES if (root / m).is_file()]


def auto_error_modules() -> list[str]:
    """Original test modules that pass the old keyword and need mechanical migration."""
    return [m for m in affected_modules(FIXTURE) if "auto_error" in (FIXTURE / m).read_text()]


def _run(command: list[str], cwd: Path, timeout: int = GRADE_TIMEOUT_S) -> subprocess.CompletedProcess[str]:
    extra = os.environ.get("PYTHONPATH")
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(cwd), extra] if extra else [str(cwd)]),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)


def _grade(directory: Path, label: str, modules: list[str]) -> str | None:
    """Run the affected modules; return a failure reason or None."""
    command = [grading_python(), "-m", "pytest", "-q", "-p", "no:cacheprovider", *modules]
    try:
        result = _run(command, directory)
    except subprocess.TimeoutExpired:
        return f"{TIMEOUT_REASON}: {label} tests timed out: pytest {len(modules)} modules"
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


class FastAPISecurityMigrationTask(Task):
    capability = "fix"

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
        with tempfile.TemporaryDirectory(prefix="fastapi-security-grade-") as temp:
            target = Path(temp)
            shutil.copytree(FIXTURE, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git", "__pycache__"))
            for relative in source_changes:
                shutil.copy2(candidate / relative, target / relative)
            _restore_tests(target)
            modules = [*affected_modules(target), HELD_OUT_TARGET]
            failure = _grade(target, "Trusted", modules)
            if failure:
                return False, failure
        failure = _grade(candidate, "Candidate", affected_modules(candidate))
        if failure:
            return False, failure
        return True, "Trusted and candidate tests pass"
