"""Check the pinned fixture, reference solution, and negative controls."""

import difflib
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from benchmark.tasks.gin_render_context_tasks import FIXTURE, ROOT_TEST_PATTERN, TIMEOUT_REASON, GinRenderContextTask
from benchmark.tasks.gin_render_context_fixtures.reference import CHECK, apply_reference


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
    assert revision == "d7776de7d444935ea4385999711bd6331a98fecb", revision
    cleanliness = subprocess.run(["git", "status", "--porcelain"], cwd=FIXTURE, check=True, capture_output=True, text=True).stdout
    assert not cleanliness, "pinned Gin fixture is dirty"
    inventory = subprocess.run(
        ["go", "test", "-list", ROOT_TEST_PATTERN, "."],
        cwd=FIXTURE, check=True, capture_output=True, text=True,
    ).stdout
    for name in ("TestContextRenderIfErr", "TestContextRenderSSE", "TestMiddlewareWrite"):
        assert name in inventory.splitlines(), f"root test selection misses {name}"
    task = GinRenderContextTask()
    with tempfile.TemporaryDirectory(prefix="gin-render-preflight-") as temp:
        root = Path(temp)

        def original() -> str:
            target = root / "original"
            clone(target)
            ok, reason = task.check_correctness("", str(target))
            assert_rejected(ok, reason)
            return f"original: rejected {reason}"

        def reference() -> str:
            target = root / "reference"
            clone(target)
            apply_reference(target)
            binding_test = (target / "binding" / "json_test.go").read_text()
            assert "(render.PureJSON{Data: obj}).Render(context.Background(), w)" in binding_test
            files, sites, added, removed = reference_size(target)
            lines = [f"reference size: {files} files, {sites} edit sites, +{added}/-{removed} lines"]
            ok, reason = task.check_correctness("", str(target))
            assert ok, reason
            lines.append(f"reference: accepted {reason}")
            return "\n".join(lines)

        def local_helper() -> str:
            target = root / "local-helper"
            clone(target)
            apply_reference(target)
            (target / "render" / "context_helper.go").write_text("package render\n")
            (target / "context_render_context_test.go").write_text("package gin\n")
            ok, reason = task.check_correctness("", str(target))
            assert ok, reason
            return f"new local helper and test: accepted {reason}"

        def unmigrated_binding() -> str:
            target = root / "unmigrated-binding"
            clone(target)
            apply_reference(target)
            shutil.copy2(FIXTURE / "binding" / "json_test.go", target / "binding" / "json_test.go")
            ok, reason = task.check_correctness("", str(target))
            assert_rejected(ok, reason)
            assert "Candidate tests failed: go test" in reason, reason
            return f"unmigrated binding caller: rejected {reason.splitlines()[0]}"

        def incomplete() -> str:
            target = root / "incomplete"
            clone(target)
            apply_reference(target)
            shutil.copy2(FIXTURE / "render" / "bson.go", target / "render" / "bson.go")
            ok, reason = task.check_correctness("", str(target))
            assert_rejected(ok, reason)
            return f"incomplete API: rejected {reason.splitlines()[0]}"

        def cancellation() -> str:
            target = root / "cancellation"
            clone(target)
            apply_reference(target)
            path = target / "render" / "json.go"
            text = path.read_text()
            assert CHECK in text
            path.write_text(text.replace(CHECK, "", 1))
            test_path = target / "render" / "render_test.go"
            original_test = test_path.read_text()
            weakened_test = original_test.replace('assert.Equal(t, "application/json; charset=utf-8", w.Header().Get("Content-Type"))', '// removed assertion', 1)
            assert weakened_test != original_test
            test_path.write_text(weakened_test)
            ok, reason = task.check_correctness("", str(target))
            assert_rejected(ok, reason)
            return f"missing cancellation with weakened test: rejected {reason.splitlines()[0]}"

        def ordering() -> str:
            target = root / "ordering"
            clone(target)
            apply_reference(target)
            path = target / "render" / "json.go"
            text = path.read_text()
            assert CHECK in text
            path.write_text(text.replace(CHECK, '\tw.Header().Set("X-Leak", "yes")\n' + CHECK, 1))
            ok, reason = task.check_correctness("", str(target))
            assert_rejected(ok, reason)
            return f"header before cancellation: rejected {reason.splitlines()[0]}"

        def dispatch() -> str:
            target = root / "dispatch"
            clone(target)
            apply_reference(target)
            path = target / "context.go"
            text = path.read_text()
            text = text.replace("renderContext = c.Request.Context()", "renderContext = context.Background()", 1)
            path.write_text(text)
            ok, reason = task.check_correctness("", str(target))
            assert_rejected(ok, reason)
            return f"missing dispatch: rejected {reason.splitlines()[0]}"

        # Each control grades its own clone in its own temp trees.
        # The Go build cache is safe to share, so the controls run in parallel.
        controls = (original, reference, local_helper, unmigrated_binding, incomplete, cancellation, ordering,
                    dispatch)
        with ThreadPoolExecutor(max_workers=len(controls)) as pool:
            futures = [pool.submit(control) for control in controls]
            for future in futures:
                print(future.result())


if __name__ == "__main__":
    main()
