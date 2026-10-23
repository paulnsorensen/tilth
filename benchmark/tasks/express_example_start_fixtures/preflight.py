"""Check the pinned fixture, reference solution, and negative controls."""

import difflib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from benchmark.tasks.express_example_start_tasks import FIXTURE, TIMEOUT_REASON, ExpressExampleStartTask
from benchmark.tasks.express_example_start_fixtures.reference import apply_reference

PINNED = "1140301f6a0ed5a05bc1ef38d48294f75a49580c"


def clone(target: Path) -> None:
    subprocess.run(["git", "clone", "--quiet", "--shared", str(FIXTURE), str(target)], check=True, capture_output=True, text=True)
    os.symlink(FIXTURE / "node_modules", target / "node_modules")


def restore(target: Path, *relatives: str) -> None:
    for relative in relatives:
        shutil.copy2(FIXTURE / relative, target / relative)


def edit(target: Path, relative: str, old: str, new: str) -> None:
    path = target / relative
    text = path.read_text()
    assert text.count(old) == 1, (relative, old)
    path.write_text(text.replace(old, new))


def reference_size(target: Path) -> tuple[int, int, int, int]:
    changed = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=target, check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    sites = added = removed = 0
    for status in changed:
        relative = status[3:]
        original = FIXTURE / relative
        before = original.read_text().splitlines() if original.exists() else []
        after = (target / relative).read_text().splitlines()
        for tag, start_a, end_a, start_b, end_b in difflib.SequenceMatcher(a=before, b=after).get_opcodes():
            if tag != "equal":
                sites += 1
                added += end_b - start_b
                removed += end_a - start_a
    return len(changed), sites, added, removed


def assert_rejected(ok: bool, reason: str) -> None:
    """A negative control passes only on a real rejection, never on a grader timeout."""
    assert not ok, reason
    assert not reason.startswith(TIMEOUT_REASON), reason


def main() -> None:
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=FIXTURE, check=True, capture_output=True, text=True).stdout.strip()
    assert revision == PINNED, revision
    cleanliness = subprocess.run(["git", "status", "--porcelain"], cwd=FIXTURE, check=True, capture_output=True, text=True).stdout
    assert not cleanliness, "pinned Express fixture is dirty"
    task = ExpressExampleStartTask()
    with tempfile.TemporaryDirectory(prefix="express-start-preflight-") as temp:
        root = Path(temp)

        def candidate(name: str, *, migrate: bool = True) -> Path:
            target = root / name
            clone(target)
            if migrate:
                apply_reference(target)
            return target

        def rejected(name: str, label: str, target: Path, expect: str | None = None) -> str:
            ok, reason = task.check_correctness("", str(target))
            assert_rejected(ok, reason)
            if expect:
                assert expect in reason, reason
            return f"{label}: rejected {reason.splitlines()[0]}"

        def original() -> str:
            return rejected("original", "original", candidate("original", migrate=False))

        def reference() -> str:
            target = candidate("reference")
            files, sites, added, removed = reference_size(target)
            lines = [f"reference size: {files} files, {sites} edit sites, +{added}/-{removed} lines"]
            ok, reason = task.check_correctness("", str(target))
            assert ok, reason
            lines.append(f"reference: accepted {reason}")
            return "\n".join(lines)

        def local_helper() -> str:
            target = candidate("local-helper")
            (target / "examples" / "support.js").write_text("'use strict'\n")
            (target / "test" / "examples.start.local.js").write_text("'use strict'\n")
            ok, reason = task.check_correctness("", str(target))
            assert ok, reason
            return f"new local helper and test: accepted {reason}"

        def helper_only() -> str:
            target = candidate("helper-only", migrate=False)
            shutil.copy2(root / "reference" / "examples" / "start.js", target / "examples" / "start.js")
            return rejected("helper-only", "helper without migrating examples", target)

        def incomplete_guarded() -> str:
            target = candidate("incomplete-guarded")
            restore(target, "examples/ejs/index.js", "examples/web-service/index.js")
            return rejected("incomplete-guarded", "two guarded examples not migrated", target, "Trusted tests failed")

        def incomplete_unguarded() -> str:
            target = candidate("incomplete-unguarded")
            restore(target, "examples/static-files/index.js", "examples/session/redis.js")
            return rejected("incomplete-unguarded", "unguarded examples not migrated", target, "Trusted tests failed")

        def incomplete_quiet() -> str:
            target = candidate("incomplete-quiet")
            restore(target, "examples/route-map/index.js", "examples/cookies/index.js")
            return rejected("incomplete-quiet", "route-map and cookies left on NODE_ENV", target, "Trusted tests failed")

        def inverted_guard() -> str:
            target = candidate("inverted-guard")
            edit(target, "examples/mvc/index.js", "{ verbose: isMain(module) }", "{ verbose: !isMain(module) }")
            return rejected("inverted-guard", "mvc verbose flag inverted", target, "Trusted tests failed")

        def ignores_port() -> str:
            target = candidate("ignores-port")
            edit(target, "examples/start.js", "Number(requested)", "3000")
            return rejected("ignores-port", "helper ignores PORT", target, "Trusted tests failed")

        def always_hints() -> str:
            target = candidate("always-hints")
            edit(target, "examples/static-files/index.js", "if (isMain(module)) {", "if (true) {")
            return rejected("always-hints", "static-files prints hints when required", target, "Trusted tests failed")

        def edited_test() -> str:
            target = candidate("edited-test")
            path = target / "test" / "app.js"
            path.write_text(path.read_text() + "\n")
            return rejected("edited-test", "existing test edited", target, "Unsafe changed path")

        # The controls share the machine with other test runs, so they run one at a time.
        for control in (original, reference, local_helper, helper_only, incomplete_guarded, incomplete_unguarded,
                        incomplete_quiet, inverted_guard, ignores_port, always_hints, edited_test):
            print(control())


if __name__ == "__main__":
    main()
