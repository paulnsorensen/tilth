"""Offline tests for the diagnostic tool-call replay, against a tiny synthetic repo and trajectory.

The replay tests need a debug build (`cargo build`); like the strict-hook tests they skip when it is absent.
"""

import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from evolve import replay

BINARY = Path(__file__).resolve().parents[2] / "target" / "debug" / "tilth"
needs_binary = pytest.mark.skipif(not BINARY.is_file(), reason="target/debug/tilth is not built")

OLD_ROOT = "/tmp/tilth-benchmark-abc123/proj"
ORIGINAL = "def answer():\n    x = 1\n    return x\n"
EDITED = "def answer():\n    x = 2\n    return x\n"


@pytest.fixture
def fixture_repo(tmp_path, monkeypatch) -> Path:
    source = tmp_path / "fixture"
    source.mkdir()
    (source / "app.py").write_text(ORIGINAL)

    def prepare(row: dict, workdir: Path) -> Path:
        workspace = workdir / "proj"
        shutil.copytree(source, workspace)
        return workspace

    monkeypatch.setattr(replay, "prepare_workspace", prepare)
    return source


def file_tag(source: Path, tmp_path: Path) -> str:
    """The real whole-file tag of app.py, read through a live server."""
    work = tmp_path / "tag"
    work.mkdir()
    server = replay.McpServer(BINARY, source, work / "stderr")
    try:
        text, error = server.call("tilth_read", {"paths": ["app.py"], "cwd": str(source)})
    finally:
        server.close()
    assert not error
    match = re.search(r"#([0-9A-F]{4})\]", text)
    assert match, text
    return match.group(1)


def tilth_call(tool: str, arguments: dict, output: object = "recorded", is_error: bool = False) -> dict:
    return {"name": replay.TOOL_PREFIX + tool, "input": arguments, "output": output, "is_error": is_error}


def bash_call(command: str, output: object = "", is_error: bool = False) -> dict:
    return {"name": "Bash", "input": {"command": command}, "output": output, "is_error": is_error}


def read_call(path: str = "app.py", cwd: str = OLD_ROOT) -> dict:
    return tilth_call("tilth_read", {"paths": [path], "cwd": cwd})


def write_call(tag: str, output: object = "recorded", is_error: bool = False) -> dict:
    edits = [{"path": "app.py", "tag": tag, "ops": [{"op": "replace_text", "old": "x = 1", "new": "x = 2"}]}]
    return tilth_call("tilth_write", {"edits": edits, "cwd": OLD_ROOT}, output, is_error)


def make_row(tmp_path: Path, calls: list[dict], name: str = "cell") -> dict:
    path = tmp_path / f"{name}.jsonl"
    path.write_text("".join(json.dumps(call) + "\n" for call in calls))
    return {"task": "synthetic", "mode": "tilth_forced", "repetition": 0, "trajectory_path": str(path)}


def run_replay(tmp_path: Path, calls: list[dict], name: str = "cell", binary: Path = BINARY) -> dict:
    return replay.replay_cell(make_row(tmp_path, calls, name), binary, workdir=tmp_path / f"work-{name}")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# --- replay_cell: read -> write -> read ---


@needs_binary
def test_read_write_read_is_fully_covered_and_edits_the_fresh_workspace(fixture_repo, tmp_path) -> None:
    tag = file_tag(fixture_repo, tmp_path)

    report = run_replay(tmp_path, [read_call(), write_call(tag), read_call()])

    assert (report["total_calls"], report["replayed_calls"], report["stopped_at"]) == (3, 3, None)
    assert [call["tool"] for call in report["calls"]] == ["tilth_read", "tilth_write", "tilth_read"]
    assert report["file_hashes"] == {"app.py": sha(EDITED)}
    assert report["unremapped_paths"] == []
    assert all(call["replayed_chars"] > 0 and call["replayed_error"] is False for call in report["calls"])
    assert (fixture_repo / "app.py").read_text() == ORIGINAL, "the fixture itself must stay untouched"


@needs_binary
def test_replay_reports_recorded_and_replayed_sizes_per_call(fixture_repo, tmp_path) -> None:
    report = run_replay(tmp_path, [read_call()])

    (call,) = report["calls"]
    assert call["recorded_chars"] == len("recorded")
    assert call["replayed_chars"] != call["recorded_chars"]
    assert call["recorded_truncated"] is False


@needs_binary
def test_a_persisted_recorded_output_is_flagged_truncated(fixture_repo, tmp_path) -> None:
    persisted = replay.PERSISTED_MARKER + "preview"

    report = run_replay(tmp_path, [tilth_call("tilth_read", {"paths": ["app.py"], "cwd": OLD_ROOT}, persisted)])

    assert report["calls"][0]["recorded_truncated"] is True


# --- path remap ---


@needs_binary
def test_absolute_recorded_paths_are_remapped_and_outside_paths_are_reported(fixture_repo, tmp_path) -> None:
    calls = [read_call(OLD_ROOT + "/app.py"), read_call("/etc/hostname")]

    report = run_replay(tmp_path, calls)

    assert report["unremapped_paths"] == ["/etc/hostname"]
    assert report["calls"][0]["replayed_error"] is False
    assert OLD_ROOT not in json.dumps(report), "the report must not point into the long-gone recorded root"


def test_remap_touches_path_fields_only() -> None:
    arguments = {"cwd": OLD_ROOT, "paths": [OLD_ROOT + "/a.py"], "edits": [{"path": OLD_ROOT + "/a.py", "ops": [
        {"op": "append", "content": f"# see {OLD_ROOT}\n"}]}]}

    result = replay.remap(arguments, OLD_ROOT, "/new/proj")

    assert result["cwd"] == "/new/proj"
    assert result["paths"] == ["/new/proj/a.py"]
    assert result["edits"][0]["path"] == "/new/proj/a.py"
    assert result["edits"][0]["ops"][0]["content"] == f"# see {OLD_ROOT}\n"


@needs_binary
def test_a_write_outside_the_workspace_stops_the_replay_before_it_lands(fixture_repo, tmp_path) -> None:
    outside = tmp_path / "outside"
    shutil.copytree(fixture_repo, outside)
    tag = file_tag(fixture_repo, tmp_path)
    target = str(outside / "app.py")
    edits = [{"path": target, "tag": tag, "ops": [{"op": "replace_text", "old": "x = 1", "new": "x = 2"}]}]
    calls = [read_call(target), tilth_call("tilth_write", {"edits": edits, "cwd": OLD_ROOT}), read_call()]

    report = run_replay(tmp_path, calls)

    assert report["stopped_at"] == {"index": 1, "reason": "write names a path outside the workspace"}
    assert (outside / "app.py").read_text() == ORIGINAL, "replay must never write outside its temp workspace"
    assert target in report["unremapped_paths"]


def test_unremapped_resolves_dotdot_before_the_containment_check() -> None:
    escape = "/new/proj/../../etc/x"

    assert replay.unremapped({"path": escape, "cwd": "/new/proj"}, "/new/proj") == [escape]
    assert replay.unremapped({"path": "/new/proj/a/../b.py"}, "/new/proj") == []


def test_recorded_root_is_the_most_common_workspace_prefix() -> None:
    calls = [read_call(cwd=OLD_ROOT), read_call(cwd=OLD_ROOT), read_call(cwd="/tmp/tilth-benchmark-zzz/other")]

    assert replay.recorded_root(calls) == OLD_ROOT


# --- Bash handling ---


@needs_binary
def test_unlisted_bash_stops_the_replay_and_is_never_executed(fixture_repo, tmp_path) -> None:
    sentinel = tmp_path / "sentinel"
    calls = [read_call(), bash_call(f"touch {sentinel}"), read_call()]

    report = run_replay(tmp_path, calls)

    assert report["stopped_at"]["index"] == 1
    assert "allowlist" in report["stopped_at"]["reason"]
    assert report["replayed_calls"] == 1
    assert not sentinel.exists(), "replay must never run recorded Bash"


@needs_binary
def test_allowlisted_and_hook_blocked_bash_are_skipped_without_running(fixture_repo, tmp_path) -> None:
    sentinel = tmp_path / "sentinel"
    calls = [
        read_call(),
        bash_call("git status"),
        bash_call(f"cat app.py > {sentinel}", replay.HOOK_BLOCKED + ": blocked", is_error=True),
        read_call(),
    ]

    report = run_replay(tmp_path, calls)

    assert (report["replayed_calls"], report["skipped_bash"], report["stopped_at"]) == (2, 2, None)
    assert [call["index"] for call in report["calls"]] == [0, 3]
    assert not sentinel.exists()


@needs_binary
def test_a_non_tilth_tool_stops_the_replay(fixture_repo, tmp_path) -> None:
    report = run_replay(tmp_path, [{"name": "Task", "input": {}, "output": ""}, read_call()])

    assert report["stopped_at"]["index"] == 0
    assert report["replayed_calls"] == 0


@pytest.mark.parametrize("command", [
    "ls -la", "pwd", "git status", "git -C /x diff --stat", "git log --oneline | head -5",
    "cd /x && git show HEAD", "find . -name '*.py'",
])
def test_read_only_bash_accepts_audited_commands(command) -> None:
    assert replay.read_only_bash(command) is True


@pytest.mark.parametrize("command", [
    "find . -exec rm {} ;", "find . -delete", "find . -fprint out.txt", "ls > out.txt", "git status >> log",
    "git commit -m x", "git checkout main", "git diff --output=patch.txt", "sort -o out in", "cat app.py",
    "ls; rm -rf /", "ls $(rm x)", "ls `rm x`", "ls &", "(ls)", "python -m pytest", "ls\nrm x",
])
def test_read_only_bash_refuses_writers_and_unlisted_commands(command) -> None:
    assert replay.read_only_bash(command) is False


# --- divergence ---


@needs_binary
def test_a_write_that_fails_on_replay_but_succeeded_when_recorded_stops_the_replay(fixture_repo, tmp_path) -> None:
    calls = [read_call(), write_call("0000"), read_call()]

    report = run_replay(tmp_path, calls)

    assert report["stopped_at"]["index"] == 1
    assert report["stopped_at"]["reason"].startswith("divergent write")
    assert report["replayed_calls"] == 2, "the divergent write is recorded; nothing after it is replayed"
    assert report["calls"][-1]["replayed_error"] is True
    assert report["file_hashes"] == {"app.py": sha(ORIGINAL)}


@needs_binary
def test_a_rejected_section_of_a_multi_section_write_counts_as_a_failed_write(fixture_repo, tmp_path) -> None:
    tag = file_tag(fixture_repo, tmp_path)
    (fixture_repo / "other.py").write_text("y = 1\n")
    sections = [
        {"path": "app.py", "tag": tag, "ops": [{"op": "replace_text", "old": "x = 1", "new": "x = 2"}]},
        {"path": "other.py", "tag": "0000", "ops": [{"op": "replace_text", "old": "y = 1", "new": "y = 2"}]},
    ]
    calls = [read_call(), tilth_call("tilth_write", {"edits": sections, "cwd": OLD_ROOT}), read_call()]

    report = run_replay(tmp_path, calls)

    assert report["calls"][1]["replayed_error"] is True
    assert report["stopped_at"]["index"] == 1


def test_section_failed_reads_per_section_status_lines() -> None:
    assert replay._section_failed("## a.py\napplied\n\n---\n\n## b.py\nerror: Edit rejected") is True
    assert replay._section_failed("## a.py\napplied\n[a.py#1A2B]\n3:x = 2") is False


@needs_binary
def test_a_write_that_failed_when_recorded_and_fails_again_does_not_stop(fixture_repo, tmp_path) -> None:
    calls = [read_call(), write_call("0000", "stale tag", is_error=True), read_call()]

    report = run_replay(tmp_path, calls)

    assert (report["replayed_calls"], report["stopped_at"]) == (3, None)


@needs_binary
def test_a_recorded_write_without_a_result_stops_the_replay(fixture_repo, tmp_path) -> None:
    report = run_replay(tmp_path, [read_call(), write_call("0000", output=None)])

    assert report["stopped_at"] == {"index": 1, "reason": "recorded write has no result"}


# --- compare ---


def report_of(calls: list[tuple[int, str, int, bool]], hashes: dict | None = None, stopped_at=None, total: int = 4) -> dict:
    return {
        "task": "t", "total_calls": total, "replayed_calls": len(calls), "skipped_bash": 0, "stopped_at": stopped_at,
        "unremapped_paths": [], "file_hashes": hashes or {},
        "calls": [{"index": i, "tool": tool, "recorded_chars": 0, "replayed_chars": chars, "recorded_error": False,
                   "replayed_error": error, "recorded_truncated": False} for i, tool, chars, error in calls],
    }


def test_compare_reports_char_deltas_by_tool_and_new_errors() -> None:
    seed = report_of([(0, "tilth_read", 100, False), (1, "tilth_write", 900, False), (2, "tilth_search", 50, False)],
                     {"a.py": "h1"})
    candidate = report_of([(0, "tilth_read", 100, False), (1, "tilth_write", 100, False), (2, "tilth_search", 60, True)],
                          {"a.py": "h1"})

    result = replay.compare(seed, candidate)

    assert result == {
        "coverage": 0.75,
        "result_chars_delta_by_tool": {"tilth_read": 0, "tilth_search": 10, "tilth_write": -800},
        "new_error_tools": ["tilth_search"],
        "files_match": True,
    }


def test_compare_flags_differing_final_files() -> None:
    seed = report_of([(0, "tilth_write", 10, False)], {"a.py": "h1"})
    candidate = report_of([(0, "tilth_write", 5, False)], {"a.py": "h2"})

    assert replay.compare(seed, candidate)["files_match"] is False


def test_compare_files_match_is_unknown_without_touched_files_or_with_different_stops() -> None:
    no_files = report_of([(0, "tilth_read", 10, False)])
    assert replay.compare(no_files, no_files)["files_match"] is None

    seed = report_of([(0, "tilth_write", 10, False)], {"a.py": "h1"})
    stopped = report_of([(0, "tilth_write", 5, False)], {"a.py": "h1"}, stopped_at={"index": 1, "reason": "divergent write"})
    assert replay.compare(seed, stopped)["files_match"] is None


def test_compare_only_counts_calls_both_replays_reached() -> None:
    seed = report_of([(0, "tilth_read", 10, False), (1, "tilth_read", 10, False)])
    candidate = report_of([(0, "tilth_read", 4, False)])

    assert replay.compare(seed, candidate)["result_chars_delta_by_tool"] == {"tilth_read": -6}


@needs_binary
def test_seed_on_seed_compare_matches_files_and_sizes(fixture_repo, tmp_path) -> None:
    tag = file_tag(fixture_repo, tmp_path)
    calls = [read_call(), write_call(tag), read_call()]

    first = run_replay(tmp_path, calls, "first")
    second = run_replay(tmp_path, calls, "second")
    result = replay.compare(first, second)

    assert result["files_match"] is True
    assert result["new_error_tools"] == []
    assert result["coverage"] == 1.0
    assert result["result_chars_delta_by_tool"]["tilth_write"] == 0
