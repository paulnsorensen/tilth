"""One bad cell cannot void a paid run.

A cell that spent its own Claude budget cap is a completed, incorrect row. A cell that did not complete is
retried a bounded number of times, each attempt charged. Finish compares the finalists on the test tasks where
every cell completed, and a resumed run re-runs only the cells that did not complete.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import baselines
import run
from evolve import candidate as candidates
from evolve import cli, engine
from evolve_support import (
    MODEL, EvolveTask, build, child, deps, evolve_args, fixture_panel, log_text, main, make_world, mcp,
    scripted_engine, seed_candidate, seed_files,
)
from spend import SpendLedger


def ready(world, *extra: str):
    evo = build(world, *extra)
    assert evo.preflight() is None
    assert evo.buy_baselines() is None
    return evo


def accept(evo, candidate: dict, means: dict[str, float]):
    result = evo.results.get(candidates.content_id(candidate)) or evo.new_cascade(candidate)
    result.sha = evo.materializer.materialize(candidate)
    result.just_check_ok = True
    result.scores = dict(means)
    result.means = dict(means)
    result.accepted = True
    evo.frontier.append(result)
    return result


def calls_of(world, task: str, sha: str | None = None) -> list[int]:
    """The repetitions of every tilth-arm runner call for ``task``, one entry per attempt."""
    return [rep for name, mode, rep, called in world.calls
            if name == task and mode == "tilth_forced" and (sha is None or called == sha)]


# --- a budget-capped cell is a completed failure ---


def test_budget_capped_cell_is_a_completed_failure_and_is_reused(monkeypatch: pytest.MonkeyPatch,
                                                                tmp_path: Path) -> None:
    world = make_world(monkeypatch, tmp_path)
    world.baseline["dev_a"] = "B"
    cells = [run.CellSpec("dev_a", "baseline", MODEL, 0)]
    ledger = SpendLedger(100.0)

    [row] = run.run_plan(cells, panel=world.panel, ledger=ledger, candidate_sha=None, refreeze_baselines=False,
                         repo=world.repo)

    assert row["budget_capped"] is True and row["correct"] is False
    assert row["error"].startswith("claude -p failed with code 1")
    assert baselines.is_completed(row)
    assert (row["total_cost_usd"], row["cost_source"]) == (world.cost, "native")
    assert ledger.spent == pytest.approx(world.cost)

    [again] = run.run_plan(cells, panel=world.panel, ledger=ledger, candidate_sha=None, refreeze_baselines=False,
                           repo=world.repo)
    assert again["reused"] is True and len(world.calls) == 1


def test_a_cap_shrunk_to_the_headroom_is_not_the_agents_own(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = make_world(monkeypatch, tmp_path)
    world.baseline["dev_a"] = "B"

    with pytest.raises(run.PlanStopped) as stopped:
        run.run_plan([run.CellSpec("dev_a", "baseline", MODEL, 0)], panel=world.panel,
                     ledger=SpendLedger(10.0, reserve=8.0), candidate_sha=None, refreeze_baselines=False,
                     max_budget_usd=3.0, cell_estimate_usd=0.1, repo=world.repo)

    assert stopped.value.reason == "ceiling"
    [row] = world.stored()
    assert "budget_capped" not in row and not baselines.is_completed(row)


def test_budget_capped_dev_cell_scores_as_a_failure_not_an_incomplete_rollout(monkeypatch: pytest.MonkeyPatch,
                                                                               tmp_path: Path) -> None:
    world = make_world(monkeypatch, tmp_path, seed_files(mcp(dev_a="1", dev_b="B", cheap_a="1", test_a="0")))
    evo = ready(world)

    score, _info = evo.evaluate(evo.seed, "dev_b")
    seed = evo.results[evo.seed_id]

    assert seed.accepted and seed in evo.frontier
    assert score == 0.0 and seed.means["dev_b"] == 0.0 and seed.means["dev_a"] > 0
    # Admission re-ran it at rep 1 like any completed cell; no retry was spent on it.
    assert calls_of(world, "dev_b") == [0, 1]
    assert "cells: retry" not in log_text(world)
    [dev_report] = [report for report in evo.deltas if report["split"] == "dev"]
    assert dev_report["paired_tasks"] == 2


# --- an incomplete cell is retried, charged, and capped ---


def test_incomplete_cell_is_retried_and_each_attempt_is_charged(monkeypatch: pytest.MonkeyPatch,
                                                                tmp_path: Path) -> None:
    world = make_world(monkeypatch, tmp_path, seed_files(mcp(dev_a="F", dev_b="1", cheap_a="1", test_a="0")))
    evo = ready(world)
    world.flaky = 1

    evo.evaluate(evo.seed, "dev_a")
    seed = evo.results[evo.seed_id]

    assert calls_of(world, "dev_a") == [0, 0, 1]
    assert seed.accepted and all(baselines.is_completed(row) for row in seed.rows)
    assert "cells: retry 1/2 of 1 incomplete cell(s): dev_a/tilth_forced/rep0" in log_text(world)
    attempts = [row for row in world.stored()
                if (row["task"], row["mode"], row["repetition"]) == ("dev_a", "tilth_forced", 0)]
    assert [baselines.is_completed(row) for row in attempts] == [False, True]
    assert attempts[0]["charged_usd"] > 0
    assert evo.ledger.spent == pytest.approx(sum(row["charged_usd"] for row in world.stored()))


@pytest.mark.parametrize(("flag", "attempts"), [((), 3), (("--cell-retries", "0"), 1), (("--cell-retries", "4"), 5)])
def test_retries_stop_at_the_bound(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, flag: tuple[str, ...],
                                   attempts: int) -> None:
    world = make_world(monkeypatch, tmp_path, seed_files(mcp(dev_a="F", dev_b="1", cheap_a="1", test_a="0")))
    evo = ready(world, *flag)
    world.flaky = 100

    evo.evaluate(evo.seed, "dev_a")

    assert calls_of(world, "dev_a") == [0] * attempts
    assert not evo.results[evo.seed_id].accepted
    assert "a first rollout did not complete; not re-run" in log_text(world)
    assert world.flaky == 100 - attempts


@pytest.mark.parametrize("value", ["-1", "6"])
def test_cell_retries_out_of_range_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    world = make_world(monkeypatch, tmp_path)

    assert cli.build(evolve_args(world, "--cell-retries", value), **deps(world)) == 2
    assert "--cell-retries from 0 to 5" in capsys.readouterr().err
    assert world.calls == []


# --- finish compares paired completed test tasks ---


def two_test_world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, seed_test_b: str):
    world = make_world(monkeypatch, tmp_path,
                       seed_files(mcp(dev_a="1", dev_b="1", cheap_a="1", test_a="0", test_b=seed_test_b)))
    monkeypatch.setitem(run.TASKS, "test_b", EvolveTask(name="test_b"))
    world.panel = fixture_panel(test=("test_a", "test_b"))
    return world


@pytest.mark.parametrize("incomplete", ["winner", "seed"])
def test_finish_compares_the_paired_completed_tasks_after_retries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                                  incomplete: str) -> None:
    world = two_test_world(monkeypatch, tmp_path, "E" if incomplete == "seed" else "0")
    evo = ready(world)
    seed = accept(evo, evo.seed, {"dev_a": 0.0, "dev_b": 0.0})
    winner = accept(evo, child(evo.seed, test_a="1", test_b="E" if incomplete == "winner" else "0"),
                    {"dev_a": 0.9, "dev_b": 0.9})
    broken = winner if incomplete == "winner" else seed

    assert evo.finish() is not None

    assert evo.finish_failure is None and world.pr.pushes == [(winner.sha, "evolve/run1-winner")]
    # Each of the two test_b reps ran once and was retried twice.
    assert sorted(calls_of(world, "test_b", broken.sha)) == [0, 0, 0, 1, 1, 1]
    log = log_text(world)
    assert (f"finish: dropped test task(s) test_b: cells did not complete after 2 retries: "
            f"test_b/{broken.cid[:12]}/rep0, test_b/{broken.cid[:12]}/rep1") in log
    assert "(95% CI [" in log and "1 tasks; dropped incomplete: test_b)" in log
    assert "dropped incomplete: test_b" in world.pr.creates[0]["body"]
    [test_report] = [report for report in evo.deltas if report["split"] == "test"]
    assert test_report["paired_tasks"] == 1


def test_a_dropped_task_cannot_make_a_winner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = two_test_world(monkeypatch, tmp_path, "0")
    evo = ready(world)
    accept(evo, evo.seed, {"dev_a": 0.0, "dev_b": 0.0})
    # The winner ties the seed on the one task that completed; its broken task is not a win.
    accept(evo, child(evo.seed, test_a="0", test_b="E"), {"dev_a": 0.9, "dev_b": 0.9})

    assert evo.finish() is None

    assert evo.finish_failure is None and world.pr.calls == []
    assert "dropped incomplete: test_b" in log_text(world)
    assert "finish: no improvement (the paired test-split interval does not exclude 0)" in log_text(world)


# --- a resumed run re-runs only the incomplete cells ---


def test_resume_reuses_completed_and_budget_capped_rows_and_reruns_only_incomplete_cells(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    files = seed_files(mcp(dev_a="0", dev_b="B", cheap_a="1", test_a="0"))
    world = make_world(monkeypatch, tmp_path, files)
    fake, _seen = scripted_engine([child(seed_candidate(files), dev_a="1", dev_b="1", cheap_a="1", test_a="F1",
                                         tag="winner")])
    monkeypatch.setattr(engine, "optimize_anything", fake)
    # The winner's test_a rep 0 crashes on its first attempt and on both retries.
    world.flaky = 3

    assert main(world) == 1
    assert "no test task has complete paired rows" in log_text(world)
    first = list(world.calls)
    test_rep0 = [sha for task, mode, rep, sha in first if (task, mode, rep) == ("test_a", "tilth_forced", 0)]
    [winner_sha] = [sha for sha in set(test_rep0) if test_rep0.count(sha) == 3]
    # The seed's budget-capped dev_b cells ran once each: a completed row is never retried.
    assert sorted(rep for task, mode, rep, sha in first
                  if (task, mode) == ("dev_b", "tilth_forced") and sha != winner_sha) == [0, 1]

    assert main(world) == 0

    assert world.calls[len(first):] == [("test_a", "tilth_forced", 0, winner_sha)]
    assert world.pr.pushes == [(winner_sha, "evolve/run1-winner")]
