"""Forward Gin render API migration with trusted, external grading tests."""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .base import GroundTruth, Task


FIXTURE = Path("/tmp/tilth_bench/repos/gin")
SOURCE_FILES = {"context.go", *(f"render/{name}.go" for name in (
    "bson", "data", "html", "json", "msgpack", "protobuf", "reader",
    "redirect", "render", "text", "toml", "xml", "yaml",
))}
HELD_OUT = Path(__file__).parent / "gin_render_context_fixtures"
ROOT_TEST_PATTERN = "^(TestContext|TestMiddleware|TestHeldOut)"


def _grade_commands(tags: list[str]) -> list[list[str]]:
    return [
        ["go", "test", *tags, "./render"],
        ["go", "test", *tags, "-run", ROOT_TEST_PATTERN, "."],
        ["go", "test", *tags, "./binding"],
        ["go", "test", *tags, "-run", "^$", "./..."],
    ]


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=300)


def _restore_tests(target: Path, *, held_out: bool = True) -> None:
    """Use pinned assertions, with only call-signature changes needed to compile."""
    for original in [FIXTURE / "context_test.go", FIXTURE / "middleware_test.go", FIXTURE / "binding/json_test.go", *sorted((FIXTURE / "render").glob("*_test.go"))]:
        relative = original.relative_to(FIXTURE)
        text = original.read_text()
        if relative.as_posix() == "context_test.go":
            old = "func (*TestRender) Render(http.ResponseWriter) error"
            assert text.count(old) == 1
            text = text.replace(old, "func (*TestRender) Render(context.Context, http.ResponseWriter) error")
            old_call = 'c.Render(-1, sse.Event{\n\t\tId:   "123",\n\t\tData: "text",\n\t})'
            assert text.count(old_call) == 1
            text = text.replace(old_call, 'c.Render(-1, testSSEContextRender{sse.Event{\n\t\tId:   "123",\n\t\tData: "text",\n\t}})')
            text += '\n// testSSEContextRender adapts the external renderer to the new test API.\ntype testSSEContextRender struct { sse.Event }\nfunc (r testSSEContextRender) Render(ctx context.Context, w http.ResponseWriter) error { return r.Event.Render(w) }\n'
        elif relative.as_posix() == "middleware_test.go":
            old_call = 'c.Render(http.StatusBadRequest, sse.Event{\n\t\t\tEvent: "test",\n\t\t\tData:  "message",\n\t\t})'
            assert text.count(old_call) == 1
            text = text.replace(old_call, 'c.Render(http.StatusBadRequest, testSSEContextRender{sse.Event{\n\t\t\tEvent: "test",\n\t\t\tData:  "message",\n\t\t}})')
        else:
            text, count = re.subn(r"\.Render\(([A-Za-z_]\w*)\)", r".Render(context.Background(), \1)", text)
            if relative.as_posix() == "binding/json_test.go":
                assert count == 1, "binding PureJSON caller changed"
            if relative.as_posix() == "render/reader_test.go":
                old_call = "r.Render(httptest.NewRecorder())"
                assert text.count(old_call) == 1
                text = text.replace(old_call, "r.Render(context.Background(), httptest.NewRecorder())")
                count += 1
            if count:
                text = text.replace("import (\n", 'import (\n\t"context"\n', 1)
        (target / relative).write_text(text)
    if held_out:
        shutil.copy2(HELD_OUT / "render_context_heldout_test.go", target / "render" / "render_context_heldout_test.go")
        shutil.copy2(HELD_OUT / "render_context_msgpack_heldout_test.go", target / "render" / "render_context_msgpack_heldout_test.go")
        shutil.copy2(HELD_OUT / "context_render_heldout_test.go", target / "context_render_heldout_test.go")
    formatted = [target / "context_test.go", target / "middleware_test.go", target / "binding/json_test.go", *sorted((target / "render").glob("*_test.go"))]
    if held_out:
        formatted.append(target / "context_render_heldout_test.go")
    subprocess.run(["gofmt", "-w", *(str(path) for path in formatted)], check=True, capture_output=True, text=True)


class GinRenderContextTask(Task):
    capability = "fix"

    @property
    def name(self) -> str:
        return "gin_edit_render_context"

    @property
    def repo(self) -> str:
        return "gin"

    @property
    def task_type(self) -> str:
        return "edit"

    @property
    def ground_truth(self) -> GroundTruth:
        return GroundTruth()

    @property
    def trusted_reference(self) -> str:
        """The held-out fixture tests, for the applicability judge; never shown to the agent."""
        return "\n".join(f"// {path.name}\n{path.read_text()}" for path in sorted(HELD_OUT.glob("*_heldout_test.go")))

    @property
    def prompt(self) -> str:
        return (
            "Migrate Gin's render.Render interface to Render(context.Context, "
            "http.ResponseWriter) error. This is a forward edit, not a repair: do not "
            "revert the API. Migrate every renderer implementation, including all six "
            "JSON variants, MsgPack, and BSON, plus direct callers and existing tests. "
            "The binding/json_test.go PureJSON caller also needs the new context argument. "
            "The external sse.Event still uses the old Render method. Adapt Gin's "
            "SSEvent path with a private context-aware wrapper that delegates to it; "
            "do not change the dependency or Context.Render's public signature. "
            "At entry, each renderer must return ctx.Err() for a cancelled or expired "
            "context before changing any header or body, reading Reader input, or "
            "executing an HTML template. No in-progress I/O interruption is required. "
            "For active contexts, preserve rendering, custom content types, and errors. "
            "Context.Render must pass c.Request.Context() when Request exists and "
            "context.Background() otherwise. Keep its public signature, status and "
            "body-disallowed behavior, and error recording plus abort behavior. "
            "Keep WriteContentType and exported WriteJSON, WriteString, and similar "
            "helper signatures unchanged. Do not weaken tests. The complete render and "
            "binding suites, relevant root tests, and all-package compilation must pass "
            "under normal and nomsgpack builds. Leave changes uncommitted."
        )

    def check_correctness(self, result_text: str, repo_path: str) -> tuple[bool, str]:
        candidate = Path(repo_path)
        status = _run(["git", "status", "--porcelain", "--untracked-files=all"], candidate)
        if status.returncode:
            return False, "Cannot inspect candidate changes"
        changes = [(line[:2], line[3:]) for line in status.stdout.splitlines()]
        if not changes:
            return False, "No source changes"
        source_changes = set()
        for _, relative in changes:
            path = Path(relative)
            parts = path.parts
            local_go = path.suffix == ".go" and (len(parts) == 1 or len(parts) == 2 and parts[0] == "render")
            original_test = relative in {"context_test.go", "middleware_test.go", "binding/json_test.go"} or (
                len(parts) == 2 and parts[0] == "render" and path.name.endswith("_test.go")
                and (FIXTURE / relative).is_file()
            )
            new_file = not (FIXTURE / relative).exists()
            source = relative in SOURCE_FILES or (new_file and local_go and not path.name.endswith("_test.go"))
            test = original_test or (new_file and local_go and path.name.endswith("_test.go"))
            entry = candidate / relative
            if not (source or test) or entry.is_symlink() or not entry.is_file():
                return False, f"Unsafe changed path: {relative}"
            if source:
                source_changes.add(relative)
        if not source_changes:
            return False, "No renderer or dispatch changes"
        with tempfile.TemporaryDirectory(prefix="gin-render-grade-") as temp:
            target = Path(temp)
            shutil.copytree(FIXTURE, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git"))
            for relative in source_changes:
                shutil.copy2(candidate / relative, target / relative)
            _restore_tests(target)
            for tags in ([], ["-tags=nomsgpack"]):
                for command in _grade_commands(tags):
                    try:
                        result = _run(command, target)
                    except subprocess.TimeoutExpired:
                        return False, f"Trusted tests timed out: {' '.join(command)}"
                    if result.returncode:
                        detail = (result.stderr + result.stdout)[-1200:]
                        return False, f"Trusted tests failed: {' '.join(command)}\n{detail}"
        for tags in ([], ["-tags=nomsgpack"]):
            for command in _grade_commands(tags):
                try:
                    result = _run(command, candidate)
                except subprocess.TimeoutExpired:
                    return False, f"Candidate tests timed out: {' '.join(command)}"
                if result.returncode:
                    detail = (result.stderr + result.stdout)[-1200:]
                    return False, f"Candidate tests failed: {' '.join(command)}\n{detail}"
        return True, "Trusted and candidate tests pass in both build variants"
