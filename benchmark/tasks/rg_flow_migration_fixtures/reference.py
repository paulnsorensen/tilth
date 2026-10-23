"""Apply the reference solution to a clone of the pinned ripgrep fixture."""

import subprocess
from pathlib import Path

PATCH = Path(__file__).parent / "reference.patch"
# Reverting these files to the fixture leaves a migration incomplete.
PRINTER_FILES = (
    "crates/printer/src/json.rs",
    "crates/printer/src/standard.rs",
    "crates/printer/src/summary.rs",
    "crates/printer/src/util.rs",
)
MATCHER_IMPL_FILES = ("crates/regex/src/matcher.rs", "crates/pcre2/src/matcher.rs")
EXAMPLE_FILE = "crates/searcher/examples/search-stdin.rs"
# Behavior-breaking edits: each text occurs once in the reference and swallows a Stop.
QUIET_STOP = ("crates/printer/src/summary.rs", "            return Ok(Flow::Stop);", "            return Ok(Flow::Continue);")
SEARCHER_STOP = (
    "crates/searcher/src/searcher/core.rs",
    "            } else if self.sink_after_context(buf, &line)? == Flow::Stop {\n                return Ok(Flow::Stop);",
    "            } else if self.sink_after_context(buf, &line)? == Flow::Stop {\n                return Ok(Flow::Continue);",
)
FIND_ITER_STOP = (
    "crates/matcher/src/lib.rs",
    "                Ok(Flow::Stop) => return Ok(Ok(())),",
    "                Ok(Flow::Stop) => continue,",
)


def apply_reference(target: Path) -> None:
    subprocess.run(["git", "apply", str(PATCH)], cwd=target, check=True, capture_output=True, text=True)


def break_behavior(target: Path, edit: tuple[str, str, str]) -> None:
    relative, old, new = edit
    path = target / relative
    text = path.read_text()
    assert old in text, f"{relative} no longer contains the control site"
    path.write_text(text.replace(old, new, 1))
