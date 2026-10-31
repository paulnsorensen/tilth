"""Adversarial tests for the evolve cost-per-correct score (p * base / c)."""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import run
from evolve.loop import COST_SUMMARY_FIELDS, EvolveError, cost_summary, _baseline_costs, _paired_score_deltas, dominates, row_cost, task_score, task_stats
from evolve import candidate, engine, proposer
from evolve_support import DEV, build, child, log_text, seed_candidate
from test_evolve import accept, ready, three_test_task_world, tilth_calls, world  # noqa: F401  (world is a fixture)


def row(task: str, correct: bool, **fields) -> dict:
    return {"task": task, "correct": correct, "contaminated": False, "repetition": 0, "run_key": "k", **fields}


def base_row(task: str, cost, **fields) -> dict:
    return row(task, True, total_cost_usd=cost, mode="baseline", **fields)


# --- row_cost: hostile values ---


@pytest.mark.parametrize("bad", [True, False, "0.5", None, [0.5], {"v": 1}])
def test_row_cost_total_cost_of_wrong_type_with_no_fallback_raises(bad) -> None:
    with pytest.raises(EvolveError):
        row_cost({"task": "t", "total_cost_usd": bad})


@pytest.mark.parametrize("bad", [True, "0.5"])
def test_row_cost_bad_total_cost_does_not_silently_fall_back_to_charged_usd(bad) -> None:
    # A present-but-malformed total_cost_usd is corrupt data; charged_usd must not paper over it.
    with pytest.raises(EvolveError):
        row_cost({"task": "t", "total_cost_usd": bad, "charged_usd": 0.0})


def test_row_cost_reused_row_uses_total_cost_not_zero_charged_usd() -> None:
    assert row_cost({"total_cost_usd": 0.25, "charged_usd": 0.0}) == 0.25
    rows = [row("t", True, total_cost_usd=0.25, charged_usd=0.0, reused=True)]
    assert task_score(rows, "t", 0.5) == pytest.approx(2.0)


def test_row_cost_total_cost_zero_is_not_replaced_by_charged_usd() -> None:
    assert row_cost({"total_cost_usd": 0.0, "charged_usd": 9.0}) == 0.0


# --- task_score: hostile costs ---


def test_task_score_nan_cost_on_solved_task_raises() -> None:
    with pytest.raises(EvolveError):
        task_score([row("t", True, total_cost_usd=math.nan)], "t", 0.1)


def test_task_score_nan_cost_on_unsolved_task_raises() -> None:
    # A non-finite cost is corrupt data whether or not the task was solved.
    with pytest.raises(EvolveError):
        task_score([row("t", False, total_cost_usd=math.nan)], "t", 0.1)


def test_task_score_infinite_cost_raises() -> None:
    # An infinite cost would silently score the task 0; it is corrupt data.
    with pytest.raises(EvolveError):
        task_score([row("t", True, total_cost_usd=math.inf)], "t", 0.1)


def test_task_score_negative_cost_on_solved_task_raises() -> None:
    with pytest.raises(EvolveError):
        task_score([row("t", True, total_cost_usd=-0.1)], "t", 0.1)


def test_task_score_negative_row_cannot_hide_behind_a_positive_mean() -> None:
    # Mean of (-0.1, 0.5) is positive, yet a negative cost is corrupt data and must not be scored.
    rows = [row("t", True, total_cost_usd=-0.1), row("t", True, total_cost_usd=0.5)]
    with pytest.raises(EvolveError):
        task_score(rows, "t", 0.1)


def test_task_score_negative_cost_on_unsolved_task_raises() -> None:
    with pytest.raises(EvolveError):
        task_score([row("t", False, total_cost_usd=-1.0)], "t", 0.1)


def test_task_score_unsolved_zero_cost_scores_zero_without_raising() -> None:
    assert task_score([row("t", False, total_cost_usd=0.0)], "t", 0.1) == 0.0


def test_task_score_solved_zero_cost_raises() -> None:
    with pytest.raises(EvolveError):
        task_score([row("t", True, total_cost_usd=0.0)], "t", 0.1)


def test_task_score_contaminated_correct_row_scores_zero_but_still_needs_a_cost() -> None:
    assert task_score([row("t", True, contaminated=True, total_cost_usd=0.2)], "t", 0.1) == 0.0
    with pytest.raises(EvolveError):
        task_score([row("t", True, contaminated=True)], "t", 0.1)


def test_task_score_contaminated_zero_cost_row_scores_zero_without_raising() -> None:
    assert task_score([row("t", True, contaminated=True, total_cost_usd=0.0)], "t", 0.1) == 0.0


def test_task_score_contaminated_row_still_dilutes_the_pass_rate() -> None:
    rows = [row("t", True, total_cost_usd=0.1), row("t", True, contaminated=True, total_cost_usd=0.1)]
    assert task_score(rows, "t", 0.1) == pytest.approx(0.5)


def test_task_stats_ignores_other_tasks_and_returns_none_without_rows() -> None:
    assert task_stats([row("u", True, total_cost_usd=1.0)], "t") is None
    assert task_stats([], "t") is None


def test_task_stats_a_costless_row_for_another_task_does_not_poison_this_task() -> None:
    rows = [row("t", True, total_cost_usd=0.2), row("u", True)]
    assert task_stats(rows, "t") == (1.0, 0.2)


# --- _baseline_costs ---


@pytest.mark.parametrize("bad", [True, False, "0.5", None, math.nan, 0.0, -0.1, math.inf])
def test_baseline_costs_rejects_a_non_finite_or_non_positive_cost(bad) -> None:
    with pytest.raises(EvolveError):
        _baseline_costs([base_row("t", bad)], ["t"])


def test_baseline_costs_one_bad_row_among_good_ones_still_stops() -> None:
    with pytest.raises(EvolveError):
        _baseline_costs([base_row("t", 0.1), base_row("t", math.nan)], ["t"])


def test_baseline_costs_incomplete_rows_are_ignored_even_with_garbage_cost() -> None:
    rows = [base_row("t", 0.2), base_row("t", None, error="boom"), base_row("t", -5.0, infra="x"),
            base_row("t", math.nan, timed_out=True)]
    assert _baseline_costs(rows, ["t"]) == {"t": 0.2}


def test_baseline_costs_mean_uses_only_completed_rows() -> None:
    rows = [base_row("t", 0.2), base_row("t", 0.4), base_row("t", 100.0, error="boom")]
    assert _baseline_costs(rows, ["t"]) == {"t": pytest.approx(0.3)}


def test_baseline_costs_task_with_only_incomplete_rows_raises() -> None:
    with pytest.raises(EvolveError, match="no completed baseline row"):
        _baseline_costs([base_row("t", 0.2, error="boom")], ["t"])


def test_baseline_costs_task_with_no_rows_raises_and_extra_tasks_are_ignored() -> None:
    with pytest.raises(EvolveError):
        _baseline_costs([base_row("u", 0.2)], ["t"])
    assert _baseline_costs([base_row("u", 0.2), base_row("t", 0.1)], ["t"]) == {"t": 0.1}


def test_baseline_costs_incorrect_and_contaminated_rows_still_supply_a_cost() -> None:
    assert _baseline_costs([row("t", False, total_cost_usd=0.3)], ["t"]) == {"t": 0.3}
    assert _baseline_costs([row("t", True, contaminated=True, total_cost_usd=0.3)], ["t"]) == {"t": 0.3}


def test_baseline_costs_reused_row_uses_total_cost_usd() -> None:
    assert _baseline_costs([base_row("t", 0.3, charged_usd=0.0, reused=True)], ["t"]) == {"t": 0.3}


# --- _paired_score_deltas ---


def test_paired_score_deltas_skips_tasks_with_rows_on_only_one_side() -> None:
    winner = [row("a", True, total_cost_usd=0.1), row("b", True, total_cost_usd=0.1)]
    seed = [row("a", True, total_cost_usd=0.2), row("c", True, total_cost_usd=0.2)]
    deltas = _paired_score_deltas(winner, seed, ["a", "b", "c"], {"a": 0.2})
    assert deltas == [pytest.approx(2.0 - 1.0)]


def test_paired_score_deltas_empty_when_no_task_is_paired() -> None:
    one = [row("a", True, total_cost_usd=0.1)]
    other = [row("b", True, total_cost_usd=0.1)]
    assert _paired_score_deltas(one, other, ["a", "b"], {}) == []
    assert _paired_score_deltas([], [], ["a"], {"a": 1.0}) == []


def test_paired_score_deltas_accepts_one_shot_iterators() -> None:
    winner = iter([row("a", True, total_cost_usd=0.1)])
    seed = iter([row("a", True, total_cost_usd=0.1)])
    assert _paired_score_deltas(winner, seed, iter(["a"]), {"a": 0.1}) == [0.0]


def test_paired_score_deltas_unsolved_seed_with_zero_cost_scores_zero_not_raise() -> None:
    winner = [row("a", True, total_cost_usd=0.1)]
    seed = [row("a", False, total_cost_usd=0.0)]
    assert _paired_score_deltas(winner, seed, ["a"], {"a": 0.1}) == [pytest.approx(1.0)]


def test_paired_score_deltas_solved_winner_with_zero_cost_raises() -> None:
    with pytest.raises(EvolveError):
        _paired_score_deltas([row("a", True, total_cost_usd=0.0)], [row("a", True, total_cost_usd=0.1)],
                             ["a"], {"a": 0.1})


# --- dominance with continuous scores ---


def test_dominates_equal_continuous_scores_never_dominate() -> None:
    scores = {"a": 1.2345678901234567, "b": 0.0}
    assert not dominates(scores, dict(scores), ["a", "b"])


def test_dominates_nan_score_never_dominates_or_is_dominated() -> None:
    assert not dominates({"a": math.nan, "b": 1.0}, {"a": 0.5, "b": 0.5}, ["a", "b"])
    assert not dominates({"a": 1.0, "b": 1.0}, {"a": math.nan, "b": 0.5}, ["a", "b"])


def test_candidate_with_scores_equal_to_the_seed_does_not_enter_the_frontier(world) -> None:  # noqa: F811
    evo = ready(world)
    seed = evo.cascade(evo.seed)
    same = evo.cascade(child(evo.seed, tag="same", dev_a="1", dev_b="1", cheap_a="1", test_a="0"))
    assert same.means == pytest.approx(seed.means)
    assert not dominates(same.means, seed.means, DEV) and not dominates(seed.means, same.means, DEV)


# --- evaluate side info ---


def test_evaluate_side_info_omits_cost_fields_for_a_dev_task_the_candidate_never_ran(world, monkeypatch) -> None:  # noqa: F811
    evo = ready(world)
    evo.cascade(evo.seed)
    real = run.run_plan

    def drop_dev_b(*args, **kwargs):
        return [found for found in real(*args, **kwargs) if found["task"] != "dev_b"]

    monkeypatch.setattr(run, "run_plan", drop_dev_b)
    fresh = child(evo.seed, tag="partial", dev_a="1", dev_b="1", cheap_a="1", test_a="0")
    score, info = evo.evaluate(fresh, "dev_a")
    assert score >= 0.0
    assert "dev_b" not in info["pass_rate"] and "dev_b" not in info["mean_cost"]
    assert "dev_b" not in info["baseline_cost"]
    assert set(info["pass_rate"]) == set(info["mean_cost"]) == set(info["baseline_cost"])


def test_evaluate_side_info_for_unsolved_dev_tasks_reports_zero_pass_rate_and_real_cost(world) -> None:  # noqa: F811
    evo = ready(world)
    evo.cascade(evo.seed)
    score, info = evo.evaluate(child(evo.seed, tag="bad", dev_a="0", dev_b="0", cheap_a="1", test_a="0"), "dev_a")
    assert score == 0.0
    assert info["pass_rate"] == {"dev_a": 0.0, "dev_b": 0.0}
    assert info["mean_cost"] == pytest.approx({"dev_a": 0.1, "dev_b": 0.1})


def test_candidate_zero_cost_row_scores_zero_when_unsolved_and_raises_when_solved(world) -> None:  # noqa: F811
    evo = ready(world)
    evo.cascade(evo.seed)
    world.costs["dev_a"] = 0.0
    unsolved = child(evo.seed, tag="unsolved0", dev_a="0", dev_b="1", cheap_a="1", test_a="0")
    score, _info = evo.evaluate(unsolved, "dev_a")
    assert score == 0.0
    with pytest.raises(EvolveError):
        evo.evaluate(child(evo.seed, tag="solved0", dev_a="1", dev_b="1", cheap_a="1", test_a="0"), "dev_a")


def test_reused_candidate_rows_with_zero_charge_are_scored_on_total_cost(world, monkeypatch) -> None:  # noqa: F811
    evo = ready(world)
    real = run.run_plan

    def reused(*args, **kwargs):
        rows = real(*args, **kwargs)
        for found in rows:
            found["charged_usd"] = 0.0
            found["reused"] = True
        return rows

    monkeypatch.setattr(run, "run_plan", reused)
    assert evo.cascade(evo.seed).scores == pytest.approx({"dev_a": 1.0, "dev_b": 1.0})


# --- cost pairs for reflection (AC-10) ---


def test_paid_side_info_pairs_candidate_and_baseline_cost_rows_for_the_task(world) -> None:  # noqa: F811
    evo = ready(world)
    _score, info = evo.evaluate(evo.seed, "dev_a")

    pairs = info["cost_pairs"]
    assert pairs["candidate"] and pairs["baseline"]
    for summary in (*pairs["candidate"], *pairs["baseline"]):
        assert isinstance(summary["total_cost_usd"], float)
        assert summary["scored_cost_usd"] == summary["total_cost_usd"]
        assert set(summary) <= {*COST_SUMMARY_FIELDS, "scored_cost_usd"}
        # No trajectory and no grader output reach reflection through the pairs.
        assert not {"trajectory_path", "result_text", "correct", "correctness_reason"} & set(summary)
    assert {summary["repetition"] for summary in pairs["baseline"]} == set(evo._reps())


def test_a_cost_summary_carries_the_charged_cost_of_a_failed_row_with_no_total() -> None:
    summary = cost_summary(row("t", False, charged_usd=0.3))

    assert summary["scored_cost_usd"] == 0.3 and "total_cost_usd" not in summary


def test_reflection_and_proposer_receive_the_score_inputs_and_each_tasks_cost_pairs(world) -> None:  # noqa: F811
    evo = ready(world)
    _score, dev_a = evo.evaluate(evo.seed, "dev_a")
    _score, dev_b = evo.evaluate(evo.seed, "dev_b")
    seen: dict[str, list[dict]] = {}
    dispatcher = engine.Dispatcher(
        engine.StopState(),
        reflect=lambda name, text, records: seen.setdefault(name, records) and text,
        propose=lambda _candidate, records: seen.setdefault("src_patch", records) and "patch",
    )
    names = ["prompts/mcp.md", "src_patch"]

    dispatcher(seed_candidate(), {name: [dev_a, dev_b, dev_a] for name in names}, names)

    expected_scores = {key: dev_a[key] for key in ("task_scores", "pass_rate", "mean_cost", "baseline_cost")}
    for name in names:
        records = seen[name]
        assert [record["scores"] for record in records if "scores" in record] == [expected_scores]
        pairs = [record["cost_pairs"] for record in records if "cost_pairs" in record]
        assert [pair["task"] for pair in pairs] == ["dev_a", "dev_b"]
        assert pairs[0]["baseline"] == dev_a["cost_pairs"]["baseline"] and pairs[0]["baseline"]
        assert pairs[1]["candidate"] == dev_b["cost_pairs"]["candidate"] and pairs[1]["candidate"]


def test_reflection_and_proposer_prompts_state_the_cost_objective() -> None:
    assert candidate.OBJECTIVE in engine.reflection_prompt("", [])
    assert candidate.OBJECTIVE in proposer.proposer_prompt([])
    assert "lower cost" in engine.REFLECTION_HEADER and "lower cost" in proposer.PROPOSER_HEADER
    assert "more effectively" not in engine.REFLECTION_HEADER
    assert "correctly more often" not in proposer.PROPOSER_HEADER


# --- buy_baselines / stop ---


def test_stop_inside_buy_baselines_buys_no_candidate_cell_and_finish_does_not_crash(world) -> None:  # noqa: F811
    evo = build(world, max_usd="0.9", calls="6")
    evo.base_cost = {}
    assert evo.preflight() is None
    assert evo.buy_baselines() is None
    assert evo.stop.is_set and evo.base_cost == {}

    fresh = child(evo.seed, tag="late", dev_a="1", dev_b="1", cheap_a="1", test_a="0")
    score, info = evo.evaluate(fresh, "dev_a")
    assert score == 0.0 and "stopped" in info
    assert tilth_calls(world) == []
    assert evo.finish() is None
    assert world.pr.calls == [] and tilth_calls(world) == []


def test_finish_under_a_no_spend_stop_with_an_empty_baseline_store_fails_cleanly(world) -> None:  # noqa: F811
    evo = ready(world)
    accept(evo, evo.seed, {"dev_a": 0.0, "dev_b": 0.0})
    accept(evo, child(evo.seed, test_a="1", tag="w"), {"dev_a": 0.9, "dev_b": 0.9})
    evo.stop.set("quota")
    if world.store_path.exists():
        world.store_path.write_text("")
    try:
        result = evo.finish()
    except EvolveError as error:
        pytest.fail(f"finish raised instead of failing cleanly: {error}")
    assert result is None and world.pr.calls == []


# --- finish ---


def _winner_world(monkeypatch, tmp_path, scale: float = 0.5):
    world = three_test_task_world(monkeypatch, tmp_path, seed_test="1")
    evo = ready(world)
    accept(evo, evo.seed, {"dev_a": 0.0, "dev_b": 0.0})
    winner = accept(evo, child(evo.seed, test_a="11", test_b="11", test_c="11", tag="w"),
                    {"dev_a": 0.9, "dev_b": 0.9})
    world.sha_scale[winner.sha] = scale
    return world, evo, winner


def test_finish_with_a_test_task_present_on_only_the_winner_side_compares_the_paired_tasks(monkeypatch, tmp_path) -> None:
    world, evo, winner = _winner_world(monkeypatch, tmp_path)
    real = evo._plan

    def plan(cells, sha, **kwargs):
        rows = real(cells, sha, **kwargs)
        return [found for found in rows if found["task"] != "test_c"] if sha == evo.seed_sha_for_test else rows

    evo.seed_sha_for_test = evo.settings.seed_sha
    evo._plan = plan
    evo.finish()
    text = log_text(world)
    assert "finish: incomplete" not in text
    # The unpaired task is dropped by name, not skipped silently.
    assert "2 tasks; dropped incomplete: test_c)" in text


def test_finish_with_no_paired_test_tasks_opens_no_pr(monkeypatch, tmp_path) -> None:
    world, evo, winner = _winner_world(monkeypatch, tmp_path)
    real = evo._plan

    def plan(cells, sha, **kwargs):
        rows = real(cells, sha, **kwargs)
        return [] if sha == winner.sha else rows

    evo._plan = plan
    assert evo.finish() is None
    assert world.pr.pushes == []
