"""The power readout is informational and no longer gates task-pool growth."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import analyze

_BENCHMARK_DIR = Path(__file__).parent.parent


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


def test_phase4_plan_is_superseded() -> None:
    plan = (_BENCHMARK_DIR / "PHASE4_PLAN.md").read_text()

    assert plan.splitlines()[0].startswith("# ")
    assert "Superseded" in plan.split("\n## ", 1)[0]
    assert "benchmark-gepa-overhaul" in plan.split("\n## ", 1)[0]
