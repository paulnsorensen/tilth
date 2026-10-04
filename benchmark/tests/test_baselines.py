"""Result-store reuse, run-key identity, and baseline drift (spec bench-result-substrate)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import baselines

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
    assert rows["cell_a"] == {**completed, **schedule, "reused": True}
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
