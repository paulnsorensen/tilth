"""Result-store reuse, run-key identity, and baseline drift (spec bench-result-substrate)."""

import importlib.util
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import baselines
import run
from tasks import TASKS

_HARNESS = {
    "system_prompt": "You are a code assistant.",
    "tools": ["Read", "Edit"],
    "strict_file_tools": False,
    "bare": False,
    "max_budget_usd": 1.0,
    "mcp_shape": {"tilth": {"args": ["--mcp", "--edit"]}},
}
_TASK = {
    "prompt": "Name the dispatcher.",
    "ground_truth": {"required_strings": ["handler_3"]},
    "test_command": ["go", "test", "./..."],
    "fixture_files": {"render_test.go": "0" * 64},
    "repo_commit": "d7776de7",
}
_ENV = {"toolchains": {"go": "go1.26", "rustc": "rustc 1.99"}, "lockfile_hash": "1" * 64}
_CELL = {
    "task": "gin_servehttp_flow",
    "model": "claude-sonnet-5",
    "cli_version": "2.1.0",
    "reasoning_effort": None,
    "timeout_s": 600,
    "mode": "tilth",
    "repetition": 0,
    "git_sha": "a" * 40,
    "binary_sha256": "b" * 64,
}

_CHANGED_INPUTS = [
    *(("harness", key, value) for key, value in {
        "system_prompt": "You are a different assistant.",
        "tools": ["Read"],
        "strict_file_tools": True,
        "bare": True,
        "max_budget_usd": 2.0,
        "mcp_shape": {"tilth": {"args": ["--mcp"]}},
    }.items()),
    *(("task", key, value) for key, value in {
        "prompt": "Name the router.",
        "ground_truth": {"required_strings": ["handler_4"]},
        "test_command": ["go", "test", "./render"],
        "fixture_files": {"render_test.go": "f" * 64},
        "repo_commit": "0a88cccd",
        "task_sources": {"tasks/gin_render_context_tasks.py": "e" * 64},
    }.items()),
    *(("env", key, value) for key, value in {
        "toolchains": {"go": "go1.27", "rustc": "rustc 1.99"},
        "lockfile_hash": "2" * 64,
    }.items()),
    *(("cell", key, value) for key, value in {
        "git_sha": "c" * 40,
        "binary_sha256": "d" * 64,
        "task": "rg_search_dispatch",
        "cli_version": "2.2.0",
        "model": "claude-opus-5",
        "reasoning_effort": "high",
        "timeout_s": 900,
        "mode": "baseline",
        "repetition": 1,
    }.items()),
]


def _key(harness: dict, task: dict, env: dict, cell: dict) -> str:
    return baselines.run_key({
        **cell,
        "harness_digest": baselines.harness_digest(**harness),
        "task_digest": baselines.task_digest(**task),
        "env_fingerprint": baselines.env_fingerprint(**env),
    })


@pytest.mark.parametrize(("group", "field", "value"), _CHANGED_INPUTS, ids=[f"{g}.{f}" for g, f, _ in _CHANGED_INPUTS])
def test_run_key_changes_per_input(group: str, field: str, value: object) -> None:
    parts = {"harness": dict(_HARNESS), "task": dict(_TASK), "env": dict(_ENV), "cell": dict(_CELL)}
    original = _key(**parts)
    parts[group][field] = value

    assert _key(**parts) != original


def test_run_key_is_stable_for_identical_inputs() -> None:
    first = _key(dict(_HARNESS), dict(_TASK), dict(_ENV), dict(_CELL))
    second = _key(dict(_HARNESS), dict(_TASK), dict(_ENV), dict(reversed(list(_CELL.items()))))

    assert first == second


def test_only_completed_rows_are_reusable(tmp_path: Path) -> None:
    path = tmp_path / "store.jsonl"
    completed = {"run_key": "k1", "correct": True}
    baselines.store({"run_key": "k2", "error": "boom"}, path=path)
    baselines.store({"run_key": "k3", "infra": "quota"}, path=path)
    baselines.store({"run_key": "k4", "error": "timeout", "timed_out": True}, path=path)
    baselines.store(completed, path=path)

    assert baselines.lookup("k1", path=path) == completed
    assert baselines.lookup("k2", path=path) is None
    assert baselines.lookup("k3", path=path) is None
    assert baselines.lookup("k4", path=path) is None
    assert baselines.lookup("missing", path=path) is None
    assert len(baselines.load_rows(path)) == 4


def test_completed_row_reused_error_row_rerun(bench) -> None:
    completed = bench.row("cell_a", "plain", 0, cost=0.3)
    bench.seed(completed)
    bench.seed(bench.row("cell_b", "plain", 0, error="RuntimeError: boom", correct=False))
    bench.seed(bench.row("cell_c", "plain", 0, infra="quota", correct=False))
    bench.runner(lambda _stream: 0.2)

    code = bench.main("--tasks", "cell_a,cell_b,cell_c", "--models", "sonnet5", "--modes", "plain",
                      "--reps", "1", "--max-usd", "5")

    assert code == 0
    assert bench.calls == [("cell_b", "plain", 0), ("cell_c", "plain", 0)]
    rows = {row["task"]: row for row in bench.output_rows()}
    schedule = {"experiment_manifest": None, "arm_order_seed": None, "arm_order": ["plain"], "arm_order_index": 0}
    variant = {"tilth_version": None, "variant": run._variant_metadata(run.MODES["plain"])}
    # A stored row without the field is scanned before it is written; it has no sidecar to scan.
    unscanned = {"contaminated": True, "contamination_hits": [{"reason": "unscanned", "tool": None, "input": None}]}
    assert rows["cell_a"] == {**completed, **unscanned, **schedule, **variant, "reused": True}
    assert rows["cell_b"]["reused"] is False and "error" not in rows["cell_b"]
    assert rows["cell_c"]["reused"] is False and "infra" not in rows["cell_c"]
    assert rows["cell_b"]["run_key"] == bench.identity("cell_b", "plain", 0)["run_key"]
    fresh = [row for row in bench.stored_rows() if row.get("correctness_reason") == "fresh"]
    assert {row["task"] for row in fresh} == {"cell_b", "cell_c"}
    assert all(not row.get("reused") for row in bench.stored_rows())


def test_fully_reused_run_needs_no_spend_ceiling(bench) -> None:
    bench.seed(bench.row("cell_a", "plain", 0))
    bench.runner(lambda _stream: pytest.fail("model call started"))

    assert bench.main("--tasks", "cell_a", "--models", "sonnet5", "--modes", "plain", "--reps", "1") == 0
    assert [row["reused"] for row in bench.output_rows()] == [True]


def test_baseline_drift_requires_refreeze(bench, capsys: pytest.CaptureFixture[str]) -> None:
    bench.cli = "2.0.0"
    bench.seed(bench.row("cell_a", "baseline", 0))
    bench.cli = "2.1.0"
    bench.runner(lambda _stream: 0.1)

    code = bench.main("--tasks", "cell_a", "--models", "sonnet5", "--modes", "baseline",
                      "--reps", "1", "--max-usd", "5")

    assert code != 0
    assert bench.calls == []
    error = capsys.readouterr().err
    assert "--refreeze-baselines" in error
    assert "cli_version" in error
    assert "env_fingerprint" not in error


def test_baseline_drift_names_env_fingerprint(bench, capsys: pytest.CaptureFixture[str]) -> None:
    bench.seed(bench.row("cell_a", "baseline", 0))
    bench.env = "env-fingerprint-b"
    bench.runner(lambda _stream: 0.1)

    code = bench.main("--tasks", "cell_a", "--models", "sonnet5", "--modes", "baseline",
                      "--reps", "1", "--max-usd", "5")

    assert code != 0
    assert bench.calls == []
    assert "env_fingerprint" in capsys.readouterr().err


def test_refrozen_baselines_do_not_block(bench) -> None:
    bench.cli = "2.0.0"
    bench.seed(bench.row("cell_a", "baseline", 0))
    bench.cli = "2.1.0"
    bench.runner(lambda _stream: 0.1)
    argv = ("--tasks", "cell_a", "--models", "sonnet5", "--modes", "baseline", "--reps", "1", "--max-usd", "5")

    assert bench.main(*argv, "--refreeze-baselines") == 0
    assert bench.calls == [("cell_a", "baseline", 0)]

    assert bench.main(*argv) == 0
    assert bench.calls == [("cell_a", "baseline", 0)]
    assert [row["reused"] for row in bench.output_rows()] == [True]


def test_first_baseline_runs(bench) -> None:
    bench.runner(lambda _stream: 0.1)

    code = bench.main("--tasks", "cell_a", "--models", "sonnet5", "--modes", "baseline",
                      "--reps", "1", "--max-usd", "5")

    assert code == 0
    assert bench.calls == [("cell_a", "baseline", 0)]
    assert [row["mode"] for row in bench.stored_rows()] == ["baseline"]


def test_drift_check_ignores_other_repetitions_and_arms(bench) -> None:
    bench.cli = "2.0.0"
    bench.seed(bench.row("cell_a", "baseline", 1))
    bench.seed(bench.row("cell_a", "plain", 0))
    bench.cli = "2.1.0"
    bench.runner(lambda _stream: 0.1)

    code = bench.main("--tasks", "cell_a", "--models", "sonnet5", "--modes", "baseline",
                      "--reps", "1", "--max-usd", "5")

    assert code == 0
    assert bench.calls == [("cell_a", "baseline", 0)]


_DEMO_TASK = '''
class DemoTask:
    repo = "not-a-repo"
    prompt = "Name the dispatcher."
    ground_truth = {"required_strings": ["handler_3"]}
    test_command = ["go", "test", "./..."]

    def check_correctness(self, result_text, repo_path):
        return "{answer}" in result_text, "graded"
'''


def _demo_task_digest(tmp_path: Path, *, answer: str = "handler_3") -> str:
    module_path = tmp_path / "demo_tasks.py"
    module_path.write_text(textwrap.dedent(_DEMO_TASK).replace("{answer}", answer))
    name = f"demo_tasks_{abs(hash((str(tmp_path), answer, module_path.read_text())))}"
    spec = importlib.util.spec_from_file_location(name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return run._cell_task_digest(module.DemoTask())


def test_task_digest_tracks_grading_code(tmp_path: Path) -> None:
    """A check_correctness edit must invalidate stored rows graded by the old code."""
    assert _demo_task_digest(tmp_path) != _demo_task_digest(tmp_path, answer="handler_4")


def test_task_digest_tracks_held_out_fixtures(tmp_path: Path) -> None:
    """Fixtures may sit in `<stem>_fixtures` or, for `*_tasks` modules, `<name>_fixtures`."""
    held_out = tmp_path / "demo_fixtures" / "held_out_test.go"
    held_out.parent.mkdir()
    held_out.write_text("package demo\n")
    before = _demo_task_digest(tmp_path)
    held_out.write_text("package demo\n\nfunc TestHeldOut() {}\n")

    assert _demo_task_digest(tmp_path) != before


def test_gin_render_context_digest_covers_held_out_tests() -> None:
    fixtures = run._task_fixture_files(TASKS["gin_edit_render_context"])

    assert any(path.startswith("gin_render_context_fixtures/") for path in fixtures)


def test_task_sources_cover_task_class_and_bases() -> None:
    sources = run._task_source_files(TASKS["gin_edit_render_context"])

    assert {"tasks/gin_render_context_tasks.py", "tasks/base.py"} <= set(sources)
    assert all(not Path(path).is_absolute() for path in sources)


def _stock_arm(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    from config import ModeConfig

    monkeypatch.setitem(run.MODES, name, ModeConfig(name=name, tools=["Read"], mcp_config_path=None,
                                                    description="stock arm"))


def test_no_tilth_arm_is_drift_checked_as_a_baseline(bench, monkeypatch: pytest.MonkeyPatch,
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    """Experiment manifests name the stock arm `no_tilth`; it is frozen like `baseline`."""
    _stock_arm(monkeypatch, "no_tilth")
    bench.cli = "2.0.0"
    bench.seed(bench.row("cell_a", "no_tilth", 0))
    bench.cli = "2.1.0"
    bench.runner(lambda _stream: 0.1)

    code = bench.main("--tasks", "cell_a", "--models", "sonnet5", "--modes", "no_tilth",
                      "--reps", "1", "--max-usd", "5")

    assert code != 0
    assert bench.calls == []
    assert "cli_version" in capsys.readouterr().err


def test_tilth_arm_is_not_a_stock_arm() -> None:
    assert run.is_stock_arm(run.MODES["baseline"])
    assert not run.is_stock_arm(run.MODES["tilth"])
    assert not run.is_stock_arm(run.MODES["tilth_forced"])


def test_harness_variant_of_a_baseline_is_not_drift(bench) -> None:
    """A --bare baseline is a different harness, not a drifted copy of the default one."""
    bench.seed(bench.row("cell_a", "baseline", 0))
    bench.runner(lambda _stream: 0.1)

    code = bench.main("--tasks", "cell_a", "--models", "sonnet5", "--modes", "baseline",
                      "--reps", "1", "--max-usd", "5", "--bare")

    assert code == 0
    assert bench.calls == [("cell_a", "baseline", 0)]


def test_load_rows_skips_a_torn_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "store.jsonl"
    baselines.store({"run_key": "k1", "correct": True}, path=path)
    baselines.store({"run_key": "k2", "correct": True}, path=path)
    with path.open("a") as store_file:
        store_file.write('{"run_key": "k3", "corr')

    assert [row["run_key"] for row in baselines.load_rows(path)] == ["k1", "k2"]
    assert "skipped 1 malformed" in capsys.readouterr().err

    baselines.store({"run_key": "k4", "correct": True}, path=path)
    assert [row["run_key"] for row in baselines.load_rows(path)] == ["k1", "k2", "k4"]
