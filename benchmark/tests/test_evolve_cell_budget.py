"""``--cell-budget-usd``: one per-cell Claude budget keys, caps, and estimates every evolve cell."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import baselines
import run
from evolve import cli, engine
from evolve_support import (
    MODEL, TEST, build, child, deps, evolve_args, main, make_world, mcp, scripted_engine, seed_candidate, seed_files,
)
from spend import SpendLedger

WINNER = child(seed_candidate(), dev_a="1", dev_b="1", cheap_a="1", test_a="1", tag="winner")


def weak_world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A world whose seed fails every dev task, so the scripted child wins dev and test and finish runs."""
    world = make_world(monkeypatch, tmp_path / "weak", seed_files(mcp(dev_a="0", dev_b="0", cheap_a="1", test_a="0")))
    fake, _seen = scripted_engine([WINNER])
    monkeypatch.setattr(engine, "optimize_anything", fake)
    return world


def record_argv(world, monkeypatch: pytest.MonkeyPatch) -> list[tuple[run.CellSpec, list[str]]]:
    """Wrap the stub runner so each cell also builds its real runner argv from the budget ``run_plan`` passed."""
    seen: list[tuple[run.CellSpec, list[str]]] = []

    def recording(task_name, mode_name, model_name, repetition, **kwargs):
        argv, _config = run._runner_command(
            run.TASKS[task_name], run.MODES[mode_name], mode_name, model_name, world.tmp,
            bare=kwargs["bare"], reasoning_effort=kwargs.get("reasoning_effort"),
            max_budget_usd=kwargs["max_budget_usd"], strict_file_tools=kwargs.get("strict_file_tools", False),
            skill_paths=[])
        seen.append((run.CellSpec(task_name, mode_name, model_name, repetition), argv))
        return world.fake_run_single(task_name, mode_name, model_name, repetition, **kwargs)

    monkeypatch.setattr(run, "run_single", recording)
    return seen


def budget_arg(argv: list[str]) -> str:
    return argv[argv.index("--max-budget-usd") + 1]


def default_identity(cell: run.CellSpec) -> dict:
    """The run key every evolve cell had before ``--cell-budget-usd``: the old ``planned_identity`` body."""
    return run.cell_identity(cell.task, cell.mode, cell.model, cell.repetition, bare=True, reasoning_effort=None,
                             max_budget_usd=run.DEFAULT_MAX_BUDGET_USD, strict_file_tools=False)


def stored_cells(world) -> list[tuple[run.CellSpec, dict]]:
    return [(run.CellSpec(row["task"], row["mode"], MODEL, row["repetition"]), row) for row in world.stored()]


def without_estimate(world, *extra: str, max_usd: str = "100") -> list[str]:
    """Evolve argv with no ``--cell-estimate-usd``, so the estimate takes its default."""
    args = evolve_args(world, *extra, max_usd=max_usd)
    at = args.index("--cell-estimate-usd")
    return args[:at] + args[at + 2:]


# --- the budget reaches every runner call and every key ---


def test_cell_budget_reaches_every_runner_argv_and_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = weak_world(monkeypatch, tmp_path)
    seen = record_argv(world, monkeypatch)
    evo = build(world, "--cell-budget-usd", "3")

    assert evo.run() == 0

    assert world.pr.creates, "finish opened no PR, so the test split was not scored"
    modes = {(cell.mode, cell.task in TEST) for cell, _argv in seen}
    assert modes == {("baseline", False), ("baseline", True), ("tilth_forced", False), ("tilth_forced", True)}
    assert {budget_arg(argv) for _cell, argv in seen} == {"3.0000"}
    rows = stored_cells(world)
    assert len(rows) == len(seen)
    for cell, row in rows:
        planned = run.planned_identity(cell, max_budget_usd=3.0)
        assert row["max_budget_usd"] == 3.0 and row["applied_max_budget_usd"] == 3.0
        assert row["harness_digest"] == planned["harness_digest"] != default_identity(cell)["harness_digest"]
        if cell.mode == "baseline":
            assert row["run_key"] == planned["run_key"]


def test_omitted_cell_budget_keeps_todays_run_keys(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = weak_world(monkeypatch, tmp_path)
    seen = record_argv(world, monkeypatch)

    assert main(world) == 0

    assert {budget_arg(argv) for _cell, argv in seen} == {"1.0000"}
    for cell, row in stored_cells(world):
        assert row["max_budget_usd"] == run.DEFAULT_MAX_BUDGET_USD
        assert row["harness_digest"] == default_identity(cell)["harness_digest"]
        if cell.mode == "baseline":
            assert row["run_key"] == default_identity(cell)["run_key"]


def test_baselines_bought_at_another_budget_are_not_reused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = weak_world(monkeypatch, tmp_path)
    assert main(world) == 0
    bought = len(world.runner_calls("baseline"))

    assert main(world, "--cell-budget-usd", str(run.DEFAULT_MAX_BUDGET_USD), "--run-id", "same") == 0
    assert len(world.runner_calls("baseline")) == bought

    assert main(world, "--cell-budget-usd", "3", "--run-id", "larger") == 0
    assert len(world.runner_calls("baseline")) == 2 * bought


# --- invalid budgets are refused before any paid call ---


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_invalid_cell_budget_refused_before_any_paid_call(world_factory, value: str,
                                                          capsys: pytest.CaptureFixture[str]) -> None:
    world = world_factory()
    auth_calls: list[list[str]] = []

    assert main(world, "--cell-budget-usd", value, auth_spawn=lambda argv, **_kwargs: auth_calls.append(argv)) == 2

    assert world.calls == [] and world.judge.labelled == [] and world.spawned == [] and auth_calls == []
    assert "--cell-budget-usd must be a positive finite number" in capsys.readouterr().err


@pytest.fixture
def world_factory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    return lambda: make_world(monkeypatch, tmp_path)


# --- estimates and the finish reserve follow the budget ---


def test_estimate_and_finish_reserve_follow_a_larger_budget(world_factory) -> None:
    world = world_factory()
    evo = cli.build(without_estimate(world, "--cell-budget-usd", "3"), **deps(world))

    assert evo.settings.cell_budget_usd == 3.0 and evo.settings.cell_estimate_usd == 3.0
    # test_a x (reruns 1 + 1) reps x (candidate + unstored baseline) cells, each at the $3 fallback.
    assert evo.finalist_test_cost() == pytest.approx(len(TEST) * 2 * 2 * 3.0)
    assert evo.preflight() is None
    assert evo.ledger.reserve == pytest.approx(2 * len(TEST) * 2 * 2 * 3.0)


def test_explicit_estimate_still_wins_over_the_budget(world_factory) -> None:
    world = world_factory()
    evo = build(world, "--cell-budget-usd", "3")

    assert evo.settings.cell_budget_usd == 3.0 and evo.settings.cell_estimate_usd == 0.1


def test_finish_reserve_counts_baselines_stored_at_another_budget(world_factory) -> None:
    world = world_factory()
    cells = [run.CellSpec(task, "baseline", MODEL, rep) for task in TEST for rep in (0, 1)]
    run.run_plan(cells, panel=world.panel, ledger=SpendLedger(100.0), candidate_sha=None, refreeze_baselines=False,
                 repo=world.repo)
    stored = baselines.completed_by_key(world.stored())
    assert all(run.planned_identity(cell)["run_key"] in stored for cell in cells)

    at_default = build(world)
    at_three = build(world, "--cell-budget-usd", "3", "--run-id", "run2")

    # Stored $1 baselines answer the default run but not the $3 run, so only the $3 reserve buys them again.
    assert at_three.finalist_test_cost() - at_default.finalist_test_cost() == pytest.approx(len(cells) * world.cost)


# --- run_plan caps a cell at the run's budget, not at the default ---


def test_run_plan_caps_at_the_budget_and_stops_on_a_headroom_cap(world_factory,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    world = world_factory()
    budgets: list[float] = []

    def recording(*args, **kwargs):
        budgets.append(kwargs["max_budget_usd"])
        return world.fake_run_single(*args, **kwargs)

    monkeypatch.setattr(run, "run_single", recording)
    [ample] = run.run_plan([run.CellSpec("dev_a", "baseline", MODEL, 0)], panel=world.panel,
                           ledger=SpendLedger(100.0), candidate_sha=None, refreeze_baselines=False,
                           max_budget_usd=3.0, repo=world.repo)
    [capped] = run.run_plan([run.CellSpec("dev_b", "baseline", MODEL, 0)], panel=world.panel,
                            ledger=SpendLedger(10.0, reserve=8.0), candidate_sha=None, refreeze_baselines=False,
                            max_budget_usd=3.0, cell_estimate_usd=0.1, repo=world.repo)

    assert budgets == [3.0, pytest.approx(2.0)]
    assert ample["applied_max_budget_usd"] == 3.0 and capped["applied_max_budget_usd"] == pytest.approx(2.0)
    assert ample["max_budget_usd"] == capped["max_budget_usd"] == 3.0

    def hits_cap(*args, **kwargs):
        raise RuntimeError("claude -p did not complete successfully: error_max_budget_usd")

    monkeypatch.setattr(run, "run_single", hits_cap)
    with pytest.raises(run.PlanStopped) as stopped:
        run.run_plan([run.CellSpec("dev_a", "baseline", MODEL, 1)], panel=world.panel,
                     ledger=SpendLedger(10.0, reserve=8.0), candidate_sha=None, refreeze_baselines=False,
                     max_budget_usd=3.0, cell_estimate_usd=0.1, repo=world.repo)
    assert stopped.value.reason == "ceiling"


def test_run_plan_estimate_defaults_to_the_budget(world_factory) -> None:
    world = world_factory()
    with pytest.raises(run.PlanStopped) as stopped:
        run.run_plan([run.CellSpec("dev_a", "baseline", MODEL, 0)], panel=world.panel, ledger=SpendLedger(2.5),
                     candidate_sha=None, refreeze_baselines=False, max_budget_usd=3.0, repo=world.repo)

    assert stopped.value.reason == "ceiling" and "$3.0000 estimated" in str(stopped.value)
    assert world.calls == []
