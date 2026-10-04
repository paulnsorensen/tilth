"""Result store and run-key identity for benchmark cells.

A run key hashes everything that can change a cell's outcome: the cell's own
coordinates (task, model, agent CLI version, effort, timeout, arm, repetition), the
harness digest, the task digest, the environment fingerprint, and, for tilth
arms, the candidate's git SHA and binary SHA-256. The result store keeps every
row it is given, keyed by run key, but only a completed row is reusable.
"""

import hashlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from parse import tolerant_jsonl

STORE_FILENAME = "result_store.jsonl"

# Row fields that make up a run key besides the three digests.
CELL_KEY_FIELDS = (
    "task", "model", "cli_version", "reasoning_effort", "timeout_s", "mode",
    "repetition", "git_sha", "binary_sha256",
)
DIGEST_FIELDS = ("harness_digest", "task_digest", "env_fingerprint")

# Coordinates under which two stock-arm rows describe the same frozen cell. The
# harness digest is part of the slot: a --bare or strict-file-tools baseline is a
# different harness, not a drifted copy of the default one.
BASELINE_SLOT_FIELDS = (
    "task", "model", "mode", "reasoning_effort", "timeout_s", "repetition", "harness_digest",
)


def _digest(payload: object) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def harness_digest(
    *,
    system_prompt: str,
    tools: list[str],
    strict_file_tools: bool,
    bare: bool,
    max_budget_usd: float | None,
    mcp_shape: Mapping[str, Any],
    command: list[str] = (),
    bash_guard_sha256: str | None = None,
) -> str:
    """Hash the runner configuration a cell's agent sees.

    ``command`` is the runner argv template with per-cell paths normalized; it
    carries codex developer instructions, ``--disallowedTools``, the strict Bash
    hook settings, and ``--setting-sources``. ``bash_guard_sha256`` hashes the
    strict-mode Bash allowlist source, which the settings only name by path.
    """
    return _digest({
        "system_prompt": system_prompt, "tools": tools, "strict_file_tools": strict_file_tools,
        "bare": bare, "max_budget_usd": max_budget_usd, "mcp_shape": mcp_shape,
        "command": list(command), "bash_guard_sha256": bash_guard_sha256,
    })


def task_digest(
    *,
    prompt: str,
    ground_truth: object,
    test_command: list[str],
    fixture_files: Mapping[str, str],
    repo_commit: str | None,
    mutations: list[object] = (),
    hide_git: bool = False,
    task_sources: Mapping[str, str] | None = None,
) -> str:
    """Hash what the task asks, how it is graded, and the code it runs against.

    ``task_sources`` maps each source file defining the task class or its bases to
    its hash, so an edit to grading code such as a ``check_correctness`` override
    yields a new digest.
    """
    return _digest({
        "prompt": prompt, "ground_truth": ground_truth, "test_command": test_command,
        "fixture_files": fixture_files, "repo_commit": repo_commit,
        "mutations": list(mutations), "hide_git": hide_git,
        "task_sources": dict(task_sources or {}),
    })


def env_fingerprint(*, toolchains: Mapping[str, str | None], lockfile_hash: str) -> str:
    """Hash toolchain versions and the dependency lockfiles a cell builds against."""
    return _digest({"toolchains": toolchains, "lockfile_hash": lockfile_hash})


def run_key(cell: Mapping[str, Any]) -> str:
    """Hash a cell's key inputs; unrelated row fields are ignored."""
    return _digest({field: cell.get(field) for field in (*CELL_KEY_FIELDS, *DIGEST_FIELDS)})


def is_completed(row: Mapping[str, Any]) -> bool:
    """A completed row has no error, no infra failure, and no timeout."""
    return not row.get("error") and row.get("infra") is None and not row.get("timed_out")


def load_rows(path: Path) -> list[dict]:
    """Return every stored row, oldest first, skipping a line a killed run left torn."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return []
    rows = tolerant_jsonl(text)
    skipped = sum(1 for line in text.splitlines() if line.strip()) - len(rows)
    if skipped:
        print(f"warning: skipped {skipped} malformed line(s) in {path}", file=sys.stderr)
    return rows


def completed_by_key(rows: list[dict]) -> dict[str, dict]:
    """Map each run key to its newest completed row."""
    return {row["run_key"]: row for row in rows if row.get("run_key") and is_completed(row)}


def lookup(key: str, *, path: Path) -> dict | None:
    """Return the newest completed row stored under ``key``."""
    return completed_by_key(load_rows(path)).get(key)


def store(row: Mapping[str, Any], *, path: Path) -> None:
    """Append one row to the store."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row) + "\n"
    with path.open("a+b") as store_file:
        # Start a fresh line after a torn tail so the new row is not glued onto it.
        if store_file.seek(0, 2):
            store_file.seek(-1, 2)
            if store_file.read(1) != b"\n":
                line = "\n" + line
        store_file.write(line.encode())


def drifted_inputs(planned: Mapping[str, Any], stored: Mapping[str, Any]) -> list[str]:
    """Name the key inputs that differ between a planned cell and a stored row."""
    return [
        field for field in (*CELL_KEY_FIELDS, *DIGEST_FIELDS)
        if planned.get(field) != stored.get(field)
    ]


def baseline_drift(planned: Mapping[str, Any], rows: list[dict]) -> list[str]:
    """Name the inputs that changed since a completed stock-arm row for the same slot.

    ``planned`` is a stock-arm cell (no tilth MCP) with no completed row under its
    own key. A stored completed row for the same slot (``BASELINE_SLOT_FIELDS``)
    under another key means the frozen baseline drifted.
    """
    changed: set[str] = set()
    for row in rows:
        if (is_completed(row)
                and row.get("run_key") != planned.get("run_key")
                and all(row.get(field) == planned.get(field) for field in BASELINE_SLOT_FIELDS)):
            changed.update(drifted_inputs(planned, row) or ["run_key"])
    return sorted(changed)
