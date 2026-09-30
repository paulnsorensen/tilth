"""Check the pinned fixture, reference solution, and negative controls."""

import difflib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from benchmark.tasks.gin_render_context_tasks import FIXTURE, ROOT_TEST_PATTERN, GinRenderContextTask
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
        original = root / "original"
        clone(original)
        ok, reason = task.check_correctness("", str(original))
        assert not ok, reason
        print("original: rejected", reason)

        reference = root / "reference"
        clone(reference)
        apply_reference(reference)
        binding_test = (reference / "binding" / "json_test.go").read_text()
        assert "(render.PureJSON{Data: obj}).Render(context.Background(), w)" in binding_test
        files, sites, added, removed = reference_size(reference)
        print(f"reference size: {files} files, {sites} edit sites, +{added}/-{removed} lines")
        ok, reason = task.check_correctness("", str(reference))
        assert ok, reason
        print("reference: accepted", reason)
        (reference / "render" / "context_helper.go").write_text("package render\n")
        (reference / "context_render_context_test.go").write_text("package gin\n")
        ok, reason = task.check_correctness("", str(reference))
        assert ok, reason
        print("new local helper and test: accepted", reason)

        unmigrated_binding = root / "unmigrated-binding"
        clone(unmigrated_binding)
        apply_reference(unmigrated_binding)
        shutil.copy2(FIXTURE / "binding" / "json_test.go", unmigrated_binding / "binding" / "json_test.go")
        ok, reason = task.check_correctness("", str(unmigrated_binding))
        assert not ok and "Candidate tests failed: go test" in reason, reason
        print("unmigrated binding caller: rejected", reason.splitlines()[0])
        incomplete = root / "incomplete"
        clone(incomplete)
        apply_reference(incomplete)
        shutil.copy2(FIXTURE / "render" / "bson.go", incomplete / "render" / "bson.go")
        ok, reason = task.check_correctness("", str(incomplete))
        assert not ok, reason
        print("incomplete API: rejected", reason.splitlines()[0])

        cancellation = root / "cancellation"
        clone(cancellation)
        apply_reference(cancellation)
        path = cancellation / "render" / "json.go"
        text = path.read_text()
        assert CHECK in text
        path.write_text(text.replace(CHECK, "", 1))
        test_path = cancellation / "render" / "render_test.go"
        original_test = test_path.read_text()
        weakened_test = original_test.replace('assert.Equal(t, "application/json; charset=utf-8", w.Header().Get("Content-Type"))', '// removed assertion', 1)
        assert weakened_test != original_test
        test_path.write_text(weakened_test)
        ok, reason = task.check_correctness("", str(cancellation))
        assert not ok, reason
        print("missing cancellation with weakened test: rejected", reason.splitlines()[0])

        ordering = root / "ordering"
        clone(ordering)
        apply_reference(ordering)
        path = ordering / "render" / "json.go"
        text = path.read_text()
        assert CHECK in text
        path.write_text(text.replace(CHECK, '\tw.Header().Set("X-Leak", "yes")\n' + CHECK, 1))
        ok, reason = task.check_correctness("", str(ordering))
        assert not ok, reason
        print("header before cancellation: rejected", reason.splitlines()[0])
        dispatch = root / "dispatch"
        clone(dispatch)
        apply_reference(dispatch)
        path = dispatch / "context.go"
        text = path.read_text()
        text = text.replace("renderContext = c.Request.Context()", "renderContext = context.Background()", 1)
        path.write_text(text)
        ok, reason = task.check_correctness("", str(dispatch))
        assert not ok, reason
        print("missing dispatch: rejected", reason.splitlines()[0])


if __name__ == "__main__":
    main()
