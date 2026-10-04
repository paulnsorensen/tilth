"""Adversarial contract tests for the benchmark result substrate (Press, bench-result-substrate)."""

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import baselines
import run
from conftest import STREAMS, StoreTask

_ROW_FIELDS = ("run_key", "harness_digest", "task_digest", "env_fingerprint", "cli_version",
               "timeout_s", "trajectory_path", "cost_source", "reused")
_ARGS = ("--models", "sonnet5", "--modes", "plain", "--reps", "1")


class _Replay:
    def __init__(self, stream: str) -> None:
        self.stdout = io.StringIO(stream)
        self.stderr = io.StringIO("")
        self.returncode = 0

    def wait(self) -> None:
        return None

    def kill(self) -> None:
        self.returncode = -9


def test_reused_rows_do_not_count_toward_spend(bench) -> None:
    bench.seed(bench.row("cell_a", "plain", 0, cost=50.0))
    bench.runner(lambda _stream: 0.3)

    code = bench.main("--tasks", "cell_a,cell_b", *_ARGS, "--max-usd", "1.0", "--cell-estimate-usd", "0.5")

    assert code == 0
    assert bench.calls == [("cell_b", "plain", 0)]


def test_ceiling_admits_a_cell_that_lands_exactly_on_it(bench) -> None:
    bench.runner(lambda _stream: 0.5)

    code = bench.main("--tasks", "cell_a,cell_b", *_ARGS, "--max-usd", "1.0", "--cell-estimate-usd", "0.5")

    assert code == 0
    assert len(bench.calls) == 2


def test_timeout_row_records_estimate_and_is_rerun_next_run(bench) -> None:
    def timeout(_stream: Path) -> float:
        raise subprocess.TimeoutExpired(["claude"], run.CELL_TIMEOUT_S)

    bench.runner(timeout)
    assert bench.main("--tasks", "cell_a", *_ARGS, "--max-usd", "5", "--cell-estimate-usd", "0.25") == 0
    [row] = bench.output_rows()
    assert row["error"] == "timeout" and row["timed_out"] is True
    assert (row["total_cost_usd"], row["cost_source"]) == (0.25, "estimate")
    assert all(field in row for field in _ROW_FIELDS)
    assert row["reused"] is False

    bench.runner(lambda _stream: 0.1)
    assert bench.main("--tasks", "cell_a", *_ARGS, "--max-usd", "5") == 0
    assert bench.calls == [("cell_a", "plain", 0), ("cell_a", "plain", 0)]


def test_failed_cell_reporting_zero_native_cost_charges_zero(bench) -> None:
    def zero_cost_failure(stream: Path) -> float:
        stream.parent.mkdir(parents=True, exist_ok=True)
        stream.write_text(json.dumps({"type": "result", "subtype": "error_during_execution",
                                      "is_error": True, "total_cost_usd": 0}) + "\n")
        raise RuntimeError("claude -p did not complete successfully: error_during_execution")

    bench.runner(zero_cost_failure)

    assert bench.main("--tasks", "cell_a", *_ARGS, "--max-usd", "5", "--cell-estimate-usd", "0.9") == 0
    [row] = bench.output_rows()
    assert (row["total_cost_usd"], row["cost_source"]) == (0.0, "native")


def test_quota_stop_resumes_only_the_unfinished_cells(bench) -> None:
    def quota_on_second(stream: Path) -> float:
        if len(bench.calls) == 2:
            stream.parent.mkdir(parents=True, exist_ok=True)
            stream.write_text((STREAMS / "claude_quota_rejected.jsonl").read_text())
            raise RuntimeError("claude -p failed with code 1")
        return 0.1

    bench.runner(quota_on_second)
    assert bench.main("--tasks", "cell_a,cell_b,cell_c", *_ARGS, "--max-usd", "5") != 0

    bench.runner(lambda _stream: 0.1)
    assert bench.main("--tasks", "cell_a,cell_b,cell_c", *_ARGS, "--max-usd", "5") == 0
    assert bench.calls[2:] == [("cell_b", "plain", 0), ("cell_c", "plain", 0)]
    assert [row["reused"] for row in bench.output_rows()] == [True, False, False]


def test_api_key_does_not_block_a_fully_reused_claude_run(bench, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api")
    bench.seed(bench.row("cell_a", "plain", 0))
    bench.runner(lambda _stream: pytest.fail("model call started"))

    assert bench.main("--tasks", "cell_a", *_ARGS) == 0


def test_fresh_row_reproduces_its_own_run_key(bench) -> None:
    bench.runner(lambda _stream: 0.1)

    assert bench.main("--tasks", "cell_a", *_ARGS, "--max-usd", "5") == 0
    [row] = bench.output_rows()
    assert baselines.run_key(row) == row["run_key"]
    assert all(field in row for field in _ROW_FIELDS)


def test_task_content_change_invalidates_reuse(bench, monkeypatch: pytest.MonkeyPatch) -> None:
    bench.seed(bench.row("cell_a", "plain", 0))
    monkeypatch.setitem(run.TASKS, "cell_a", StoreTask(prompt="A different question."))
    bench.runner(lambda _stream: 0.1)

    assert bench.main("--tasks", "cell_a", *_ARGS, "--max-usd", "5") == 0
    assert bench.calls == [("cell_a", "plain", 0)]


def test_codex_cell_writes_an_untruncated_sidecar(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(run.TASKS, "codex_sidecar_task", StoreTask())
    long_output = "y" * 900
    events = [
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "item_1", "type": "command_execution",
                                             "command": "go test ./render", "aggregated_output": long_output,
                                             "exit_code": 0, "status": "completed"}},
        {"type": "item.completed", "item": {"id": "item_2", "type": "agent_message", "text": "handler_3"}},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
    ]
    stream = "".join(json.dumps(event) + "\n" for event in events)
    monkeypatch.setattr(run.subprocess, "Popen", lambda *_args, **_kwargs: _Replay(stream))

    row = run._run_single_in_repo("codex_sidecar_task", "baseline", "luna56", 0, tmp_path,
                                  stream_log_path=tmp_path / "streams" / "01_codex.jsonl")

    [record] = [json.loads(line) for line in Path(row["trajectory_path"]).read_text().splitlines()]
    assert record["input"] == {"command": "go test ./render"}
    assert record["output"] == long_output
    assert row["cost_source"] == "pricing"


def test_opencode_row_records_null_trajectory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(run.TASKS, "opencode_task", StoreTask())
    monkeypatch.setattr(run.subprocess, "run", lambda cmd, **_kwargs: subprocess.CompletedProcess(
        cmd, 0, stdout=json.dumps({"type": "text", "part": {"text": "handler_3"}}), stderr=""))

    row = run._run_single_in_repo("opencode_task", "baseline", "gpt5mini", 0, tmp_path,
                                  stream_log_path=tmp_path / "streams" / "01_opencode.jsonl")

    assert row["trajectory_path"] is None
    assert all(field in row for field in _ROW_FIELDS)


def test_reused_row_carries_the_current_runs_schedule_metadata(bench) -> None:
    stored = bench.row("cell_a", "plain", 0, experiment_manifest="/old/manifest.json",
                       arm_order_seed=7, arm_order=["plain"], arm_order_index=0)
    bench.seed(stored)
    bench.runner(lambda _stream: pytest.fail("model call started"))

    assert bench.main("--tasks", "cell_a", *_ARGS, "--arm-order-seed", "42") == 0
    [row] = bench.output_rows()
    assert (row["experiment_manifest"], row["arm_order_seed"]) == (None, 42)
    assert (row["arm_order"], row["arm_order_index"], row["reused"]) == (["plain"], 0, True)
    assert row["run_key"] == stored["run_key"]
    assert bench.stored_rows() == [stored]


def test_quota_classifier_ignores_a_rejected_event_when_the_stream_succeeded(bench) -> None:
    def succeeded_then_failed_grading(stream: Path) -> float:
        events = [json.loads(line) for line in (STREAMS / "claude_native_cost.jsonl").read_text().splitlines()]
        events.insert(1, {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}})
        stream.parent.mkdir(parents=True, exist_ok=True)
        stream.write_text("".join(json.dumps(event) + "\n" for event in events))
        raise RuntimeError("strict cell used forbidden Bash command: cat x")

    bench.runner(succeeded_then_failed_grading)

    assert bench.main("--tasks", "cell_a,cell_b", *_ARGS, "--max-usd", "5") == 0
    assert len(bench.calls) == 2
    assert all("infra" not in row for row in bench.output_rows())
