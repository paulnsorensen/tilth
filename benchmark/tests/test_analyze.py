"""The power readout is informational and no longer gates task-pool growth."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import analyze


def _row(task: str, mode: str, correct: bool) -> dict:
    return {
        "task": task, "mode": mode, "model": "claude-haiku-4-5-20251001", "repetition": 0,
        "correct": correct, "total_cost_usd": 0.1, "context_tokens": 10, "output_tokens": 5,
        "input_tokens": 2, "cache_creation_tokens": 3, "cache_read_tokens": 4, "num_turns": 1,
        "num_tool_calls": 0, "duration_ms": 1, "result_text": f"{task}-{mode}",
    }


def test_power_readout_is_informational(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    # Four tasks with a small, non-significant effect: the old readout said "grow TASK pool".
    pattern = [(True, True), (True, False), (False, True), (False, True)]
    rows = []
    for index, (baseline_correct, tilth_correct) in enumerate(pattern):
        rows.append(_row(f"t{index}", "baseline", baseline_correct))
        rows.append(_row(f"t{index}", "tilth", tilth_correct))
    results_file = tmp_path / "benchmark_fixture.jsonl"
    results_file.write_text("".join(json.dumps(row) + "\n" for row in rows))
    monkeypatch.setattr(sys, "argv", ["analyze.py", str(results_file)])

    analyze.main()

    report = capsys.readouterr().out
    assert "Power readout by model (informational" in report
    assert "grow TASK pool" not in report
    assert "N INSUFFICIENT for observed effect" in report


def _write(tmp_path: Path, rows: list[dict], name: str = "benchmark_panel.jsonl") -> Path:
    path = tmp_path / name
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def _panel_rows() -> list[dict]:
    stamp = {"panel_name": "gepa-v1", "panel_split_digest": "d" * 64, "panel_split": "dev"}
    return [
        {**_row("fb_task", "baseline", True), **stamp, "contaminated": True, "total_cost_usd": 0.2},
        {**_row("fb_task", "baseline", True), **stamp, "contaminated": False, "total_cost_usd": 0.2},
        {**_row("fb_task", "tilth", True), **stamp, "contaminated": False, "total_cost_usd": 0.2},
        {**_row("fb_task", "tilth", False), **stamp, "contaminated": True, "total_cost_usd": 0.2},
    ]


def _plain_rows() -> list[dict]:
    return [
        {**_row("plain_task", "baseline", True), "contaminated": True},
        {**_row("plain_task", "tilth", True), "contaminated": False},
    ]


def test_panel_contaminated_rows_count_incorrect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    results = analyze.load_results(_write(tmp_path, _panel_rows()))

    baseline = [row for row in results if row["mode"] == "baseline"]
    tilth = [row for row in results if row["mode"] == "tilth"]
    assert len(baseline) == 2 and len(tilth) == 2
    assert analyze.correctness_pct(baseline) == 50.0
    assert analyze.correctness_with_ci(baseline)[0] == 50.0
    assert analyze.cost_per_correct(baseline)[0] == pytest.approx(0.4)
    assert analyze.correctness_pct(tilth) == 50.0

    monkeypatch.setattr(sys, "argv", ["analyze.py", str(_write(tmp_path, [*_panel_rows(), *_plain_rows()]))])
    analyze.main()
    report = capsys.readouterr().out
    section = report.split("## Contaminated panel rows", 1)[1].split("\n## ", 1)[0]
    assert "| fb_task | 1 / 2 | 1 / 2 |" in section
    assert "| **All tasks** | 1 / 2 | 1 / 2 |" in section
    assert "plain_task" not in section


def test_non_panel_rows_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    plain = analyze.load_results(_write(tmp_path, _plain_rows(), "plain.jsonl"))
    assert plain == _plain_rows()
    assert analyze.correctness_pct([row for row in plain if row["mode"] == "baseline"]) == 100.0

    mixed = analyze.load_results(_write(tmp_path, [*_plain_rows(), *_panel_rows()], "mixed.jsonl"))
    assert [row for row in mixed if row["task"] == "plain_task"] == _plain_rows()

    monkeypatch.setattr(sys, "argv", ["analyze.py", str(tmp_path / "plain.jsonl")])
    analyze.main()
    assert "Contaminated panel rows" not in capsys.readouterr().out
