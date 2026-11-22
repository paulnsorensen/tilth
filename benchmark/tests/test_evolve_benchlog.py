"""The append-only evolve benchmark log: one entry per finished run, and its light and dark SVG charts.

Every run here writes to a temp log; no test touches the committed ``benchmark/log/``.
"""

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import run
from evolve import benchlog, chart, engine
from evolve.gitops import GitError
from evolve.loop import EvolveError, Evolution
from evolve_support import (DEV, TEST, child, log_text, main, make_world, mcp, scripted_engine, seed_candidate,
                            seed_files)

ENTRY_KEYS = {
    "schema", "run_id", "recorded_at", "seed_sha", "tilth_version", "panel", "model", "model_id", "preamble",
    "stop_reason", "outcome", "seed_dev_mean", "best_cid", "best_dev_mean", "test_means", "paired_test_delta",
    "pass_rate_delta", "arms", "spend", "pr_url",
}


def weak_world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A seed that fails every dev and test task, so a child that passes them opens a draft PR."""
    return make_world(monkeypatch, tmp_path, seed_files(mcp(dev_a="0", dev_b="0", cheap_a="1", test_a="0")))


def use_engine(monkeypatch: pytest.MonkeyPatch, candidates: list[dict] = ()) -> None:
    fake, _seen = scripted_engine(list(candidates))
    monkeypatch.setattr(engine, "optimize_anything", fake)


def winning_child() -> dict:
    return child(seed_candidate(), dev_a="1", dev_b="1", cheap_a="1", test_a="1")


def entries(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def parses_as_svg(path: Path) -> bytes:
    data = path.read_bytes()
    assert ElementTree.fromstring(data).tag == "{http://www.w3.org/2000/svg}svg"
    return data


def run_entry(world, run_id: str = "run1") -> dict:
    """The entry that the run wrote to its own run directory."""
    return json.loads((run.RESULTS_DIR / "evolve" / run_id / "benchmark-entry.json").read_text())


def assert_no_round_trip_lines(world) -> None:
    """The run builds its entry from memory, so its log holds no line that only served a text round trip."""
    for line in log_text(world).splitlines():
        assert not line.startswith(("run: ", "frontier: ", "arms: ")), line


def test_a_draft_pr_run_appends_one_entry_and_draws_both_charts(monkeypatch: pytest.MonkeyPatch,
                                                               tmp_path: Path) -> None:
    world = weak_world(monkeypatch, tmp_path)
    use_engine(monkeypatch, [winning_child()])
    log = tmp_path / "bench" / "evolve.jsonl"

    assert main(world, "--benchmark-log", str(log)) == 0

    [entry] = entries(log)
    assert set(entry) == ENTRY_KEYS
    assert entry["schema"] == benchlog.SCHEMA == 2
    assert entry["run_id"] == "run1" and entry["seed_sha"] == world.seed_sha
    assert entry["recorded_at"].endswith("Z")
    assert entry["panel"] == "evolve-fixture" and entry["model"] == "sonnet5" and entry["preamble"] is False
    assert entry["outcome"] == "pr_opened" and entry["pr_url"] == "https://example.invalid/pr/1"
    assert entry["seed_dev_mean"] == 0.0
    assert entry["best_dev_mean"] == pytest.approx(1.0) and len(entry["best_cid"]) == 12
    assert entry["test_means"] == {entry["best_cid"]: 1.0, **{cid: 0.0 for cid in entry["test_means"]
                                                               if cid != entry["best_cid"]}}
    assert len(entry["test_means"]) == 2
    assert entry["paired_test_delta"] == {"point": 1.0, "low": 1.0, "high": 1.0, "tasks": 1}
    assert entry["pass_rate_delta"] == 1.0
    assert set(entry["spend"]) == {"cells", "judge", "reflection", "proposer", "total", "max_usd"}
    assert entry["spend"]["max_usd"] == 100.0 and entry["spend"]["total"] > 0
    # The fixture rows carry no tilth_version, so the entry records null rather than a guess.
    assert entry["tilth_version"] is None
    light, dark = parses_as_svg(log.with_suffix(".svg")), parses_as_svg(log.with_name("evolve-dark.svg"))
    assert light != dark and b"<title>run1: draft PR opened</title>" in light
    assert f"benchmark log: appended run1 to {log}" in log_text(world)
    assert run_entry(world) == entry
    assert_no_round_trip_lines(world)

    # A second run appends a line, keeps the first byte for byte, and redraws both charts.
    first_line = log.read_text()
    assert main(world, "--benchmark-log", str(log), "--run-id", "run2") == 0
    assert log.read_text().startswith(first_line)
    assert [entry["run_id"] for entry in entries(log)] == ["run1", "run2"]
    assert parses_as_svg(log.with_suffix(".svg")) != light
    assert parses_as_svg(log.with_name("evolve-dark.svg")) != dark


def test_a_no_improvement_run_appends_an_entry_with_no_test_split(monkeypatch: pytest.MonkeyPatch,
                                                                tmp_path: Path) -> None:
    world = make_world(monkeypatch, tmp_path)
    use_engine(monkeypatch)
    log = tmp_path / "evolve.jsonl"

    assert main(world, "--benchmark-log", str(log)) == 0

    [entry] = entries(log)
    assert entry["outcome"] == "no_improvement" and entry["pr_url"] is None
    assert entry["seed_dev_mean"] == pytest.approx(1.0)
    assert entry["best_cid"] is None and entry["best_dev_mean"] is None
    assert entry["test_means"] is None and entry["paired_test_delta"] is None and entry["pass_rate_delta"] is None
    assert entry["stop_reason"] == "metric-call budget"
    assert entry["spend"] is not None
    assert run_entry(world) == entry
    assert_no_round_trip_lines(world)


def test_an_incomplete_run_appends_an_entry_and_keeps_its_exit_code(monkeypatch: pytest.MonkeyPatch,
                                                                   tmp_path: Path) -> None:
    world = make_world(monkeypatch, tmp_path)
    use_engine(monkeypatch)
    real_search = Evolution.search

    def ceiling_search(self):
        self.stop.set("ceiling")
        return real_search(self)

    monkeypatch.setattr(Evolution, "search", ceiling_search)
    log = tmp_path / "evolve.jsonl"

    assert main(world, "--benchmark-log", str(log)) == 1

    [entry] = entries(log)
    assert entry["outcome"] == "incomplete" and entry["stop_reason"] == "ceiling"
    assert entry["pr_url"] is None and entry["paired_test_delta"] is None

    assert run_entry(world) == entry


def raise_git_error(*_args, **_kwargs):
    raise GitError(["push"], "rejected")


def raise_gh_error(*_args, **_kwargs):
    raise subprocess.CalledProcessError(1, ["gh", "pr", "create"], stderr="gh: no network")


def broken_search(_self):
    raise EvolveError("the seed is not on the accepted frontier")


@pytest.mark.parametrize(("fault", "outcome"), [("push", "refused"), ("create", "pr_failed"), ("run", "failed")])
def test_a_finish_fault_records_its_outcome_kind(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fault: str,
                                                outcome: str) -> None:
    world = weak_world(monkeypatch, tmp_path)
    use_engine(monkeypatch, [winning_child()])
    if fault == "push":
        monkeypatch.setattr(world.pr, "push", raise_git_error)
    elif fault == "create":
        monkeypatch.setattr(world.pr, "create_draft", raise_gh_error)
    else:
        monkeypatch.setattr(Evolution, "search", broken_search)
    log = tmp_path / "evolve.jsonl"

    assert main(world, "--benchmark-log", str(log)) == 1

    [entry] = entries(log)
    assert run_entry(world) == entry
    assert entry["outcome"] == outcome and entry["pr_url"] is None
    assert_no_round_trip_lines(world)
    if fault == "run":
        assert entry["test_means"] is None and entry["paired_test_delta"] is None and entry["best_cid"] is None
    else:
        assert entry["paired_test_delta"] == {"point": 1.0, "low": 1.0, "high": 1.0, "tasks": 1}
        assert entry["pass_rate_delta"] == 1.0 and len(entry["test_means"]) == 2


def test_the_entry_records_the_version_that_the_seed_build_reports(monkeypatch: pytest.MonkeyPatch,
                                                                   tmp_path: Path) -> None:
    world = weak_world(monkeypatch, tmp_path)
    use_engine(monkeypatch, [winning_child()])

    def versioned(task_name, mode_name, model_name, repetition, **kwargs):
        row = world.fake_run_single(task_name, mode_name, model_name, repetition, **kwargs)
        sha = run.MODES[mode_name].git_sha
        return {**row, "tilth_version": f"0.0.0-experimental+{sha[:7]}"} if sha else row

    monkeypatch.setattr(run, "run_single", versioned)
    log = tmp_path / "evolve.jsonl"

    assert main(world, "--benchmark-log", str(log)) == 0

    # Each candidate build reports its own commit; the entry keeps the seed's, with its build suffix.
    [entry] = entries(log)
    assert entry["outcome"] == "pr_opened"
    assert entry["tilth_version"] == f"0.0.0-experimental+{world.seed_sha[:7]}"

@pytest.mark.parametrize("failing", ["log", "chart"])
def test_a_log_or_chart_failure_keeps_the_exit_code_and_the_finish_result(monkeypatch: pytest.MonkeyPatch,
                                                                          tmp_path: Path, failing: str) -> None:
    world = weak_world(monkeypatch, tmp_path)
    use_engine(monkeypatch, [winning_child()])
    if failing == "log":
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("")
        log = blocker / "evolve.jsonl"
    else:
        log = tmp_path / "evolve.jsonl"

        def broken(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(benchlog, "redraw", broken)

    assert main(world, "--benchmark-log", str(log)) == 0

    text = log_text(world)
    assert "finish: draft PR opened from evolve/run1-winner" in text
    assert world.pr.pushes
    [failure] = [line for line in text.splitlines() if line.startswith("benchmark log: ")]
    assert failure.startswith(f"benchmark log: {'append' if failing == 'log' else 'redraw'} failed (")
    assert text.rstrip().splitlines()[-1] == failure


def sample_entry(run_id: str, recorded_at: str, **fields) -> dict:
    entry = dict.fromkeys(ENTRY_KEYS)
    return {**entry, "schema": 2, "run_id": run_id, "recorded_at": recorded_at, **fields}


SAMPLES = [
    sample_entry("b", "2026-11-02T10:00:00Z", seed_dev_mean=0.8, best_dev_mean=1.3, outcome="pr_opened",
                 paired_test_delta={"point": 0.2, "low": 0.05, "high": 0.4, "tasks": 3},
                 pr_url="https://example.invalid/pr/2"),
    sample_entry("a", "2026-11-01T10:00:00Z", seed_dev_mean=0.7, best_dev_mean=0.9, outcome="no_improvement",
                 paired_test_delta={"point": -0.1, "low": -0.3, "high": 0.1, "tasks": 3}),
    sample_entry("c", "2026-11-03T10:00:00Z", outcome="incomplete"),
]


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_chart_is_deterministic_and_valid(theme: str) -> None:
    first = chart.render(SAMPLES, theme)
    assert chart.render(list(reversed(SAMPLES)), theme) == first
    root = ElementTree.fromstring(first)
    texts = " ".join("".join(node.itertext()) for node in root.iter("{http://www.w3.org/2000/svg}text"))
    assert "parity 1.0" in texts and "PR" in texts and "95% CI" in texts
    assert root.find(".//{http://www.w3.org/2000/svg}line[@stroke-dasharray]") is not None


def test_the_themes_use_their_own_surface() -> None:
    assert chart.THEMES["light"]["surface"] in chart.render(SAMPLES, "light")
    assert chart.THEMES["dark"]["surface"] in chart.render(SAMPLES, "dark")


def polylines(svg: str) -> int:
    return len(ElementTree.fromstring(svg).findall(".//{http://www.w3.org/2000/svg}polyline"))


def test_no_line_joins_runs_of_different_panels() -> None:
    """Scores from different panels do not compare: a panel change breaks both series, and each panel is named."""
    runs = [sample_entry("a", "2026-11-01T10:00:00Z", seed_dev_mean=0.3, best_dev_mean=0.4, model="sonnet"),
            sample_entry("b", "2026-11-02T10:00:00Z", seed_dev_mean=0.8, best_dev_mean=0.9, model="sonnet")]
    one_panel = chart.render([{**entry, "panel": "gepa-v1"} for entry in runs], "light")
    two_panels = chart.render([{**runs[0], "panel": "gepa-v1"}, {**runs[1], "panel": "gepa-v3"}], "light")

    assert polylines(one_panel) == 2
    assert polylines(two_panels) == 0
    assert ">gepa-v1 · sonnet<" in two_panels and ">gepa-v3 · sonnet<" in two_panels
    assert f">{chart.PANEL_UNKNOWN} · sonnet<" in chart.render(runs, "light")
    assert f" · {chart.MODEL_UNKNOWN}<" in chart.render([{**entry, "model": None} for entry in runs], "light")


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_an_empty_log_renders_a_valid_no_runs_chart(tmp_path: Path, theme: str) -> None:
    svg = chart.render([], theme)
    root = ElementTree.fromstring(svg)
    assert "No evolve runs yet" in "".join(root.itertext())

    log = tmp_path / "evolve.jsonl"
    assert benchlog.main(["redraw", "--log", str(log)]) == 0
    assert log.exists() and log.read_text() == ""
    parses_as_svg(log.with_suffix(".svg"))
    parses_as_svg(log.with_name("evolve-dark.svg"))


def test_the_committed_charts_match_the_committed_log(tmp_path: Path) -> None:
    """A redraw of the committed log gives the committed charts, so the README never shows a stale graph."""
    light, dark = benchlog.chart_paths(benchlog.COMMITTED_LOG)
    rows = benchlog.load(benchlog.COMMITTED_LOG)
    assert light.read_text() == chart.render(rows, "light")
    assert dark.read_text() == chart.render(rows, "dark")


def test_tests_never_write_the_committed_log() -> None:
    assert benchlog.DEFAULT_LOG != benchlog.COMMITTED_LOG
    assert "benchmark/log" not in str(benchlog.DEFAULT_LOG)


def test_the_redraw_script_runs_without_the_harness(tmp_path: Path) -> None:
    log = tmp_path / "evolve.jsonl"
    script = Path(benchlog.__file__)
    done = subprocess.run([sys.executable, str(script), "redraw", "--log", str(log)], capture_output=True,
                          text=True, env={**os.environ, "PYTHONPATH": ""})
    assert done.returncode == 0, done.stderr
    parses_as_svg(log.with_suffix(".svg"))


# --- Sonnet only, the model on the chart, and the baseline arm next to tilth ---


def test_the_evolve_agent_model_is_sonnet_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    from evolve import cli

    sonnet = [name for name in run.MODELS if run.RUNNERS[name] == "claude" and "sonnet" in run.MODELS[name]]
    assert benchlog.sonnet_models() == sorted(sonnet) and "haiku" not in sonnet
    assert cli._parser().get_default("model") == "sonnet"
    world = make_world(monkeypatch, tmp_path)

    with pytest.raises(SystemExit) as exit_:
        main(world, "--model", "haiku")

    assert exit_.value.code == 2
    assert world.runner_calls() == []
    err = capsys.readouterr().err
    assert "invalid choice: 'haiku'" in err and all(f"'{name}'" in err for name in sonnet)


def expected_arm(world, tasks: tuple[str, ...], mode: str, keep) -> dict:
    """The arm numbers of the stub runner's cells of ``mode`` on ``tasks`` whose candidate sha ``keep`` accepts."""
    cells = {(task, rep): sha for task, call_mode, rep, sha in world.calls
             if call_mode == mode and task in tasks and keep(sha)}
    passed = [world.outcome(task, mode, rep, sha) == "1" for (task, rep), sha in cells.items()]
    costs = [world.costs.get(task, world.cost) * world.sha_scale.get(sha, 1.0) for (task, _rep), sha in cells.items()]
    return {"pass_rate": sum(passed) / len(cells), "mean_cost_usd": sum(costs) / len(cells),
            "cost_per_correct_usd": sum(costs) / sum(passed) if any(passed) else None, "cells": len(cells)}


def assert_arm(found: dict, expected: dict) -> None:
    assert set(found) == {"pass_rate", "mean_cost_usd", "cost_per_correct_usd", "cells"}
    assert found["cells"] == expected["cells"]
    for key in ("pass_rate", "mean_cost_usd", "cost_per_correct_usd"):
        assert found[key] == (None if expected[key] is None else pytest.approx(expected[key], abs=1e-6)), key


def test_a_draft_pr_run_records_baseline_seed_and_best_arms(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = weak_world(monkeypatch, tmp_path)
    world.baseline = {"cheap_a": "0", "dev_a": "10", "dev_b": "1", "test_a": "01"}
    world.costs = {"cheap_a": 0.1, "dev_a": 0.2, "dev_b": 0.5, "test_a": 0.3}
    use_engine(monkeypatch, [winning_child()])
    log = tmp_path / "evolve.jsonl"

    assert main(world, "--benchmark-log", str(log)) == 0

    [entry] = entries(log)
    assert entry["outcome"] == "pr_opened"
    [(winner_sha, _branch)] = world.pr.pushes
    is_seed, is_best = (lambda sha: sha != winner_sha), (lambda sha: sha == winner_sha)
    arms = entry["arms"]
    assert set(arms) == {"dev", "test"} and set(arms["dev"]) == set(arms["test"]) == {"baseline", "seed", "best"}
    for split, tasks in (("dev", DEV), ("test", TEST)):
        assert_arm(arms[split]["baseline"], expected_arm(world, tasks, "baseline", lambda sha: True))
        assert_arm(arms[split]["seed"], expected_arm(world, tasks, "tilth_forced", is_seed))
        assert_arm(arms[split]["best"], expected_arm(world, tasks, "tilth_forced", is_best))
    # Spot values from the marks: baseline dev passes 3 of 4 cells; the seed passes none, so it has no cost per correct.
    assert arms["dev"]["baseline"]["pass_rate"] == 0.75 and arms["dev"]["baseline"]["cells"] == 4
    assert arms["dev"]["seed"]["cost_per_correct_usd"] is None and arms["test"]["best"]["pass_rate"] == 1.0
    assert arms["test"]["baseline"]["cost_per_correct_usd"] == pytest.approx(0.6)
    assert_no_round_trip_lines(world)
    # The fixture rows carry no resolved model, so the entry records null rather than a guess.
    assert entry["model_id"] is None


def test_a_no_improvement_run_records_dev_arms_and_no_test_arms(monkeypatch: pytest.MonkeyPatch,
                                                                tmp_path: Path) -> None:
    world = make_world(monkeypatch, tmp_path)
    use_engine(monkeypatch)
    log = tmp_path / "evolve.jsonl"

    assert main(world, "--benchmark-log", str(log)) == 0

    [entry] = entries(log)
    assert entry["arms"]["test"] is None and entry["arms"]["dev"]["best"] is None
    assert_arm(entry["arms"]["dev"]["baseline"], expected_arm(world, DEV, "baseline", lambda sha: True))
    assert_arm(entry["arms"]["dev"]["seed"], expected_arm(world, DEV, "tilth_forced", lambda sha: True))


def arm(pass_rate: float, cost_per_correct: float | None) -> dict:
    return {"pass_rate": pass_rate, "mean_cost_usd": 0.25, "cost_per_correct_usd": cost_per_correct, "cells": 4}


def svg_texts(svg: str) -> list[str]:
    root = ElementTree.fromstring(svg)
    return ["".join(node.itertext()) for node in root.iter("{http://www.w3.org/2000/svg}text")]


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_chart_shows_the_model_and_the_baseline_next_to_tilth(theme: str) -> None:
    dev = {"baseline": arm(0.75, 0.4667), "seed": arm(0.5, 1.2), "best": arm(1.0, 0.35)}
    runs = [sample_entry("old", "2026-11-01T10:00:00Z", panel="gepa-v1", model="sonnet",
                         model_id="claude-sonnet-5-5", seed_dev_mean=0.3,
                         arms={"dev": {"baseline": arm(0.2, 9.0), "seed": None, "best": None}, "test": None}),
            sample_entry("new", "2026-11-02T10:00:00Z", panel="gepa-v1", model="sonnet",
                         model_id="claude-sonnet-5-5", seed_dev_mean=0.4, arms={"dev": dev, "test": None}),
            sample_entry("none", "2026-11-03T10:00:00Z", panel="gepa-v1", model="sonnet",
                         model_id="claude-sonnet-5-5", seed_dev_mean=0.5)]

    texts = svg_texts(chart.render(runs, theme))

    assert any("claude-sonnet-5-5" in text for text in texts)
    assert "baseline (no tilth)" in texts
    # The band's latest run with arms gives the row: its baseline, seed, and winner, each pass rate and cost per correct.
    assert "75% pass · $0.47/correct" in texts
    assert "50% pass · $1.20/correct" in texts and "100% pass · $0.35/correct" in texts
    assert "20% pass · $9.00/correct" not in texts
    assert any("run new" in text for text in texts)


def test_the_chart_says_when_an_arm_has_no_numbers() -> None:
    runs = [sample_entry("old", "2026-11-01T10:00:00Z", panel="gepa-v1", model="sonnet", model_id=None,
                         arms={"dev": {"baseline": arm(0.0, None), "seed": None, "best": None}, "test": None})]

    texts = svg_texts(chart.render(runs, "light"))

    assert "0% pass · no correct cell" in texts and chart.ARM_UNKNOWN in texts
    assert any("gepa-v1 · sonnet" in text for text in texts)


def test_a_model_change_breaks_the_series_and_labels_its_band() -> None:
    """``sonnet`` can resolve to a new model, and scores from different models do not compare."""
    runs = [sample_entry("a", "2026-11-01T10:00:00Z", panel="gepa-v1", model="sonnet", model_id="claude-sonnet-5-5",
                         seed_dev_mean=0.3, best_dev_mean=0.4),
            sample_entry("b", "2026-11-02T10:00:00Z", panel="gepa-v1", model="sonnet", model_id="claude-sonnet-5-5",
                         seed_dev_mean=0.5, best_dev_mean=0.6),
            sample_entry("c", "2026-11-03T10:00:00Z", panel="gepa-v1", model="sonnet", model_id="claude-sonnet-6",
                         seed_dev_mean=0.8, best_dev_mean=0.9)]

    svg = chart.render(runs, "light")

    # In each series a line joins a and b; c stands alone, so no line has three points.
    lines = ElementTree.fromstring(svg).findall(".//{http://www.w3.org/2000/svg}polyline")
    assert [len(line.get("points").split()) for line in lines] == [2, 2]
    same_model = [{**entry, "model_id": "claude-sonnet-5-5"} for entry in runs]
    same = ElementTree.fromstring(chart.render(same_model, "light")).findall(".//{http://www.w3.org/2000/svg}polyline")
    assert [len(line.get("points").split()) for line in same] == [3, 3]
    texts = svg_texts(svg)
    assert "gepa-v1 · claude-sonnet-5-5" in texts and "gepa-v1 · claude-sonnet-6" in texts
