"""Check the pinned fixture, reference solution, and negative controls."""

import difflib
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from benchmark.tasks import rg_flow_migration_tasks as module
from benchmark.tasks.rg_flow_migration_tasks import FIXTURE, TIMEOUT_REASON, RgFlowMigrationTask
from benchmark.tasks.rg_flow_migration_fixtures.reference import (
    EXAMPLE_FILE, FIND_ITER_STOP, MATCHER_IMPL_FILES, PRINTER_FILES, QUIET_STOP, SEARCHER_STOP,
    apply_reference, break_behavior,
)

REVISION = "0a88cccd5188074de96f54a4b6b44a63971ac157"


def clone(target: Path) -> None:
    subprocess.run(["git", "clone", "--quiet", "--shared", str(FIXTURE), str(target)], check=True, capture_output=True, text=True)


def reference_size(target: Path) -> tuple[int, int, int, int, int]:
    changed = subprocess.run(
        ["git", "status", "--porcelain"], cwd=target, check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    sites = added = removed = 0
    crates = set()
    for status in changed:
        relative = status[3:]
        crates.add(relative.split("/")[1])
        before = (FIXTURE / relative).read_text().splitlines()
        after = (target / relative).read_text().splitlines()
        for tag, start_a, end_a, start_b, end_b in difflib.SequenceMatcher(a=before, b=after, autojunk=False).get_opcodes():
            if tag != "equal":
                sites += 1
                added += end_b - start_b
                removed += end_a - start_a
    return len(changed), sites, len(crates), added, removed


def assert_rejected(ok: bool, reason: str) -> None:
    """A negative control passes only on a real rejection, never on a grader timeout."""
    assert not ok, reason
    assert not reason.startswith(TIMEOUT_REASON), reason


def revert(target: Path, files: tuple[str, ...]) -> None:
    for relative in files:
        shutil.copy2(FIXTURE / relative, target / relative)


def first_line(reason: str) -> str:
    return reason.splitlines()[0]


def main() -> None:
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=FIXTURE, check=True, capture_output=True, text=True).stdout.strip()
    assert revision == REVISION, revision
    cleanliness = subprocess.run(["git", "status", "--porcelain"], cwd=FIXTURE, check=True, capture_output=True, text=True).stdout
    assert not cleanliness, "pinned ripgrep fixture is dirty"
    task = RgFlowMigrationTask()
    with tempfile.TemporaryDirectory(prefix="rg-flow-preflight-") as temp:
        root = Path(temp)

        def candidate(name: str, *, reference: bool = True) -> Path:
            target = root / name
            clone(target)
            if reference:
                apply_reference(target)
            return target

        def original() -> str:
            ok, reason = task.check_correctness("", str(candidate("original", reference=False)))
            assert_rejected(ok, reason)
            return f"original (no change): rejected {reason}"

        def original_with_held_out_tests() -> str:
            target = root / "original-held-out"
            shutil.copytree(FIXTURE, target, ignore=shutil.ignore_patterns(".git"))
            module._inject_tests(target)
            reason = module._grade(target, "Trusted", time.monotonic() + module.GRADE_DEADLINE_S)
            assert reason, "held-out tests passed on unchanged source"
            assert_rejected(False, reason)
            return f"original source with held-out tests: rejected {first_line(reason)}"

        def reference() -> str:
            target = candidate("reference")
            files, sites, crates, added, removed = reference_size(target)
            assert files >= 10 and sites >= 40 and crates >= 2, (files, sites, crates)
            lines = [f"reference size: {files} files, {sites} edit sites, {crates} crates, +{added}/-{removed} lines"]
            ok, reason = task.check_correctness("", str(target))
            assert ok, reason
            lines.append(f"reference: accepted {reason}")
            return "\n".join(lines)

        def unsafe_manifest() -> str:
            target = candidate("unsafe-manifest")
            manifest = target / "crates" / "searcher" / "Cargo.toml"
            manifest.write_text(manifest.read_text() + "\n# edited\n")
            ok, reason = task.check_correctness("", str(target))
            assert_rejected(ok, reason)
            assert reason.startswith("Unsafe changed path"), reason
            return f"edited manifest: rejected {reason}"

        def incomplete_printers() -> str:
            target = candidate("incomplete-printers")
            revert(target, PRINTER_FILES)
            ok, reason = task.check_correctness("", str(target))
            assert_rejected(ok, reason)
            return f"printers not migrated: rejected {first_line(reason)}"

        def incomplete_matchers() -> str:
            target = candidate("incomplete-matchers")
            revert(target, MATCHER_IMPL_FILES)
            ok, reason = task.check_correctness("", str(target))
            assert_rejected(ok, reason)
            return f"regex and pcre2 matchers not migrated: rejected {first_line(reason)}"

        def incomplete_example() -> str:
            target = candidate("incomplete-example")
            revert(target, (EXAMPLE_FILE,))
            ok, reason = task.check_correctness("", str(target))
            assert_rejected(ok, reason)
            return f"searcher example not migrated: rejected {first_line(reason)}"

        def quiet_summary() -> str:
            target = candidate("quiet-summary")
            break_behavior(target, QUIET_STOP)
            ok, reason = task.check_correctness("", str(target))
            assert_rejected(ok, reason)
            return f"quiet summary no longer stops: rejected {first_line(reason)}"

        def searcher_stop() -> str:
            target = candidate("searcher-stop")
            break_behavior(target, SEARCHER_STOP)
            ok, reason = task.check_correctness("", str(target))
            assert_rejected(ok, reason)
            return f"searcher ignores an after-context Stop: rejected {first_line(reason)}"

        def find_iter_stop() -> str:
            target = candidate("find-iter-stop")
            break_behavior(target, FIND_ITER_STOP)
            ok, reason = task.check_correctness("", str(target))
            assert_rejected(ok, reason)
            return f"try_find_iter ignores Stop: rejected {first_line(reason)}"

        def stale_target() -> str:
            """Grade the reference, then an incomplete copy in the same directory, on one target dir.

            The printer files return with their old fixture mtimes, so a cargo build keyed on
            source mtimes would reuse the migrated printer from the first grade.
            """
            target = candidate("stale-target")
            module._inject_tests(target)
            first = module._grade(target, "Trusted", time.monotonic() + module.GRADE_DEADLINE_S)
            assert first is None, first
            revert(target, PRINTER_FILES)
            reason = module._grade(target, "Trusted", time.monotonic() + module.GRADE_DEADLINE_S)
            assert reason, "incomplete migration passed on a target dir warmed by the reference"
            assert_rejected(False, reason)
            return f"reference then printers not migrated, same directory and target dir: rejected {first_line(reason)}"

        # One cargo build at a time: the controls share one target dir and the host limit is 4 jobs.
        controls = (original, original_with_held_out_tests, reference, unsafe_manifest, incomplete_printers,
                    incomplete_matchers, incomplete_example, quiet_summary, searcher_stop, find_iter_stop,
                    stale_target)
        selected = set(sys.argv[1:])
        for control in controls:
            if not selected or control.__name__ in selected:
                print(control(), flush=True)


if __name__ == "__main__":
    main()
