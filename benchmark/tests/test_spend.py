"""Run-wide spend ledger shared by cells, judge calls, reflection, and the proposer."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from spend import SpendLedger


def test_would_cross_compares_spent_plus_estimate_to_ceiling() -> None:
    ledger = SpendLedger(max_usd=1.0)
    ledger.charge(0.5, source="native")

    assert ledger.would_cross(0.6)
    assert not ledger.would_cross(0.5)


def test_charges_accumulate_with_their_source() -> None:
    ledger = SpendLedger(max_usd=10.0)
    ledger.charge(0.25, source="native")
    ledger.charge(0.5, source="estimate")

    assert ledger.spent == pytest.approx(0.75)
    assert ledger.charges == [(0.25, "native"), (0.5, "estimate")]


def test_unbounded_ledger_never_crosses() -> None:
    ledger = SpendLedger(max_usd=None)
    ledger.charge(1_000.0, source="pricing")

    assert not ledger.would_cross(1_000.0)


def test_negative_charge_refused() -> None:
    with pytest.raises(ValueError):
        SpendLedger(max_usd=1.0).charge(-0.1, source="native")


def test_would_cross_counts_reserve() -> None:
    ledger = SpendLedger(max_usd=1.0, reserve=0.3)
    ledger.charge(0.5, source="native")

    assert not ledger.would_cross(0.2)
    assert ledger.would_cross(0.3)
    ledger.reserve = 0.5
    assert ledger.would_cross(0.1)
    ledger.reserve = 0.0
    assert not ledger.would_cross(0.5)
    assert not SpendLedger(max_usd=None, reserve=10.0).would_cross(1_000.0)


# --- persistent budget files and failed-call settlement ---

import json as _json
import multiprocessing
from datetime import datetime, timedelta

from spend import plan_budget_id, settle_failed

STREAMS = Path(__file__).parent / "fixtures" / "streams"


def lines(path: Path) -> list[dict]:
    return [_json.loads(line) for line in path.read_text().splitlines()]


def test_persistent_seeds_spent_from_prior_file(tmp_path: Path) -> None:  # AC-1
    first = SpendLedger.persistent("b1", 10.0, root=tmp_path)
    first.charge(0.5, source="native", run_key="k1")
    first.charge(0.25, source="pricing", run_key="k2")

    second = SpendLedger.persistent("b1", 10.0, root=tmp_path)

    assert second.spent == pytest.approx(0.75)


def test_charge_appends_one_record_before_returning(tmp_path: Path) -> None:  # AC-2
    ledger = SpendLedger.persistent("b2", 10.0, root=tmp_path)
    ledger.charge(0.4, source="estimate", run_key="cell-1")

    first = lines(tmp_path / "b2.jsonl")
    assert [{k: v for k, v in record.items() if k != "at"} for record in first] == [
        {"run_key": "cell-1", "charged_usd": 0.4, "cost_source": "estimate"}]
    ledger.charge(0.1, source="native", run_key="cell-2")
    assert len(lines(tmp_path / "b2.jsonl")) == 2


def test_plan_budget_id_stable_order_and_duplicate_insensitive() -> None:  # AC-3
    assert plan_budget_id(["a", "b", "c"]) == plan_budget_id(["c", "a", "b"])
    assert plan_budget_id(["a", "b"]) == plan_budget_id(["a", "b", "b"])
    assert plan_budget_id(["a"]).startswith("plan-")


def test_plan_budget_id_differs_for_different_sets() -> None:  # AC-12
    assert plan_budget_id(["a", "b"]) != plan_budget_id(["a", "c"])
    assert plan_budget_id(["a", "b"]) != plan_budget_id(["a"])


def test_named_id_file_under_root(tmp_path: Path) -> None:  # AC-4
    ledger = SpendLedger.persistent("my-budget", None, root=tmp_path / "r")

    assert ledger.budget_path == tmp_path / "r" / "my-budget.jsonl"
    assert ledger.budget_id == "my-budget"


def test_default_root_is_bench_data_budgets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # AC-4
    monkeypatch.setenv("TILTH_BENCH_DATA", str(tmp_path / "data"))
    ledger = SpendLedger.persistent("named", 1.0)

    assert ledger.budget_path == tmp_path / "data" / "budgets" / "named.jsonl"


@pytest.mark.parametrize("bad", ["../x", "", "a/b", "..", "a..b", ".hidden"])
def test_invalid_budget_id_raises(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        SpendLedger.persistent(bad, 1.0, root=tmp_path)


def test_fresh_appends_one_reset_marker_and_zeroes_spent(tmp_path: Path) -> None:  # AC-5, AC-10
    SpendLedger.persistent("b3", 10.0, root=tmp_path).charge(2.0, source="native", run_key="old")

    ledger = SpendLedger.persistent("b3", 10.0, fresh=True, root=tmp_path)

    records = lines(tmp_path / "b3.jsonl")
    markers = [r for r in records if r.get("reset")]
    assert len(markers) == 1 and records[-1] == markers[0]
    assert set(markers[0]) == {"reset", "at"} and markers[0]["reset"] is True
    datetime.fromisoformat(markers[0]["at"])
    assert ledger.spent == 0.0


def test_only_records_after_newest_marker_count_on_reopen(tmp_path: Path) -> None:  # AC-5
    SpendLedger.persistent("b4", 10.0, root=tmp_path).charge(2.0, source="native", run_key="before")
    fresh = SpendLedger.persistent("b4", 10.0, fresh=True, root=tmp_path)
    fresh.charge(0.5, source="native", run_key="after")

    assert SpendLedger.persistent("b4", 10.0, root=tmp_path).spent == pytest.approx(0.5)
    assert len(lines(tmp_path / "b4.jsonl")) == 3


def test_new_ceiling_applies_to_recorded_spend(tmp_path: Path) -> None:  # AC-8
    SpendLedger.persistent("b5", 1.0, root=tmp_path).charge(0.6, source="native", run_key="k")

    ledger = SpendLedger.persistent("b5", 5.0, root=tmp_path)
    ledger.reserve = 0.5

    assert ledger.headroom() == pytest.approx(5.0 - 0.6 - 0.5)


def test_would_cross_sees_another_instances_charge(tmp_path: Path) -> None:  # AC-9
    mine = SpendLedger.persistent("b6", 1.0, root=tmp_path)
    other = SpendLedger.persistent("b6", 1.0, root=tmp_path)
    assert not mine.would_cross(0.5)

    other.charge(0.8, source="native", run_key="theirs")

    assert mine.would_cross(0.5)
    assert mine.spent == pytest.approx(0.8)


def _hammer(path: str, count: int, cost: float) -> None:
    ledger = SpendLedger.persistent("shared", None, root=Path(path))
    for index in range(count):
        ledger.charge(cost, source="native", run_key=f"{cost}-{index}")


def test_concurrent_processes_keep_every_record_whole_and_counted(tmp_path: Path) -> None:  # AC-9
    workers, count = 4, 50
    context = multiprocessing.get_context("spawn")
    procs = [context.Process(target=_hammer, args=(str(tmp_path), count, 0.25)) for _ in range(workers)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(60)
        assert proc.exitcode == 0

    records = lines(tmp_path / "shared.jsonl")
    assert len(records) == workers * count
    assert sum(r["charged_usd"] for r in records) == pytest.approx(workers * count * 0.25)
    assert SpendLedger.persistent("shared", None, root=tmp_path).spent == pytest.approx(workers * count * 0.25)


# settle_failed
def _claude_events(*, result: dict | None) -> list[dict]:
    usage = {"input_tokens": 1000, "output_tokens": 200, "cache_creation_input_tokens": 0,
             "cache_read_input_tokens": 0}
    events = [
        {"type": "system", "subtype": "init", "apiKeySource": "none", "model": "claude-sonnet-5"},
        {"type": "assistant", "message": {"id": "m1", "model": "claude-sonnet-5", "role": "assistant",
                                           "usage": usage, "content": [{"type": "text", "text": "hi"}]}},
    ]
    return events + ([result] if result else [])


def _jsonl(events: list[dict]) -> str:
    return "\n".join(_json.dumps(event) for event in events)


def test_settle_failed_claude_native_cost() -> None:  # AC-13
    result = {"type": "result", "subtype": "error_max_turns", "is_error": True, "total_cost_usd": 0.4321,
              "num_turns": 1, "usage": {"input_tokens": 1000, "output_tokens": 200}}

    assert settle_failed(_jsonl(_claude_events(result=result)), "claude", 9.0, "sonnet5") == (0.4321, "native")


def test_settle_failed_claude_prices_assistant_usage_without_result() -> None:  # AC-13
    cost, source = settle_failed(_jsonl(_claude_events(result=None)), "claude", 9.0, "sonnet5")

    assert source == "pricing"
    assert cost == pytest.approx((1000 * 3.0 + 200 * 15.0) / 1e6)


def test_settle_failed_claude_fixture_minus_result_prices() -> None:  # AC-13
    raw = (STREAMS / "claude_no_native_cost.jsonl").read_text().splitlines()
    stripped = "\n".join(line for line in raw if '"type": "result"' not in line)

    cost, source = settle_failed(stripped, "claude", 9.0, "sonnet5")

    assert source == "pricing" and 0 < cost < 9.0


def test_settle_failed_claude_torn_last_line_dropped() -> None:  # AC-13
    raw = _jsonl(_claude_events(result=None)) + '\n{"type": "assistant", "message": {"usa'

    cost, source = settle_failed(raw, "claude", 9.0, "sonnet5")

    assert source == "pricing" and cost == pytest.approx((1000 * 3.0 + 200 * 15.0) / 1e6)


def test_settle_failed_claude_torn_result_does_not_count_as_native() -> None:  # AC-13
    raw = _jsonl(_claude_events(result=None)) + '\n{"type": "result", "total_cost_usd": 7.0, "is_er'

    assert settle_failed(raw, "claude", 9.0, "sonnet5")[1] == "pricing"


@pytest.mark.parametrize("raw", ["", '{"type": "system", "subtype": "init"', _jsonl(_claude_events(result=None)[:1])])
def test_settle_failed_claude_without_usage_uses_estimate(raw: str) -> None:  # AC-14
    assert settle_failed(raw, "claude", 1.5, "sonnet5") == (1.5, "estimate")


def _codex_turn(input_tokens: int, output_tokens: int) -> list[dict]:
    return [
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
        {"type": "turn.completed", "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}},
    ]


def test_settle_failed_codex_prices_completed_turns() -> None:  # AC-13
    raw = _jsonl([{"type": "thread.started", "thread_id": "t"}, *_codex_turn(1000, 200)])

    cost, source = settle_failed(raw, "codex", 9.0, "gpt5")

    assert source == "pricing"
    assert cost == pytest.approx((1000 * 5.0 + 200 * 30.0) / 1e6)


def test_settle_failed_codex_torn_last_line_dropped() -> None:  # AC-13
    raw = _jsonl([{"type": "thread.started", "thread_id": "t"}, *_codex_turn(1000, 200)]) + '\n{"type": "turn.sta'

    assert settle_failed(raw, "codex", 9.0, "gpt5")[1] == "pricing"


@pytest.mark.parametrize("events", [
    [],
    [{"type": "thread.started", "thread_id": "t"}, {"type": "turn.started"}],
])
def test_settle_failed_codex_without_completed_usage_uses_estimate(events: list[dict]) -> None:  # AC-14
    assert settle_failed(_jsonl(events), "codex", 2.5, "gpt5") == (2.5, "estimate")


def test_settle_failed_codex_prices_completed_turns_before_a_trailing_error() -> None:  # AC-13
    raw = _jsonl([{"type": "thread.started", "thread_id": "t"}, *_codex_turn(1000, 200),
                  {"type": "error", "message": "x"}])

    cost, source = settle_failed(raw, "codex", 9.0, "gpt5")

    assert source == "pricing"
    assert cost == pytest.approx((1000 * 5.0 + 200 * 30.0) / 1e6)


def test_settle_failed_codex_prices_completed_turns_before_a_trailing_turn_started() -> None:  # AC-13
    raw = _jsonl([{"type": "thread.started", "thread_id": "t"}, *_codex_turn(1000, 200), {"type": "turn.started"}])

    cost, source = settle_failed(raw, "codex", 9.0, "gpt5")

    assert source == "pricing"
    assert cost == pytest.approx((1000 * 5.0 + 200 * 30.0) / 1e6)


def test_settle_failed_codex_prices_completed_turns_before_a_trailing_turn_failed() -> None:  # AC-13
    raw = _jsonl([{"type": "thread.started", "thread_id": "t"}, *_codex_turn(1000, 200),
                  {"type": "turn.started"}, {"type": "turn.failed", "error": {"message": "x"}}])

    cost, source = settle_failed(raw, "codex", 9.0, "gpt5")

    assert source == "pricing"
    assert cost == pytest.approx((1000 * 5.0 + 200 * 30.0) / 1e6)


def test_settle_failed_null_message_falls_back_to_the_estimate() -> None:
    raw = _jsonl([{"type": "assistant", "message": None}])

    assert settle_failed(raw, "claude", 1.5, "sonnet5") == (1.5, "estimate")


def test_charge_record_has_an_iso_utc_timestamp(tmp_path: Path) -> None:
    ledger = SpendLedger.persistent("stamped", 10.0, root=tmp_path)
    ledger.charge(0.4, source="native", run_key="k")

    stamp = datetime.fromisoformat(lines(tmp_path / "stamped.jsonl")[0]["at"])

    assert stamp.utcoffset() == timedelta(0)


def test_records_without_a_timestamp_still_count(tmp_path: Path) -> None:
    (tmp_path / "old.jsonl").write_text(_json.dumps({"run_key": "k", "charged_usd": 2.0, "cost_source": "x"}) + "\n")

    assert SpendLedger.persistent("old", 10.0, root=tmp_path).spent == pytest.approx(2.0)


def test_charge_survives_an_unreadable_line_and_the_next_check_raises(tmp_path: Path) -> None:
    ledger = SpendLedger.persistent("mid", 10.0, root=tmp_path)
    ledger.charge(1.0, source="native", run_key="k1")
    with open(tmp_path / "mid.jsonl", "a") as out:
        out.write('{"run_key": "torn", "charged_usd": 5.0, "cost_sou\n')

    ledger.charge(2.0, source="native", run_key="k2")  # does not raise: the paid row must be stored

    assert ledger.spent == pytest.approx(3.0)
    assert lines_or_raw(tmp_path / "mid.jsonl")[-1]["run_key"] == "k2"
    with pytest.raises(ValueError, match="mid.jsonl"):
        ledger.would_cross(0.1)
    with pytest.raises(ValueError, match="mid.jsonl"):
        ledger.exhausted()


def lines_or_raw(path: Path) -> list[dict]:
    records = []
    for line in path.read_text().splitlines():
        try:
            records.append(_json.loads(line))
        except ValueError:
            pass
    return records


def test_unreadable_and_negative_records_keep_their_meaning(tmp_path: Path) -> None:
    (tmp_path / "mix.jsonl").write_text(
        _json.dumps({"reset": True}) + "\n" + _json.dumps({"charged_usd": -3.0}) + "\n"
        + _json.dumps({"note": "keyless"}) + "\n" + _json.dumps({"charged_usd": 1.5}) + "\n")

    assert SpendLedger.persistent("mix", 10.0, root=tmp_path).spent == pytest.approx(1.5)
