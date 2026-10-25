"""Evolve cost attribution and the tool-to-source map, over small synthetic trajectories."""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from evolve import attribution, source_map
from evolve.attribution import summarize

REPO = Path(__file__).resolve().parents[2]
TASK = "t1"
TAG_ERROR = "## z.py\nerror: replace_text/rewrite needs a tag"


def sized(text: str, n: int) -> str:
    return text.ljust(n, ".")


def call(name, args, out, turn=None, is_error=False):
    record = {"tool_use_id": f"id-{name}", "name": name, "input": args,
              "output": [{"type": "text", "text": out}] if name.startswith("mcp__") else out,
              "is_error": is_error}
    if turn is not None:
        record["api_turn"] = turn
    return record


def write_cell(tmp_path: Path, name: str, calls: list[dict], **row) -> dict:
    path = tmp_path / f"{name}.trajectory.jsonl"
    path.write_text("\n".join(json.dumps(c) for c in calls))
    return {"task": TASK, "contaminated": False, "trajectory_path": str(path), **row}


def candidate_calls() -> list[dict]:
    cwd = "/tmp/tilth-benchmark-x/repo"
    four = ["a.py", "b.py", "c.py", "d.py"]
    return [
        call("mcp__tilth__tilth_read", {"paths": ["a.py#1-5"], "cwd": cwd}, sized("r", 100), 0),
        call("mcp__tilth__tilth_read", {"paths": four, "mode": "full", "cwd": cwd}, sized("r", 400), 1),
        call("mcp__tilth__tilth_write", {"edits": [{"path": "a.py", "ops": [{"op": "replace_text"}]}], "cwd": cwd},
             sized("## a.py\napplied", 300), 2),
        call("mcp__tilth__tilth_write", {"edits": [{"path": "z.py", "ops": [{"op": "replace_text"}]}], "cwd": cwd},
             sized(TAG_ERROR, 50), 3),
        call("mcp__tilth__tilth_search", {"queries": [{"query": "x"}], "cwd": cwd}, sized("s", 200), 3),
    ]


def baseline_calls() -> list[dict]:
    return [
        call("Read", {"file_path": "/tmp/tilth-benchmark-y/repo/a.py", "offset": 1, "limit": 5}, sized("r", 50), 0),
        call("Read", {"file_path": "/tmp/tilth-benchmark-y/repo/b.py"}, sized("r", 150), 1),
        call("Edit", {"file_path": "/tmp/tilth-benchmark-y/repo/c.py"}, sized("ok", 20), 2),
        call("Grep", {"pattern": "x"}, sized("g", 30), 3),
    ]


@pytest.fixture
def summary(tmp_path) -> dict:
    cand = [write_cell(tmp_path, "c0", candidate_calls())]
    base = [write_cell(tmp_path, "b0", baseline_calls())]
    return summarize(cand, base, [TASK])


def by(items: list[dict], **keys) -> dict:
    (match,) = [i for i in items if all(i[k] == v for k, v in keys.items())]
    return match


def test_per_tool_sums_means_and_error_rate(summary) -> None:
    read = by(summary["per_tool"], tool="tilth_read", arm="candidate")
    assert (read["calls_per_cell"], read["result_chars_per_cell"], read["mean_result_chars"]) == (2, 500, 250)
    write = by(summary["per_tool"], tool="tilth_write", arm="candidate")
    assert (write["calls_per_cell"], write["result_chars_per_cell"], write["error_rate"]) == (2, 350, 0.5)
    assert by(summary["per_tool"], tool="Edit", arm="baseline")["mean_result_chars"] == 20
    assert summary["version"] == 1


def test_call_shapes_record_mode_and_batch_size_separately(summary) -> None:
    shapes = summary["call_shapes"]
    full = by(shapes, tool="tilth_read", shape="requested_mode_full")
    batched = by(shapes, tool="tilth_read", shape="batched_4plus")
    assert (full["share_of_calls"], full["share_of_result_chars"], full["mean_result_chars"]) == (0.5, 0.8, 400)
    assert batched == {**full, "shape": "batched_4plus"}
    assert by(shapes, tool="tilth_read", shape="section")["share_of_result_chars"] == pytest.approx(0.2)
    assert not [s for s in shapes if s["shape"] == "whole_default"]
    assert by(shapes, tool="Read", shape="window")["share_of_calls"] == 0.5
    assert by(shapes, tool="Read", shape="whole")["mean_result_chars"] == 150


def test_whole_default_is_a_plain_unbatched_read(tmp_path) -> None:
    plain = call("mcp__tilth__tilth_read", {"paths": ["a.py", "b.py"], "cwd": "/r"}, "x" * 10, 0)
    cells = [write_cell(tmp_path, "c", [plain])]
    shapes = summarize(cells, [write_cell(tmp_path, "b", baseline_calls())], [TASK])["call_shapes"]
    assert by(shapes, tool="tilth_read", shape="whole_default")["share_of_calls"] == 1


@pytest.mark.parametrize("text,expected", [
    ("error: replace_text/rewrite needs a tag; retry", "tag_missing_or_stale"),
    ("error: Edit rejected for a.py: line 4 was never displayed under this tag", "never_displayed"),
    ("error: create_file target already exists: /x — use a tagged read", "create_file_exists"),
    ("<tool_use_error>InputValidationError: tilth_write was called", "input_validation"),
    ("error: text to replace was not found; copy old verbatim", "text_not_found"),
    ("error: seaborn: Permission denied", "other"),
])
def test_error_classes(tmp_path, text, expected) -> None:
    failing = call("mcp__tilth__tilth_write", {"edits": []}, "## f\n" + text, 0, is_error=expected == "input_validation")
    result = summarize([write_cell(tmp_path, "c", [failing])], [write_cell(tmp_path, "b", baseline_calls())], [TASK])
    (error,) = result["errors"]
    assert (error["arm"], error["tool"], error["class"], error["per_cell"]) == ("candidate", "tilth_write", expected, 1)
    assert error["example"] == ("## f\n" + text)[:160]


def test_error_example_is_capped_at_160_chars(summary) -> None:
    assert len(by(summary["errors"], **{"class": "tag_missing_or_stale"})["example"]) <= 160


def test_breadth_counts_distinct_files_and_edits_on_unread_files(summary) -> None:
    assert summary["breadth"]["candidate"] == {"distinct_files_read_per_cell": 4, "edits_on_unread_files_per_cell": 1}
    assert summary["breadth"]["baseline"] == {"distinct_files_read_per_cell": 2, "edits_on_unread_files_per_cell": 1}


def test_exposure_weights_chars_by_later_api_turns(summary) -> None:
    key = "result_chars_times_remaining_api_turns_per_cell"
    assert summary["exposure"]["candidate"][key] == 100 * 4 + 400 * 3 + 300 * 2 + 50 + 200
    assert summary["exposure"]["baseline"][key] == 50 * 4 + 150 * 3 + 20 * 2 + 30


def stream_event(kind: str, msg_id: str | None, blocks: list[dict]) -> str:
    return json.dumps({"type": kind, "message": {"id": msg_id, "role": kind, "content": blocks}})


def write_stream(tmp_path: Path, name: str) -> None:
    """Raw stream beside ``name.trajectory.jsonl``: turn 0 has two parallel calls, turn 1 has one."""
    use = lambda i: {"type": "tool_use", "id": i, "name": "Read", "input": {}}
    result = lambda i, n: {"type": "tool_result", "tool_use_id": i, "content": "r" * n}
    lines = [stream_event("assistant", "m0", [use("u1"), use("u2")]),
             stream_event("user", None, [result("u1", 10), result("u2", 20)]),
             stream_event("assistant", "m1", [use("u3")]),
             stream_event("user", None, [result("u3", 30)])]
    (tmp_path / f"{name}.jsonl").write_text("\n".join(lines))


def bare_calls() -> list[dict]:
    return [{"tool_use_id": i, "name": "Read", "input": {}, "output": "r" * n, "is_error": False}
            for i, n in (("u1", 10), ("u2", 20), ("u3", 30))]


def test_exposure_without_api_turn_is_derived_from_the_adjacent_stream(tmp_path) -> None:
    write_stream(tmp_path, "c")
    write_stream(tmp_path, "b")
    rows = [write_cell(tmp_path, "c", bare_calls())], [write_cell(tmp_path, "b", bare_calls())]
    result = summarize(*rows, [TASK])
    assert result["exposure"]["candidate"]["result_chars_times_remaining_api_turns_per_cell"] == (10 + 20) * 2 + 30
    assert result["coverage"]["exposure_cells"] == {"candidate": 1, "baseline": 1}
    assert result["coverage"]["stream_turn_cells"] == {"candidate": 1, "baseline": 1}


def test_exposure_is_null_when_neither_sidecar_nor_stream_gives_turns(tmp_path) -> None:
    rows = [write_cell(tmp_path, "c", bare_calls())], [write_cell(tmp_path, "b", bare_calls())]
    result = summarize(*rows, [TASK])
    key = "result_chars_times_remaining_api_turns_per_cell"
    assert result["exposure"] == {"candidate": {key: None}, "baseline": {key: None}}
    assert result["coverage"]["exposure_cells"] == {"candidate": 0, "baseline": 0}
    assert result["coverage"]["stream_turn_cells"] == {"candidate": 0, "baseline": 0}


def test_dominant_is_ranked_by_candidate_minus_baseline_excess(summary) -> None:
    dominant = summary["dominant"]
    assert [d["cause"] for d in dominant] == [
        "read_breadth", "tilth_write_receipt", "tilth_read_full_mode", "tilth_read_batched_4plus", "search_output"]
    assert [d["excess_chars_per_cell"] for d in dominant] == [500, 280, 240, 240, 170]
    assert [d["component_hint"] for d in dominant][:2] == ["prompts/mcp.md", "src_patch"]
    assert all(d["evidence"] for d in dominant)
    assert {d["cause"] for d in dominant} <= set(source_map.SOURCE_MAP)


def test_no_excess_means_no_dominant_cause(tmp_path) -> None:
    cells = [write_cell(tmp_path, "c", baseline_calls())]
    assert summarize(cells, [write_cell(tmp_path, "b", baseline_calls())], [TASK])["dominant"] == []


class Guarded(dict):
    """A row that fails the test when a grader field is read."""

    GRADER = {"result_text", "correctness_reason", "correct", "pass_rate"}

    def get(self, key, default=None):
        assert key not in self.GRADER, key
        return super().get(key, default)

    def __getitem__(self, key):
        assert key not in self.GRADER, key
        return super().__getitem__(key)


def test_grader_fields_contamination_and_other_tasks_are_excluded(tmp_path) -> None:
    grader = {"result_text": "secret", "correctness_reason": "r", "correct": True, "pass_rate": 1.0}
    good = Guarded(write_cell(tmp_path, "c0", candidate_calls(), **grader))
    huge = [call("mcp__tilth__tilth_search", {}, "x" * 99999, 0)]
    tainted = write_cell(tmp_path, "c1", huge, contaminated=True)
    other = write_cell(tmp_path, "c2", huge, task="elsewhere")
    result = summarize([good, tainted, other], [Guarded(write_cell(tmp_path, "b0", baseline_calls(), **grader))], [TASK])
    assert {k: v for k, v in result["coverage"].items() if not k.endswith("_turn_cells") and k != "exposure_cells"} == {
        "candidate_cells": 1, "baseline_cells": 1, "matched_tasks": 1, "missing_trajectories": 0}
    assert result["coverage"]["exposure_cells"] == {"candidate": 1, "baseline": 1}
    assert by(result["per_tool"], tool="tilth_search", arm="candidate")["result_chars_per_cell"] == 200


def test_missing_trajectory_is_counted_and_skipped(tmp_path) -> None:
    gone = {"task": TASK, "contaminated": False, "trajectory_path": str(tmp_path / "nope.jsonl")}
    nopath = {"task": TASK, "contaminated": False}
    cand = [write_cell(tmp_path, "c0", candidate_calls()), gone, nopath]
    result = summarize(cand, [write_cell(tmp_path, "b0", baseline_calls())], [TASK])
    assert {k: result["coverage"][k] for k in ("candidate_cells", "baseline_cells", "matched_tasks", "missing_trajectories")} == {
        "candidate_cells": 1, "baseline_cells": 1, "matched_tasks": 1, "missing_trajectories": 2}


def test_only_tasks_present_in_both_arms_are_summarized(tmp_path) -> None:
    solo = write_cell(tmp_path, "c9", candidate_calls(), task="solo")
    cand = [write_cell(tmp_path, "c0", candidate_calls()), solo]
    result = summarize(cand, [write_cell(tmp_path, "b0", baseline_calls())], [TASK, "solo"])
    assert result["coverage"]["matched_tasks"] == 1 and result["coverage"]["candidate_cells"] == 1


def test_cells_are_balanced_across_tasks_not_pooled(tmp_path) -> None:
    cheap = [call("mcp__tilth__tilth_search", {}, "x" * 100, 0)]
    dear = [call("mcp__tilth__tilth_search", {}, "x" * 300, 0)]
    cand = [write_cell(tmp_path, f"a{i}", cheap, task="a") for i in range(3)] + [write_cell(tmp_path, "b0", dear, task="b")]
    base = [write_cell(tmp_path, "ba", baseline_calls(), task="a"), write_cell(tmp_path, "bb", baseline_calls(), task="b")]
    result = summarize(cand, base, ["a", "b"])
    assert by(result["per_tool"], tool="tilth_search", arm="candidate")["result_chars_per_cell"] == 200


# --- source map ---


def test_every_mapped_symbol_exists_in_its_file() -> None:
    for cause, entries in source_map.SOURCE_MAP.items():
        assert entries, cause
        for entry in entries:
            path = REPO / entry["path"]
            assert path.is_file(), entry
            assert f"fn {entry['symbol']}" in path.read_text(), entry
            assert entry["role"]


def test_source_map_covers_the_required_outputs() -> None:
    mapped = {(e["path"], e["symbol"]) for entries in source_map.SOURCE_MAP.values() for e in entries}
    assert {("src/mcp/tools/write.rs", "commit_section"), ("src/mcp/tools/write.rs", "resolve_edit"),
            ("src/read/mod.rs", "read_file"), ("src/edit/snapshots.rs", "record"),
            ("src/mcp/tools/definitions.rs", "tool_definitions")} <= mapped
    assert {c for c in attribution.HINTS} <= set(source_map.SOURCE_MAP)


def test_entries_for_dedups_and_ignores_unknown_causes() -> None:
    entries = source_map.entries_for(["write_errors", "tilth_write_receipt", "nope", "write_errors"])
    keys = [(e["path"], e["symbol"]) for e in entries]
    assert len(keys) == len(set(keys)) and ("src/mcp/tools/write.rs", "resolve_edit") in keys
    assert source_map.entries_for(["nope"]) == []


@pytest.mark.skipif(not os.environ.get("TILTH_ATTRIBUTION_ROWS"), reason="TILTH_ATTRIBUTION_ROWS not set")
def test_summarize_over_a_real_rows_file() -> None:
    rows = [json.loads(line) for line in Path(os.environ["TILTH_ATTRIBUTION_ROWS"]).read_text().splitlines() if line]
    cand = [r for r in rows if r.get("mode") != "baseline"]
    base = [r for r in rows if r.get("mode") == "baseline"]
    result = summarize(cand, base, sorted({r["task"] for r in rows}))
    assert result["coverage"]["matched_tasks"] > 0
    assert result["dominant"]
    json.dumps(result)
