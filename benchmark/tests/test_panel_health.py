"""panel_health: per-task pass rates, failure classes, wall time against timeout, and budget hits."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import panel_health


def row(task: str, mode: str, rep: int, correct: bool, **fields: object) -> dict:
    return {"task": task, "mode": mode, "repetition": rep, "correct": correct, "reused": False,
            "run_key": f"{task}-{mode}-{rep}", "trajectory_path": f"{task}-{mode}-{rep}.jsonl",
            "wall_duration_ms": 100_000, "timeout_s": 600, "max_budget_usd": 1.0, "charged_usd": 0.2,
            "correctness_reason": "ok" if correct else "Missing: needle", **fields}


def chain(mode: str, rep: int, wrong: list[str]) -> dict:
    subs = [{"task": name, "correct": name not in wrong, "duration_ms": 50_000} for name in ("read_a", "read_b", "edit")]
    return row("chain:c-3", mode, rep, not wrong, sub_tasks=subs, chain_size=3,
               correctness_reason=f"{3 - len(wrong)}/3 sub-tasks correct")


def write_rows(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(item) + "\n" for item in rows) + "{torn\n")
    return path


def test_a_sub_task_that_fails_in_both_arms_is_flagged() -> None:
    rows = [chain("baseline", 0, ["read_b"]), chain("baseline", 1, []), chain("tilth_forced", 0, ["read_b"]),
            chain("tilth_forced", 1, ["read_b"])]

    health = panel_health.summarize(rows)["chain:c-3"]

    assert (health.arms["baseline"].passed, health.arms["tilth_forced"].passed) == (1, 0)
    assert health.shared_failure() == "sub:read_b"
    assert "both-arms: sub:read_b" in health.flags()


def test_a_failure_in_one_arm_only_is_not_a_shared_failure() -> None:
    rows = [row("t", "baseline", rep, True) for rep in range(4)]
    rows += [row("t", "tilth_forced", rep, rep > 1, f2p_total=1, f2p_passed=0) for rep in range(4)]

    health = panel_health.summarize(rows)["t"]

    assert health.arms["tilth_forced"].failures == {"f2p": 2}
    assert health.shared_failure() is None
    assert health.flags() == []


def test_budget_cap_timeout_and_slow_cells_are_counted() -> None:
    capped = row("t", "tilth_forced", 0, False, error="claude -p failed: ... error_max_budget_usd ...",
                 charged_usd=1.01, wall_duration_ms=None, duration_ms=None)
    timed_out = row("t", "tilth_forced", 1, False, error="timeout", timed_out=True, wall_duration_ms=610_000)
    slow = row("t", "baseline", 0, True, wall_duration_ms=400_000, charged_usd=0.9)

    health = panel_health.summarize([capped, timed_out, slow])["t"]

    assert (health.budget_caps, health.timed_out) == (1, 1)
    assert set(health.flags()) == {"timeout", "slow", "budget-cap"}
    assert panel_health.suggested_timeout(health) == 1500


def test_a_chain_budget_is_the_member_budget_times_its_size() -> None:
    rows = [{**chain("baseline", 0, []), "charged_usd": 2.7}]

    health = panel_health.summarize(rows)["chain:c-3"]

    assert health.budgets == {3.0}
    assert health.cost_ratio == [pytest.approx(0.9)]
    assert "near-budget" in health.flags()


def test_a_chain_is_timed_by_its_slowest_turn() -> None:
    """The cell timeout applies to each chain turn, so the whole conversation is not compared with it."""
    health = panel_health.summarize([{**chain("baseline", 0, []), "wall_duration_ms": 900_000}])["chain:c-3"]

    assert health.wall_s == [50.0]


def test_load_rows_skips_reused_shared_and_torn_rows(tmp_path: Path) -> None:
    paid = row("t", "baseline", 0, True)
    first = write_rows(tmp_path / "a.jsonl", [paid, {**row("t", "baseline", 1, True), "reused": True}])
    second = write_rows(tmp_path / "b.jsonl", [paid, row("t", "baseline", 2, False)])

    rows = panel_health.load_rows([first, second])

    assert [item["repetition"] for item in rows] == [0, 2]


def test_main_prints_a_markdown_table_in_panel_order(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rows = write_rows(tmp_path / "rows.jsonl", [row("b_task", "baseline", 0, True), row("a_task", "baseline", 0, False),
                                                row("not_in_panel", "baseline", 0, True)])
    panel = tmp_path / "panel.json"
    panel.write_text(json.dumps({"members": [{"id": "b_task", "split": "dev"}, {"id": "a_task", "split": "test"}]}))

    assert panel_health.main([str(rows), "--panel", str(panel)]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("| task | split | baseline pass |")
    assert [line.split(" | ")[0] for line in lines[2:]] == ["| b_task", "| a_task"]
    assert "| a_task | test | 0/1 (0%) | b: missing:needle x1 |" in lines[3]


def test_percentile_is_nearest_rank() -> None:
    assert [panel_health.percentile([4, 1, 3, 2], q) for q in (0, 50, 90, 100)] == [1, 2, 4, 4]
