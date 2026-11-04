"""Evolve search budget: an accurate finish reserve, a preamble slot, and a more aggressive selector.

No test calls a model: cells come from the stub runner in ``evolve_support``.
"""

import json
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import run
from evolve import attribution, diagnosis, directives
from evolve import candidate as candidates
from evolve_support import MODEL, TEST, build, log_text, main, make_world, seed_candidate, seed_files
from spend import SpendLedger
from test_evolve import world  # noqa: F401  (world is a fixture)

SRC = candidates.SRC_PATCH
PREAMBLE = candidates.PREAMBLE
READ_MD = "prompts/tools/read.md"
WRITE_MD = "prompts/tools/write.md"
SEED_PREAMBLE = "# tilth agent preamble\n\n## Tools\n\nSEED-PREAMBLE\n\n## Safety\n\n## Verification\n"
SHIPPED = Path(__file__).resolve().parents[1] / "evolve" / "directives" / "preamble-r2.json"
REPS = (0, 1)  # evolve_args runs --reruns 1


def preamble_seed() -> dict[str, str]:
    return {**seed_candidate(), PREAMBLE: SEED_PREAMBLE}


@pytest.fixture
def preamble_world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    return make_world(monkeypatch, tmp_path, {**seed_files(), PREAMBLE: SEED_PREAMBLE})


def store_test_cells(world, mode: str, *, sha: str | None = None, preamble: bool = False) -> None:
    cells = [run.CellSpec(task, mode, MODEL, rep) for task in TEST for rep in REPS]
    run.run_plan(cells, panel=world.panel, ledger=SpendLedger(100.0), candidate_sha=sha, refreeze_baselines=False,
                 repo=world.repo, preamble=preamble)


# --- the finish reserve counts only the cells finish buys ---


def test_the_reserve_excludes_stored_seed_and_baseline_test_cells(world) -> None:
    store_test_cells(world, "baseline")
    store_test_cells(world, "tilth_forced", sha=world.seed_sha)
    evo = build(world)

    parts = evo.finish_reserve()

    # The winner's test_a x 2 reps at the stored $0.10 mean; finish answers the seed and baseline from the store.
    assert parts["seed"] == 0.0 and parts["baseline"] == 0.0
    assert parts["winner"] == pytest.approx(len(TEST) * len(REPS) * 0.1)
    assert parts["old"] == pytest.approx(2 * len(TEST) * len(REPS) * 0.1)
    assert evo.preflight() is None
    assert evo.ledger.reserve == pytest.approx(parts["winner"])
    assert "reserve: $0.2000 held for finish (winner $0.2000 + seed $0.0000 + baseline $0.0000; " \
           "old estimate $0.4000)" in log_text(world)


def test_the_reserve_counts_each_unstored_cell_once(world) -> None:
    evo = build(world)

    parts = evo.finish_reserve()

    cells = len(TEST) * len(REPS) * 0.1
    assert (parts["winner"], parts["seed"], parts["baseline"]) == pytest.approx((cells, cells, cells))
    assert parts["total"] == pytest.approx(3 * cells)
    assert parts["old"] == pytest.approx(4 * cells)


def test_a_preamble_run_reuses_only_seed_cells_stored_under_the_seed_preamble(preamble_world) -> None:
    store_test_cells(preamble_world, "tilth_forced", sha=preamble_world.seed_sha, preamble=False)
    evo = build(preamble_world, "--preamble")
    # Rows without the preamble have another run key, so finish would buy the seed's cells again.
    assert evo.finish_reserve()["seed"] == pytest.approx(len(TEST) * len(REPS) * 0.1)

    store_test_cells(preamble_world, "tilth_forced", sha=preamble_world.seed_sha, preamble=True)
    assert evo.finish_reserve()["seed"] == 0.0


def test_a_seed_that_cannot_be_keyed_counts_in_full(world, monkeypatch: pytest.MonkeyPatch) -> None:
    store_test_cells(world, "tilth_forced", sha=world.seed_sha)
    evo = build(world)
    monkeypatch.setattr(run, "_CANDIDATE_BUILDS", {})
    monkeypatch.setattr(run, "_build_candidate", lambda sha, repo=None: (_ for _ in ()).throw(RuntimeError("no cargo")))

    assert evo.finish_reserve()["seed"] == pytest.approx(len(TEST) * len(REPS) * 0.1)
    assert "the seed's test cells count in full" in log_text(world)


# --- --finish-reserve-usd ---


def test_the_reserve_override_is_held_and_logged_beside_the_computed_reserve(world) -> None:
    evo = build(world, "--finish-reserve-usd", "0.3")

    assert evo.preflight() is None

    assert evo.ledger.reserve == pytest.approx(0.3)
    assert "reserve: $0.3000 held for finish (--finish-reserve-usd; computed $0.6000; old estimate $0.8000)" \
           in log_text(world)


def test_an_override_below_the_winner_floor_is_refused_before_any_paid_call(world, capsys) -> None:
    assert main(world, "--finish-reserve-usd", "0.19") == 2

    assert world.calls == [] and world.judge.labelled == [] and world.spawned == []
    assert "--finish-reserve-usd $0.19 is below the safe floor $0.20" in capsys.readouterr().err
    assert build(world, "--finish-reserve-usd", "0.2", "--run-id", "at-floor").preflight() is None


@pytest.mark.parametrize("value", ["-1", "nan", "inf"])
def test_a_malformed_override_is_a_usage_error(world, capsys, value: str) -> None:
    assert main(world, "--finish-reserve-usd", value) == 2

    assert world.calls == [] and world.judge.labelled == []
    assert "--finish-reserve-usd must be a finite number of at least 0" in capsys.readouterr().err


# --- the preamble gets an early slot ---


def heavy(component: str) -> list[dict]:
    return [{"cause": "c", "excess_chars_per_cell": 1e9, "evidence": "e", "component_hint": component}]


def queue_of(*items: tuple[str, str]) -> directives.DirectiveQueue:
    return directives.DirectiveQueue([directives.Directive(i, c, f"change {i}") for i, c in items], lambda line: None)


def picks(selector, candidate: dict, count: int) -> list[str]:
    return [selector(None, None, None, 0, candidate)[0] for _ in range(count)]


def test_a_preamble_run_proposes_the_preamble_by_proposal_three_whatever_the_hints() -> None:
    for seed in range(40):
        selector = diagnosis.WeightedSelector(lambda candidate: heavy(WRITE_MD), lambda line: None,
                                              random.Random(seed))
        assert PREAMBLE in picks(selector, preamble_seed(), 3), seed


def test_the_preamble_slot_follows_a_directive_and_the_src_patch_slot() -> None:
    """The preamble-r1 order: a read.md directive, then the src_patch deadline, then the preamble deadline."""
    selector = diagnosis.WeightedSelector(lambda candidate: heavy(SRC), lambda line: None, random.Random(0),
                                          directives=queue_of(("read-batch", READ_MD)))

    assert picks(selector, preamble_seed(), 3) == [READ_MD, SRC, PREAMBLE]


def test_a_directive_for_the_preamble_replaces_the_early_slot() -> None:
    logs: list[str] = []
    selector = diagnosis.WeightedSelector(lambda candidate: heavy(WRITE_MD), logs.append, random.Random(1),
                                          directives=queue_of(("a", READ_MD), ("b", READ_MD), ("c", READ_MD),
                                                              ("d", PREAMBLE)))

    chosen = picks(selector, preamble_seed(), 5)

    assert chosen[:4] == [READ_MD, READ_MD, READ_MD, PREAMBLE]
    assert chosen[4] == SRC  # src_patch keeps its deadline; the preamble had none to miss


def test_a_run_without_the_preamble_has_no_preamble_slot() -> None:
    selector = diagnosis.WeightedSelector(lambda candidate: heavy(WRITE_MD), lambda line: None, random.Random(2))

    assert PREAMBLE not in picks(selector, seed_candidate(), 12)


# --- the strict-hook denial cause maps to the preamble in preamble mode only ---


def denial_cell(tmp_path: Path, name: str, denials: int, ok_bash: int = 0) -> dict:
    path = tmp_path / f"{name}.trajectory.jsonl"
    calls = [{"tool_use_id": f"t{i}", "name": "Bash", "api_turn": i, "is_error": True,
              "input": {"command": "git status && grep -rn X ."},
              "output": "PreToolUse:Bash hook error: [tilth hook --hint tilth]: Blocked: shell `grep` searches files."}
             for i in range(denials)]
    calls += [{"tool_use_id": f"ok{i}", "name": "Bash", "api_turn": denials, "is_error": False,
               "input": {"command": "git status"}, "output": "clean"} for i in range(ok_bash)]
    calls.append({"tool_use_id": "read", "name": "mcp__tilth__tilth_read", "api_turn": denials, "is_error": False,
                  "input": {"paths": ["src/lib.rs"]}, "output": "x" * 400})
    path.write_text("\n".join(json.dumps(call) for call in calls) + "\n")
    return {"task": "t", "contaminated": False, "trajectory_path": str(path)}


def test_extra_hook_denials_name_the_preamble_only_in_a_preamble_run(tmp_path: Path) -> None:
    candidate = [denial_cell(tmp_path, "cand", 3)]
    baseline = [denial_cell(tmp_path, "base", 1)]

    preamble = attribution.summarize(candidate, baseline, ["t"], hints=attribution.PREAMBLE_HINTS)
    plain = attribution.summarize(candidate, baseline, ["t"])

    found = {d["cause"]: d for d in preamble["dominant"]}
    assert found["hook_denials"]["component_hint"] == PREAMBLE
    assert found["hook_denials"]["excess_chars_per_cell"] > 0
    assert "3.00 per cell vs baseline 1.00" in found["hook_denials"]["evidence"]
    assert "hook_denials" not in {d["cause"] for d in plain["dominant"]}
    assert [d["cause"] for d in plain["dominant"]] == [d["cause"] for d in preamble["dominant"]
                                                       if d["cause"] != "hook_denials"]
    assert {e["class"] for e in plain["errors"] if e["tool"] == "Bash"} == {"hook_denied"}


def test_fewer_hook_denials_than_the_baseline_are_not_a_cause(tmp_path: Path) -> None:
    summary = attribution.summarize([denial_cell(tmp_path, "cand", 1)], [denial_cell(tmp_path, "base", 2)], ["t"],
                                    hints=attribution.PREAMBLE_HINTS)

    assert "hook_denials" not in {d["cause"] for d in summary["dominant"]}


def test_non_preamble_hints_are_unchanged() -> None:
    assert attribution.HINTS == {
        "tilth_write_receipt": SRC, "tilth_read_full_mode": READ_MD, "tilth_read_batched_4plus": READ_MD,
        "search_output": SRC, "write_errors": WRITE_MD, "read_breadth": "prompts/mcp.md"}
    assert attribution.PREAMBLE_HINTS == {**attribution.HINTS, "read_breadth": PREAMBLE, "hook_denials": PREAMBLE}


# --- a more aggressive selector ---


def longest_wait(chosen: list[str], name: str) -> int:
    gap = longest = 0
    for pick in chosen:
        gap = 0 if pick == name else gap + 1
        longest = max(longest, gap)
    return longest


def test_the_default_bound_gives_every_preamble_component_a_turn_within_seven_proposals() -> None:
    assert diagnosis.MAX_UNSELECTED == 6
    for seed in range(20):
        selector = diagnosis.WeightedSelector(lambda candidate: heavy(READ_MD), lambda line: None,
                                              random.Random(seed))
        chosen = picks(selector, preamble_seed(), 80)
        assert all(longest_wait(chosen, name) <= 6 for name in preamble_seed()), (seed, chosen)


def test_the_bounds_are_flags_that_reach_the_selector(world) -> None:
    evo = build(world, "--max-unselected", "8", "--src-patch-by", "3")

    assert evo.selector.max_unselected == 8 and evo.selector.early[SRC] == 3
    default = build(world, "--run-id", "default")
    assert default.selector.max_unselected == 6 and default.selector.early[SRC] == 2


@pytest.mark.parametrize(("flags", "message"), [
    (("--max-unselected", "3"), "--max-unselected must be at least 4"),
    (("--src-patch-by", "0"), "--src-patch-by must be between 1 and --max-unselected + 1"),
    (("--src-patch-by", "8"), "--src-patch-by must be between 1 and --max-unselected + 1"),
])
def test_unsafe_bounds_are_refused(world, capsys, flags: tuple[str, str], message: str) -> None:
    assert main(world, *flags) == 2
    assert message in capsys.readouterr().err


def test_a_later_src_patch_deadline_holds() -> None:
    for seed in range(40):
        selector = diagnosis.WeightedSelector(lambda candidate: heavy(READ_MD), lambda line: None,
                                              random.Random(seed), src_patch_by=4)
        chosen = picks(selector, seed_candidate(), 4)
        assert SRC in chosen, (seed, chosen)


def cooling_selector(stages: list[str | None], logs: list[str]) -> diagnosis.WeightedSelector:
    return diagnosis.WeightedSelector(lambda candidate: heavy(SRC), logs.append, random.Random(0),
                                      stages=lambda: list(stages))


def test_a_component_whose_last_two_proposals_failed_just_check_rests() -> None:
    stages: list[str | None] = [None]  # the seed
    logs: list[str] = []
    selector = cooling_selector(stages, logs)
    parent = seed_candidate()

    chosen = []
    for proposal in range(1, 12):
        pick = selector(None, None, None, 0, parent)[0]
        chosen.append(pick)
        # Every src_patch child fails `just check` (the preamble-r1 tails); every text child passes.
        stages.append("just check" if pick == SRC else None)

    second = [i for i, pick in enumerate(chosen) if pick == SRC][1]
    assert chosen[second + 1:second + 1 + diagnosis.COOLDOWN_PROPOSALS].count(SRC) == 0, chosen
    assert SRC in chosen[second + 1 + diagnosis.COOLDOWN_PROPOSALS:], chosen
    assert any(f"{SRC} rests for proposals" in line and "failed just check" in line for line in logs)


def test_a_passing_child_resets_the_failure_streak() -> None:
    stages: list[str | None] = [None]
    selector = cooling_selector(stages, [])
    parent = seed_candidate()
    outcomes = iter(["just check", None, "just check"])

    for _ in range(12):
        pick = selector(None, None, None, 0, parent)[0]
        stages.append(next(outcomes, None) if pick == SRC else None)

    assert selector.resting == {}


def test_the_loop_feeds_cascade_stages_to_the_selector(world) -> None:
    evo = build(world)
    evo.new_cascade(seed_candidate())
    evo.new_cascade({**seed_candidate(), SRC: "diff"}).fail("just check", "tail")

    assert evo.selector.stages() == [None, "just check"]


# --- the preamble-r2 directives ---


def test_the_preamble_r2_directives_load_and_target_the_preamble() -> None:
    loaded = directives.load(SHIPPED, preamble_seed())

    assert [d.id for d in loaded] == ["preamble-tilth-not-bash", "preamble-script-files"]
    for d in loaded:
        assert d.component == PREAMBLE
        assert d.hypothesis["metric"] in diagnosis.TOOL_METRICS and d.hypothesis["scope"] == "Bash"
        assert "benchmark" not in d.brief.lower() and "panel" not in d.brief.lower()
    with pytest.raises(directives.DirectiveError, match="prompts/preamble.md"):
        directives.load(SHIPPED, seed_candidate())


def test_the_main_directive_hypothesis_is_measurable_from_attribution(tmp_path: Path) -> None:
    main_directive = directives.load(SHIPPED, preamble_seed())[0]
    parent = attribution.summarize([denial_cell(tmp_path, "p", 3)], [denial_cell(tmp_path, "b", 1)], ["t"])
    # The child keeps its git status calls; only the refused compound commands go.
    child = attribution.summarize([denial_cell(tmp_path, "c", 1, ok_bash=2)], [denial_cell(tmp_path, "b2", 1)], ["t"])

    status, observed = diagnosis.judge(main_directive.hypothesis, parent, child)

    assert status == "met" and observed["parent"] == 1.0 > observed["child"]
