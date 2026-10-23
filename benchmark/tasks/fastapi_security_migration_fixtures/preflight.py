"""Check the pinned fixture, reference solution, and negative controls."""

import difflib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from benchmark.tasks.fastapi_security_migration_tasks import (
    FIXTURE,
    TIMEOUT_REASON,
    FastAPISecurityMigrationTask,
    auto_error_modules,
)
from benchmark.tasks.fastapi_security_migration_fixtures.reference import apply_reference

PINNED = "6fa573ce0bc16fe445f93db413d20146dd9ff35d"


def clone(target: Path) -> None:
    subprocess.run(["git", "clone", "--quiet", "--shared", str(FIXTURE), str(target)], check=True, capture_output=True, text=True)


def reference_size(target: Path) -> tuple[int, int, int, int]:
    changed = subprocess.run(
        ["git", "status", "--porcelain"], cwd=target, check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    sites = added = removed = 0
    for status in changed:
        relative = status[3:]
        before = (FIXTURE / relative).read_text().splitlines()
        after = (target / relative).read_text().splitlines()
        for tag, start_a, end_a, start_b, end_b in difflib.SequenceMatcher(a=before, b=after, autojunk=False).get_opcodes():
            if tag != "equal":
                sites += 1
                added += end_b - start_b
                removed += end_a - start_a
    return len(changed), sites, added, removed


def assert_rejected(ok: bool, reason: str) -> None:
    """A negative control passes only on a real rejection, never on a grader timeout."""
    assert not ok, reason
    assert not reason.startswith(TIMEOUT_REASON), reason


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert text.count(old) >= 1, (path, old)
    path.write_text(text.replace(old, new, 1))


def main() -> None:
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=FIXTURE, check=True, capture_output=True, text=True).stdout.strip()
    assert revision == PINNED, revision
    cleanliness = subprocess.run(["git", "status", "--porcelain"], cwd=FIXTURE, check=True, capture_output=True, text=True).stdout
    assert not cleanliness, "pinned FastAPI fixture is dirty"
    assert len(auto_error_modules()) == 15, auto_error_modules()
    task = FastAPISecurityMigrationTask()
    with tempfile.TemporaryDirectory(prefix="fastapi-security-preflight-") as temp:
        root = Path(temp)

        def prepared(name: str) -> Path:
            target = root / name
            clone(target)
            apply_reference(target)
            return target

        def rejected(name: str, label: str, mutate, expect: str | None = None) -> str:
            target = prepared(name)
            mutate(target)
            ok, reason = task.check_correctness("", str(target))
            assert_rejected(ok, reason)
            if expect:
                assert reason.startswith(expect), reason
            return f"{label}: rejected {reason.splitlines()[0]}"

        def original() -> str:
            target = root / "original"
            clone(target)
            ok, reason = task.check_correctness("", str(target))
            assert_rejected(ok, reason)
            return f"original: rejected {reason}"

        def reference() -> str:
            target = prepared("reference")
            files, sites, added, removed = reference_size(target)
            lines = [f"reference size: {files} files, {sites} edit sites, +{added}/-{removed} lines"]
            ok, reason = task.check_correctness("", str(target))
            assert ok, reason
            lines.append(f"reference: accepted {reason}")
            return "\n".join(lines)

        def local_helper() -> str:
            target = prepared("local-helper")
            (target / "fastapi" / "security" / "helper.py").write_text("VALUE = 1\n")
            (target / "tests" / "test_security_local_helper.py").write_text("def test_ok():\n    assert True\n")
            ok, reason = task.check_correctness("", str(target))
            assert ok, reason
            return f"new local helper and test: accepted {reason}"

        def unrelated_path() -> str:
            return rejected(
                "unrelated-path", "unapproved changed path",
                lambda t: (t / "fastapi" / "utils.py").write_text((t / "fastapi" / "utils.py").read_text() + "\n"),
                "Unsafe changed path",
            )

        def restore(target: Path, relative: str) -> None:
            shutil.copy2(FIXTURE / relative, target / relative)

        def tests_unmigrated() -> str:
            return rejected(
                "tests-unmigrated", "one existing test still passes auto_error",
                lambda t: restore(t, "tests/test_security_http_bearer_optional.py"), "Candidate tests failed",
            )

        def oauth2_unmigrated() -> str:
            return rejected("oauth2-unmigrated", "oauth2.py left on auto_error", lambda t: restore(t, "fastapi/security/oauth2.py"))

        def openid_unmigrated() -> str:
            return rejected(
                "openid-unmigrated", "open_id_connect_url.py left on auto_error",
                lambda t: restore(t, "fastapi/security/open_id_connect_url.py"),
            )

        def one_class_unmigrated() -> str:
            def mutate(t: Path) -> None:
                edit(t / "fastapi/security/http.py", "        self.raise_on_missing = resolve_raise_on_missing(raise_on_missing, auto_error)\n", "        self.raise_on_missing = True\n")
            return rejected("one-class", "HTTPBase ignores its keyword", mutate)

        def no_type_error() -> str:
            def mutate(t: Path) -> None:
                edit(t / "fastapi/security/base.py", "    if raise_on_missing is not None:\n        raise TypeError(", "    if False:\n        raise TypeError(")
            return rejected("no-type-error", "both keywords accepted", mutate, "Trusted tests failed")

        def wrong_stacklevel() -> str:
            def mutate(t: Path) -> None:
                edit(t / "fastapi/security/base.py", "stacklevel=3", "stacklevel=2")
            return rejected("stacklevel", "warning points inside FastAPI", mutate, "Trusted tests failed")

        def no_alias() -> str:
            def mutate(t: Path) -> None:
                edit(t / "fastapi/security/base.py", "    @property\n    def auto_error", "    @property\n    def _removed_auto_error")
                edit(t / "fastapi/security/base.py", "    @auto_error.setter\n    def auto_error", "    @_removed_auto_error.setter\n    def _removed_auto_error")
            return rejected("no-alias", "auto_error attribute removed", mutate, "Trusted tests failed")

        def inverted_default() -> str:
            def mutate(t: Path) -> None:
                edit(t / "fastapi/security/base.py", "return True if raise_on_missing is None else raise_on_missing", "return False if raise_on_missing is None else raise_on_missing")
            return rejected("inverted-default", "default flipped to optional", mutate, "Trusted tests failed")

        def stale_docs() -> str:
            def mutate(t: Path) -> None:
                path = t / "fastapi/security/api_key.py"
                path.write_text(path.read_text().replace("`raise_on_missing` is set to `False`", "`auto_error` is set to `False`"))
            return rejected("stale-docs", "api_key.py docs still name auto_error", mutate, "Trusted tests failed")

        controls = (
            original, reference, local_helper, unrelated_path, tests_unmigrated, oauth2_unmigrated,
            openid_unmigrated, one_class_unmigrated, no_type_error, wrong_stacklevel, no_alias, inverted_default,
            stale_docs,
        )
        for control in controls:
            print(control(), flush=True)


if __name__ == "__main__":
    main()
