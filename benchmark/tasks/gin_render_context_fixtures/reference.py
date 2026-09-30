"""Trusted reference transformation for the pinned Gin fixture."""

import re
import subprocess
from pathlib import Path

from ..gin_render_context_tasks import SOURCE_FILES, _restore_tests


CHECK = "\tif err := ctx.Err(); err != nil {\n\t\treturn err\n\t}\n"


def apply_reference(target: Path) -> None:
    for relative in sorted(SOURCE_FILES - {"context.go"}):
        path = target / relative
        text = path.read_text()
        if relative == "render/render.go":
            assert text.count("Render(http.ResponseWriter) error") == 1
            text = text.replace("Render(http.ResponseWriter) error", "Render(context.Context, http.ResponseWriter) error")
        else:
            text, count = re.subn(
                r"(func \(r \w+\) Render)\(w http.ResponseWriter\)([^\n]*\{\n)",
                lambda m: m.group(1) + "(ctx context.Context, w http.ResponseWriter)" + m.group(2) + CHECK,
                text,
            )
            assert count >= 1, relative
        if 'import "net/http"' in text:
            text = text.replace('import "net/http"', 'import (\n\t"context"\n\t"net/http"\n)')
        else:
            assert "import (\n" in text, relative
            text = text.replace("import (\n", 'import (\n\t"context"\n', 1)
        path.write_text(text)
    path = target / "context.go"
    text = path.read_text()
    assert text.count("r.Render(c.Writer)") == 1
    text = text.replace("import (\n", 'import (\n\t"context"\n', 1)
    text = text.replace(
        "if err := r.Render(c.Writer); err != nil {",
        "renderContext := context.Background()\n\tif c.Request != nil {\n\t\trenderContext = c.Request.Context()\n\t}\n\tif err := r.Render(renderContext, c.Writer); err != nil {",
    )
    old = 'c.Render(-1, sse.Event{\n\t\tEvent: name,\n\t\tData:  message,\n\t})'
    assert text.count(old) == 1
    text = text.replace(old, 'c.Render(-1, sseContextRender{sse.Event{\n\t\tEvent: name,\n\t\tData:  message,\n\t}})')
    text += '\ntype sseContextRender struct { sse.Event }\nfunc (r sseContextRender) Render(ctx context.Context, w http.ResponseWriter) error {\n\tif err := ctx.Err(); err != nil { return err }\n\treturn r.Event.Render(w)\n}\n'
    path.write_text(text)
    subprocess.run(["gofmt", "-w", *(str(target / path) for path in SOURCE_FILES)], check=True, capture_output=True, text=True)
    _restore_tests(target, held_out=False)
