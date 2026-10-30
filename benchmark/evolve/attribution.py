"""Deterministic cost attribution: where a candidate's tool-result characters come from, versus baseline.

``summarize`` is pure over rows and their trajectory sidecars. It reads only ``task``, ``contaminated`` and
``trajectory_path`` from a row, never a grader field. Every per-cell figure is task-balanced: the mean over a
task's cells, then the mean over matched tasks, so an arm with more repetitions does not outweigh another.

Two figures are proxies. ``exposure`` is result characters times the API turns that follow the call in its
cell, a context-cost proxy (cache reads re-pay a result every later turn). A sidecar without ``api_turn`` is
re-derived from the raw stream beside its sidecar; when that is missing too the cell's exposure is None and
the cell is left out of the exposure mean (``coverage.exposure_cells`` counts the cells that have one). ``read_breadth`` excess prices each extra
distinct file at the candidate's mean read-result size.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Iterable, Mapping

from parse import extract_trajectory, output_chars

from .candidate import PREAMBLE

VERSION = 1
ARMS = ("candidate", "baseline")
BASE_EDIT = ("Edit", "Write", "MultiEdit")
BASE_SEARCH = ("Grep", "Glob")
EXAMPLE_CHARS = 160
MAX_DOMINANT = 5

# Checked in order; the first match wins.
ERROR_CLASSES = (
    ("never_displayed", re.compile(r"never displayed|was not displayed")),
    ("create_file_exists", re.compile(r"create_file (?:target already exists|requires a tagless)")),
    ("text_not_found", re.compile(r"text to replace was not found|String to replace not found")),
    ("input_validation", re.compile(r"InputValidationError|could not be parsed|edits\[\d+\]")),
    ("tag_missing_or_stale", re.compile(r"needs a tag|requires the \[path#TAG\]|stale|drifted|fabricated")),
)
HINTS = {
    "tilth_write_receipt": "src_patch", "tilth_read_full_mode": "prompts/tools/read.md",
    "tilth_read_batched_4plus": "prompts/tools/read.md", "search_output": "src_patch",
    "write_errors": "prompts/tools/write.md", "read_breadth": "prompts/mcp.md",
}
# In a --preamble run the preamble asks the agent to read callers and helpers before an edit, so it steers breadth.
PREAMBLE_HINTS = {**HINTS, "read_breadth": PREAMBLE}
_SECTION_ERROR = re.compile(r"^error:", re.M)
_BENCH_ROOT = re.compile(r"^/tmp/tilth-benchmark-[^/]+/repo/")


def _text(output: object) -> str:
    if isinstance(output, dict) and isinstance(output.get("content"), list):
        output = output["content"]
    if isinstance(output, list):
        return "".join(b["text"] for b in output if isinstance(b, dict) and isinstance(b.get("text"), str))
    return output if isinstance(output, str) else json.dumps(output, default=str)


def _classify(text: str) -> str:
    return next((name for name, pattern in ERROR_CLASSES if pattern.search(text)), "other")


def _norm(path: object, cwd: object = None) -> str:
    path = str(path).split("#", 1)[0]
    if isinstance(cwd, str) and path.startswith(cwd.rstrip("/") + "/"):
        path = path[len(cwd.rstrip("/")) + 1:]
    return _BENCH_ROOT.sub("", path).removeprefix("./")


def _stream_turns(path: str) -> dict[str, int]:
    """``api_turn`` by tool_use id, re-parsed from the raw stream beside a ``.trajectory.jsonl`` sidecar."""
    stream = Path(path.removesuffix(".trajectory.jsonl") + ".jsonl")
    if path == str(stream) or not stream.is_file():
        return {}
    return {c["tool_use_id"]: c["api_turn"] for c in extract_trajectory(stream.read_text(), "claude")
            if isinstance(c.get("api_turn"), int)}


def _load(path: object) -> tuple[list[dict], bool] | None:
    """A cell's calls and whether their turns came from the raw stream; None when there is no sidecar."""
    if not isinstance(path, str) or not Path(path).is_file():
        return None
    calls = []
    for line in Path(path).read_text().splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and isinstance(record.get("name"), str):
            calls.append(record)
    if all(isinstance(c.get("api_turn"), int) for c in calls):
        return calls, False
    turns = _stream_turns(path)
    if not all(c.get("tool_use_id") in turns for c in calls):
        return calls, False
    return [{**c, "api_turn": turns[c["tool_use_id"]]} for c in calls], True


def _shapes(tool: str, args: Mapping) -> list[str]:
    if tool == "Read":
        return ["window" if any(args.get(k) is not None for k in ("offset", "limit", "start", "start_line")) else "whole"]
    if tool != "tilth_read":
        return []
    paths = args.get("paths") if isinstance(args.get("paths"), list) else [args.get("paths")]
    shapes = [name for name, hit in (("requested_mode_full", args.get("mode") == "full"),
                                     ("batched_4plus", len(paths) >= 4),
                                     ("section", any("#" in str(p) for p in paths))) if hit]
    return shapes or ["whole_default"]


def _reads(tool: str, args: Mapping) -> list[str]:
    if tool == "tilth_read":
        paths = args.get("paths")
        return [_norm(p, args.get("cwd")) for p in (paths if isinstance(paths, list) else [paths]) if p]
    if tool == "Read":
        return [_norm(p) for p in (args.get("file_path") or args.get("path"),) if p]
    return []


def _edits(tool: str, args: Mapping) -> list[str]:
    if tool in ("Edit", "MultiEdit") and args.get("file_path"):
        return [_norm(args["file_path"])]
    if tool != "tilth_write" or not isinstance(args.get("edits"), list):
        return []
    return [_norm(e["path"], args.get("cwd")) for e in args["edits"] if isinstance(e, dict) and e.get("path")
            and not any(isinstance(o, dict) and o.get("op") == "create_file" for o in e.get("ops") or [])]


def _cell_stats(cell: tuple[list[dict], bool], arm: str, examples: dict) -> Counter:
    """One cell's additive figures, keyed by tuples such as ``("chars", tool)``.

    ``("exposure",)`` is set only when every call has a real API turn; otherwise it is absent, never guessed.
    """
    calls, from_stream = cell
    turns = [c.get("api_turn") for c in calls]
    known = all(isinstance(t, int) for t in turns)
    last = max((t for t in turns if known), default=-1)
    out: Counter = Counter()
    if known:
        out[("exposure",)] = 0
        out[("stream_cells",)] = int(from_stream)
    read: set[str] = set()
    unread: set[str] = set()
    for call, turn in zip(calls, turns):
        tool = str(call["name"]).removeprefix("mcp__tilth__")
        args = call.get("input") if isinstance(call.get("input"), dict) else {}
        text = _text(call.get("output"))
        chars = call["result_chars"] if isinstance(call.get("result_chars"), int) else output_chars(call.get("output"))
        failed = call.get("is_error") is True or (tool == "tilth_write" and bool(_SECTION_ERROR.search(text)))
        out[("calls", tool)] += 1
        out[("chars", tool)] += chars
        if known:
            out[("exposure",)] += chars * (last + 1 - turn)
        if failed:
            kind = _classify(text)
            out[("err", tool)] += 1
            out[("errchars", tool)] += chars
            out[("cls", tool, kind)] += 1
            examples.setdefault((arm, tool, kind), text.strip()[:EXAMPLE_CHARS])
        for shape in _shapes(tool, args):
            out[("shape_calls", tool, shape)] += 1
            out[("shape_chars", tool, shape)] += chars
        read.update(_reads(tool, args))
        unread.update(p for p in _edits(tool, args) if p not in read)
    out[("files",)] = len(read)
    out[("unread_edits",)] = len(unread)
    return out


def _balanced(cells_by_task: Mapping[str, list[Counter]]) -> dict:
    totals: Counter = Counter()
    for cells in cells_by_task.values():
        task_sum: Counter = Counter()
        for cell in cells:
            task_sum.update(cell)
        for key, value in task_sum.items():
            totals[key] += value / len(cells)
    return {key: value / (len(cells_by_task) or 1) for key, value in totals.items()}


def _gather(rows: Iterable[Mapping], tasks: set[str]) -> tuple[dict[str, list[tuple[list[dict], bool]]], int]:
    cells: dict[str, list[tuple[list[dict], bool]]] = {}
    missing = 0
    for row in rows:
        if row.get("contaminated") is True or row.get("task") not in tasks:
            continue
        cell = _load(row.get("trajectory_path"))
        if cell is None:
            missing += 1
        else:
            cells.setdefault(row["task"], []).append(cell)
    return cells, missing


def _dominant(cand: dict, base: dict, hints: Mapping[str, str]) -> list[dict]:
    g = lambda stats, *key: stats.get(key, 0.0)  # noqa: E731
    base_write = sum(g(base, "chars", t) - g(base, "errchars", t) for t in BASE_EDIT)
    base_write_err = sum(g(base, "errchars", t) for t in BASE_EDIT)
    read_total = g(cand, "chars", "tilth_read")
    base_read = g(base, "chars", "Read")
    read_mean = read_total / g(cand, "calls", "tilth_read") if g(cand, "calls", "tilth_read") else 0.0
    found = [
        ("tilth_write_receipt", g(cand, "chars", "tilth_write") - g(cand, "errchars", "tilth_write") - base_write,
         f"tilth_write ok results {g(cand, 'calls', 'tilth_write'):.1f} calls/cell vs built-in edits "
         f"{sum(g(base, 'calls', t) for t in BASE_EDIT):.1f}"),
        ("write_errors", g(cand, "errchars", "tilth_write") - base_write_err,
         f"tilth_write error rate {g(cand, 'err', 'tilth_write') / (g(cand, 'calls', 'tilth_write') or 1):.0%}"),
        ("search_output", g(cand, "chars", "tilth_search") - sum(g(base, "chars", t) for t in BASE_SEARCH),
         f"tilth_search {g(cand, 'calls', 'tilth_search'):.1f} calls/cell vs Grep+Glob "
         f"{sum(g(base, 'calls', t) for t in BASE_SEARCH):.1f}"),
        ("read_breadth", (g(cand, "files") - g(base, "files")) * read_mean,
         f"{g(cand, 'files'):.1f} vs {g(base, 'files'):.1f} distinct files read per cell at {read_mean:.0f} chars/read"),
    ]
    for shape in ("requested_mode_full", "batched_4plus"):
        shape_chars = g(cand, "shape_chars", "tilth_read", shape)
        share = shape_chars / read_total if read_total else 0.0
        cause = "tilth_read_" + ("full_mode" if shape == "requested_mode_full" else "batched_4plus")
        found.append((cause, shape_chars - base_read * share,
                      f"{shape} is {g(cand, 'shape_calls', 'tilth_read', shape) / (g(cand, 'calls', 'tilth_read') or 1):.0%}"
                      f" of reads and {share:.0%} of read chars"))
    ranked = sorted((f for f in found if f[1] > 0), key=lambda f: -f[1])[:MAX_DOMINANT]
    return [{"cause": c, "excess_chars_per_cell": round(x, 1), "evidence": e, "component_hint": hints[c]}
            for c, x, e in ranked]


def summarize(candidate_rows: Iterable[Mapping], baseline_rows: Iterable[Mapping], tasks: Iterable[str], *,
              hints: Mapping[str, str] = HINTS) -> dict:
    """The attribution of ``tasks``; ``hints`` maps each dominant cause to the component it names."""
    wanted = set(tasks)
    gathered = {arm: _gather(rows, wanted) for arm, rows in zip(ARMS, (candidate_rows, baseline_rows))}
    matched = sorted(set(gathered["candidate"][0]) & set(gathered["baseline"][0]))
    examples: dict = {}
    cells = {arm: {t: [_cell_stats(c, arm, examples) for c in gathered[arm][0][t]] for t in matched} for arm in ARMS}
    stats = {arm: _balanced(cells[arm]) for arm in ARMS}
    with_turns = {arm: {t: [c for c in cs if ("exposure",) in c] for t, cs in cells[arm].items()} for arm in ARMS}
    exposure = {arm: _balanced({t: cs for t, cs in with_turns[arm].items() if cs}).get(("exposure",)) for arm in ARMS}
    counts = {name: {arm: sum(c.get(key, 0) for cs in cells[arm].values() for c in cs) for arm in ARMS}
              for name, key in (("exposure_cells", ("exposure",)), ("stream_turn_cells", ("stream_cells",)))}
    counts["exposure_cells"] = {arm: sum(len(cs) for cs in with_turns[arm].values()) for arm in ARMS}
    per_tool, call_shapes, errors, seen_shapes = [], [], [], set()
    for arm in ARMS:
        s = stats[arm]
        for tool in sorted({k[1] for k in s if k[0] == "calls"}):
            calls, chars = s[("calls", tool)], s.get(("chars", tool), 0.0)
            per_tool.append({"tool": tool, "arm": arm, "calls_per_cell": calls, "result_chars_per_cell": chars,
                             "mean_result_chars": chars / calls, "error_rate": s.get(("err", tool), 0.0) / calls})
        for key in sorted(k for k in s if k[0] == "shape_calls"):
            _, tool, shape = key
            if (tool, shape) in seen_shapes:
                continue
            seen_shapes.add((tool, shape))
            chars = s.get(("shape_chars", tool, shape), 0.0)
            call_shapes.append({"tool": tool, "shape": shape, "share_of_calls": s[key] / s[("calls", tool)],
                                "share_of_result_chars": chars / s[("chars", tool)] if s.get(("chars", tool)) else 0.0,
                                "mean_result_chars": chars / s[key]})
        errors += [{"arm": arm, "tool": k[1], "class": k[2], "per_cell": v, "example": examples.get((arm, k[1], k[2]), "")}
                   for k, v in s.items() if k[0] == "cls"]
    errors.sort(key=lambda e: (-e["per_cell"], e["arm"], e["tool"], e["class"]))
    return {
        "version": VERSION,
        "coverage": {"candidate_cells": sum(len(v) for v in cells["candidate"].values()),
                     "baseline_cells": sum(len(v) for v in cells["baseline"].values()),
                     "matched_tasks": len(matched),
                     "missing_trajectories": gathered["candidate"][1] + gathered["baseline"][1],
                     "exposure_cells": counts["exposure_cells"], "stream_turn_cells": counts["stream_turn_cells"]},
        "per_tool": per_tool,
        "call_shapes": call_shapes,
        "errors": errors,
        "breadth": {arm: {"distinct_files_read_per_cell": stats[arm].get(("files",), 0.0),
                          "edits_on_unread_files_per_cell": stats[arm].get(("unread_edits",), 0.0)} for arm in ARMS},
        "exposure": {arm: {"result_chars_times_remaining_api_turns_per_cell": exposure[arm]}
                     for arm in ARMS},
        "dominant": _dominant(stats["candidate"], stats["baseline"], hints),
    }
