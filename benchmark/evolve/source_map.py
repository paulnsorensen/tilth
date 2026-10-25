"""Map attribution causes to the Rust source that renders, routes, validates, or records that output.

The keys are the ``cause`` values ``attribution.summarize`` puts in ``dominant``, plus ``schemas``
(tool-definition tokens are not visible in a trajectory, so no trajectory cause maps to it).
Every ``symbol`` is a Rust ``fn`` name; a test checks the path exists and defines it.
"""
from __future__ import annotations

from typing import Iterable


def _entry(path: str, symbol: str, role: str) -> dict:
    return {"path": path, "symbol": symbol, "role": role}


SOURCE_MAP: dict[str, list[dict]] = {
    "tilth_write_receipt": [
        _entry("src/mcp/tools/write.rs", "commit_section", "commits one edit section and builds its receipt"),
        _entry("src/mcp/tools/write.rs", "render_changed_windows", "renders the changed numbered lines in the receipt"),
        _entry("src/mcp/tools/write.rs", "resolve_edit", "resolves tag and ops into new text"),
    ],
    "tilth_read_full_mode": [
        _entry("src/read/mod.rs", "read_file", "chooses full, outline, or section view for one file"),
        _entry("src/mcp/tools/read.rs", "tool_read_paths", "applies mode and budget across the paths"),
    ],
    "tilth_read_batched_4plus": [
        _entry("src/mcp/tools/read.rs", "tool_read_paths", "renders and budgets a multi-path batch"),
        _entry("src/mcp/tools/read.rs", "batch_part_caps", "splits the response budget across batch parts"),
    ],
    "search_output": [
        _entry("src/mcp/tools/search_v2.rs", "tool_search_v2", "entry point that routes and renders search results"),
        _entry("src/mcp/tools/search_v2.rs", "reduce_response", "trims the response to budget"),
        _entry("src/search/mod.rs", "search", "search orchestration"),
    ],
    "write_errors": [
        _entry("src/edit/snapshots.rs", "record", "records seen lines per tag for the seen-lines gate"),
        _entry("src/mcp/tools/write.rs", "resolve_edit", "raises tag-missing, stale, and never-displayed rejections"),
        _entry("src/mcp/tools/write.rs", "commit_section", "raises create_file and not-found rejections"),
    ],
    "read_breadth": [
        _entry("src/mcp/tools/read.rs", "tool_read", "read entry point whose outputs steer which files get read next"),
        _entry("src/mcp/tools/search_v2.rs", "tool_search_v2", "search output that points to files"),
    ],
    "schemas": [
        _entry("src/mcp/tools/definitions.rs", "tool_definitions", "tool schemas sent on every turn"),
    ],
}


def entries_for(causes: Iterable[str]) -> list[dict]:
    """Source entries for ``causes`` in order, de-duplicated; unknown causes contribute nothing."""
    seen: set[tuple[str, str]] = set()
    entries: list[dict] = []
    for cause in causes:
        for entry in SOURCE_MAP.get(cause, []):
            key = (entry["path"], entry["symbol"])
            if key not in seen:
                seen.add(key)
                entries.append(dict(entry))
    return entries
