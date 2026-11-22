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
from evolve.loop import Evolution
from evolve_support import child, log_text, main, make_world, mcp, scripted_engine, seed_candidate, seed_files

ENTRY_KEYS = {
    "schema", "run_id", "recorded_at", "seed_sha", "tilth_version", "panel", "model", "preamble", "stop_reason",
    "outcome", "seed_dev_mean", "best_cid", "best_dev_mean", "test_means", "paired_test_delta", "pass_rate_delta",
    "spend", "pr_url",
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


def test_a_draft_pr_run_appends_one_entry_and_draws_both_charts(monkeypatch: pytest.MonkeyPatch,
                                                               tmp_path: Path) -> None:
    world = weak_world(monkeypatch, tmp_path)
    use_engine(monkeypatch, [winning_child()])
    log = tmp_path / "bench" / "evolve.jsonl"

    assert main(world, "--benchmark-log", str(log)) == 0

    [entry] = entries(log)
    assert set(entry) == ENTRY_KEYS
    assert entry["schema"] == benchlog.SCHEMA == 1
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
    assert light != dark and b"PR" in light
    assert f"benchmark log: appended run1 to {log}" in log_text(world)

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


def test_import_builds_the_same_entry_from_a_run_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = weak_world(monkeypatch, tmp_path)
    use_engine(monkeypatch, [winning_child()])
    live = tmp_path / "live.jsonl"
    assert main(world, "--benchmark-log", str(live)) == 0
    imported = tmp_path / "imported" / "evolve.jsonl"
    run_dir = run.RESULTS_DIR / "evolve" / "run1"

    assert benchlog.main(["import", str(run_dir), "--log", str(imported)]) == 0
    assert benchlog.main(["import", str(run_dir), "--log", str(imported)]) == 0  # a known run id is skipped

    [live_entry], [import_entry] = entries(live), entries(imported)
    assert {**import_entry, "recorded_at": None} == {**live_entry, "recorded_at": None}
    parses_as_svg(imported.with_suffix(".svg"))


def test_import_skips_a_directory_without_a_run_log(tmp_path: Path) -> None:
    log = tmp_path / "evolve.jsonl"
    (tmp_path / "empty-run").mkdir()

    assert benchlog.main(["import", str(tmp_path / "empty-run"), "--log", str(log)]) == 0
    assert log.read_text() == ""


OLD_LOG = """\
refused: baselines: stored baseline rows were recorded under different key inputs
reserve: $34.2300 held for finish (per finalist $17.1150)
candidate 9708a2d9ca75: accepted (dev mean 0.333, frontier 1)
search: ended (metric-call budget)
finish: finalists fcb7eb780569 (dev mean 0.467), 9708a2d9ca75 (dev mean 0.333)
finish: test-split means fcb7eb780569=0.667, 9708a2d9ca75=0.333
finish: paired test delta +0.250 (95% CI [0.100, 0.400], 3 tasks); test pass rate delta +0.333
finish: draft PR opened from evolve/old-winner: {'url': 'https://github.com/o/r/pull/356', 'head': 'x'}
spend: total=$28.8068 of $150.00
"""


def test_an_old_log_uses_its_finalists_line_and_old_spend_form(tmp_path: Path) -> None:
    run_dir = tmp_path / "old-run"
    run_dir.mkdir()
    (run_dir / "log.txt").write_text(OLD_LOG)
    rows = [{"mode": "tilth_forced", "tilth_version": "tilth 0.8.4", "model_alias": "sonnet"},
            {"mode": "baseline", "tilth_version": None, "model_alias": "sonnet"}]
    (run_dir / "rows.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    refused = tmp_path / "refused-run"
    refused.mkdir()
    (refused / "log.txt").write_text("refused: baselines: drift\n")
    log = tmp_path / "evolve.jsonl"

    assert benchlog.main(["import", str(run_dir), str(refused), "--log", str(log)]) == 0

    [entry] = entries(log)
    assert entry["run_id"] == "old-run" and entry["seed_sha"] is None
    assert entry["panel"] is None and entry["preamble"] is None and entry["model"] == "sonnet"
    assert entry["tilth_version"] == "tilth 0.8.4"
    assert entry["seed_dev_mean"] == 0.333 and entry["best_dev_mean"] == 0.467
    assert entry["best_cid"] == "fcb7eb780569"
    assert entry["test_means"] == {"fcb7eb780569": 0.667, "9708a2d9ca75": 0.333}
    assert entry["paired_test_delta"] == {"point": 0.25, "low": 0.1, "high": 0.4, "tasks": 3}
    assert entry["pass_rate_delta"] == 0.333
    assert entry["spend"] == {"total": 28.8068, "max_usd": 150.0}
    assert entry["outcome"] == "pr_opened" and entry["pr_url"] == "https://github.com/o/r/pull/356"
    assert entry["stop_reason"] == "metric-call budget"


def test_an_old_log_takes_its_panel_from_the_stored_rows(tmp_path: Path) -> None:
    run_dir = tmp_path / "old-run"
    run_dir.mkdir()
    (run_dir / "log.txt").write_text(OLD_LOG)
    rows = [{"mode": "tilth_forced", "panel_name": "gepa-v1"}, {"mode": "baseline", "panel_name": "gepa-v1"}]
    (run_dir / "rows.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    log = tmp_path / "evolve.jsonl"

    assert benchlog.main(["import", str(run_dir), "--log", str(log)]) == 0

    [entry] = entries(log)
    assert entry["panel"] == "gepa-v1"


def sample_entry(run_id: str, recorded_at: str, **fields) -> dict:
    entry = dict.fromkeys(ENTRY_KEYS)
    return {**entry, "schema": 1, "run_id": run_id, "recorded_at": recorded_at, **fields}


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
    runs = [sample_entry("a", "2026-11-01T10:00:00Z", seed_dev_mean=0.3, best_dev_mean=0.4),
            sample_entry("b", "2026-11-02T10:00:00Z", seed_dev_mean=0.8, best_dev_mean=0.9)]
    one_panel = chart.render([{**entry, "panel": "gepa-v1"} for entry in runs], "light")
    two_panels = chart.render([{**runs[0], "panel": "gepa-v1"}, {**runs[1], "panel": "gepa-v3"}], "light")

    assert polylines(one_panel) == 2
    assert polylines(two_panels) == 0
    assert ">gepa-v1<" in two_panels and ">gepa-v3<" in two_panels
    assert f">{chart.PANEL_UNKNOWN}<" in chart.render(runs, "light")


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
