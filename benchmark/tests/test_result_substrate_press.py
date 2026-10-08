"""Adversarial contract tests for the benchmark result substrate (Press, bench-result-substrate)."""

import io
import json
import subprocess
import sys
from dataclasses import replace
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
    assert "total_cost_usd" not in row
    assert (row["charged_usd"], row["cost_source"]) == (0.25, "estimate")
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

    # `plain` attaches no MCP, so it is a stock arm: the changed task is baseline drift.
    assert bench.main("--tasks", "cell_a", *_ARGS, "--max-usd", "5") != 0
    assert bench.main("--tasks", "cell_a", *_ARGS, "--max-usd", "5", "--refreeze-baselines") == 0
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


def _fail_without_native_cost(_stream: Path) -> float:
    raise RuntimeError("claude -p failed with code 1")


def test_estimated_failure_cost_is_charged_not_reported_as_cost(bench) -> None:
    bench.runner(_fail_without_native_cost)

    assert bench.main("--tasks", "cell_a", *_ARGS, "--max-usd", "5", "--cell-estimate-usd", "0.3") == 0
    [row] = bench.output_rows()
    assert "total_cost_usd" not in row
    assert (row["charged_usd"], row["cost_source"]) == (0.3, "estimate")
    assert bench.stored_rows() == [row]


def test_native_failure_cost_is_both_cost_and_charge(bench) -> None:
    def capped(stream: Path) -> float:
        stream.parent.mkdir(parents=True, exist_ok=True)
        stream.write_text(json.dumps({"type": "result", "subtype": "error_max_budget_usd", "is_error": True,
                                      "total_cost_usd": 0.7}) + "\n")
        raise RuntimeError("claude -p did not complete successfully: error_max_budget_usd")

    bench.runner(capped)

    assert bench.main("--tasks", "cell_a", *_ARGS, "--max-usd", "5") == 0
    [row] = bench.output_rows()
    assert (row["total_cost_usd"], row["charged_usd"], row["cost_source"]) == (0.7, 0.7, "native")


def test_estimated_failure_rows_carry_no_cost_into_paired_analysis(bench) -> None:
    import paired

    bench.runner(_fail_without_native_cost)
    assert bench.main("--tasks", "cell_a", *_ARGS, "--max-usd", "5", "--cell-estimate-usd", "0.3") == 0
    [row] = bench.output_rows()

    assert paired.measured_cost(row) is None
    assert paired.measured_cost({**row, "total_cost_usd": 0.3}) is None  # a pre-fix row tagged as an estimate


def test_failed_native_cost_row_never_counts_toward_cost_per_correct() -> None:
    import analyze
    import paired

    ok = {"correct": True, "total_cost_usd": 0.2, "cost_source": "native"}
    failed = {"correct": False, "total_cost_usd": 0.7, "cost_source": "native", "error": "error_max_budget_usd"}

    assert paired.measured_cost(ok) == 0.2
    assert paired.measured_cost(failed) is None
    assert paired._cpc([ok, failed]) == pytest.approx(0.2)
    assert analyze.cost_per_correct([ok, failed])[0] == pytest.approx(0.2)


def test_reused_row_carries_the_current_variant_metadata(bench, monkeypatch: pytest.MonkeyPatch) -> None:
    stale = {"label": "plain", "git_ref": "old-ref", "binary_path": "/old/tilth", "tilth_version": "0.8.3"}
    stored = bench.row("cell_a", "plain", 0, variant=stale, tilth_version="0.8.3")
    bench.seed(stored)
    current = replace(run.MODES["plain"], git_ref="new-ref", binary_path="/new/tilth", tilth_version="0.8.4")
    monkeypatch.setitem(run.MODES, "plain", current)
    bench.runner(lambda _stream: pytest.fail("model call started"))

    assert bench.main("--tasks", "cell_a", *_ARGS) == 0
    [row] = bench.output_rows()
    assert row["variant"] == run._variant_metadata(current)
    assert row["tilth_version"] == "0.8.4"
    assert (row["correct"], row["correctness_reason"], row["total_cost_usd"]) == (True, "stored", 0.1)
    assert bench.stored_rows() == [stored]


def test_cli_version_change_mid_run_stops_before_the_next_paid_cell(
    bench, capsys: pytest.CaptureFixture[str],
) -> None:
    def updates_cli(_stream: Path) -> float:
        bench.cli = "2.2.0"
        return 0.1

    bench.runner(updates_cli)

    assert bench.main("--tasks", "cell_a,cell_b", *_ARGS, "--max-usd", "5") != 0
    assert bench.calls == [("cell_a", "plain", 0)]
    assert {row["cli_version"] for row in bench.stored_rows()} == {"2.1.0"}
    out = capsys.readouterr().out
    assert "CLI version changed" in out and "2.1.0" in out and "2.2.0" in out


def test_a_cell_is_stored_and_charged_once_when_reporting_fails(
    bench, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    def run_single_missing_report_fields(task_name, mode_name, model_name, repetition, **_kwargs):
        bench.calls.append((task_name, mode_name, repetition))
        return {"task": task_name, "mode": mode_name, "model": run.MODELS[model_name],
                "repetition": repetition, "correct": True, "correctness_reason": "fresh",
                "total_cost_usd": 0.2, "cost_source": "native"}

    monkeypatch.setattr(run, "run_single", run_single_missing_report_fields)

    assert bench.main("--tasks", "cell_a,cell_b", *_ARGS, "--max-usd", "5") != 0
    assert bench.calls == [("cell_a", "plain", 0)]
    assert len(bench.stored_rows()) == 1
    assert bench.stored_rows()[0]["charged_usd"] == 0.2
    assert len(bench.output_rows()) == 1
    out = capsys.readouterr().out
    assert "reporting failed after cell cell_a/plain/sonnet5/rep0 was stored" in out
    assert "Spend: $0.2000" in out


# --- independent-review cure of 8efe6bd ---


def test_scheduler_reprobes_the_cli_fresh_before_each_paid_cell(
    bench, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Only a fresh probe sees the update; a cached probe would let the cell run."""
    probes: list[bool] = []

    def probe(_runner: str, *, fresh: bool = False) -> str:
        probes.append(fresh)
        return "9.9.9" if fresh else bench.cli

    monkeypatch.setattr(run, "cli_version", probe)
    bench.runner(lambda _stream: pytest.fail("model call started"))

    assert bench.main("--tasks", "cell_a", *_ARGS, "--max-usd", "5") != 0
    assert bench.calls == []
    assert probes[-1] is True
    assert "CLI version changed" in capsys.readouterr().out


def test_fresh_cli_version_bypasses_the_probe_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    import functools

    versions = iter(["2.1.0", "2.2.0", "2.3.0"])
    monkeypatch.setattr(run, "_run_probe", lambda _argv: next(versions))
    monkeypatch.setattr(run, "_probe_version", functools.lru_cache(maxsize=None)(lambda argv: run._run_probe(argv)))

    assert run.cli_version("claude") == "2.1.0"
    assert run.cli_version("claude") == "2.1.0"
    assert run.cli_version("claude", fresh=True) == "2.2.0"


def test_failed_cli_probe_stops_with_its_own_reason(
    bench, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(run, "cli_version", lambda _runner, *, fresh=False: None if fresh else bench.cli)
    bench.runner(lambda _stream: pytest.fail("model call started"))

    assert bench.main("--tasks", "cell_a", *_ARGS, "--max-usd", "5") != 0
    out = capsys.readouterr().out
    assert "agent CLI version probe failed" in out
    assert "now None" not in out


def test_reused_rows_carry_no_charge_from_the_run_that_stored_them(bench) -> None:
    bench.seed(bench.row("cell_a", "plain", 0, charged_usd=0.4))
    bench.runner(lambda _stream: 0.3)

    assert bench.main("--tasks", "cell_a,cell_b", *_ARGS, "--max-usd", "5") == 0
    rows = bench.output_rows()
    assert sum(row.get("charged_usd", 0) for row in rows) == pytest.approx(0.3)
    assert bench.stored_rows()[0]["charged_usd"] == 0.4


def test_reused_rows_probe_the_tilth_version_once_per_mode(bench, monkeypatch: pytest.MonkeyPatch) -> None:
    for task in ("cell_a", "cell_b", "cell_c"):
        bench.seed(bench.row(task, "plain", 0))
    probes: list[str] = []
    monkeypatch.setattr(run, "_reported_tilth_version", lambda mode: probes.append(mode.name) or "0.8.4")
    bench.runner(lambda _stream: pytest.fail("model call started"))

    assert bench.main("--tasks", "cell_a,cell_b,cell_c", *_ARGS) == 0
    assert probes == ["plain"]
    assert {row["tilth_version"] for row in bench.output_rows()} == {"0.8.4"}
