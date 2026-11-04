"""Persistent spend budgets, failed-cell pricing, and the subscription-only auth gate in ``run.py`` (offline)."""

import json
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import auth
import run
from evolve_support import DEV, make_world
from spend import MIN_HEADROOM_USD, SpendLedger
from test_run_hardening import _RunnerTask

INIT_API_KEY = {"type": "system", "subtype": "init", "apiKeySource": "ANTHROPIC_API_KEY", "tools": []}
INIT_OK = {"type": "system", "subtype": "init", "apiKeySource": "none", "tools": []}
ASSISTANT = {"type": "assistant", "message": {"id": "msg_1", "content": [{"type": "text", "text": "hi"}],
                                               "usage": {"input_tokens": 1000, "output_tokens": 500}}}
ARGS = ("--models", "sonnet5", "--modes", "plain")


def budgets(tmp_path: Path) -> Path:
    return tmp_path / "bench-data" / "budgets"


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def only_budget(tmp_path: Path) -> Path:
    [path] = budgets(tmp_path).glob("*.jsonl")
    return path


def budget_id(out: str) -> str:
    return re.search(r"^Budget:\s+(\S+)", out, re.M).group(1)


def stream_with(path: Path, *events: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(event) + "\n" for event in events))


# --- AC-19: the live gate kills the subprocess ---


def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *events: dict) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    printed = "\n".join(f"print({json.dumps(json.dumps(event))}, flush=True)" for event in events)
    script.write_text(f"#!{sys.executable}\nimport time\n{printed}\ntime.sleep(30)\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    return script


@pytest.mark.parametrize("events, offending", [
    ((INIT_API_KEY,), INIT_API_KEY),
    ((ASSISTANT,), ASSISTANT),
], ids=["api-key-init", "assistant-before-init"])
def test_live_gate_kills_the_subprocess(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, events, offending) -> None:
    monkeypatch.setitem(run.TASKS, "auth_task", _RunnerTask())
    fake_claude(tmp_path, monkeypatch, *events)
    log = tmp_path / "streams" / "cell.jsonl"
    start = time.monotonic()

    with pytest.raises(auth.ClaudeAuthError):
        run._run_single_in_repo("auth_task", "baseline", "sonnet5", 0, tmp_path, stream_log_path=log)

    assert time.monotonic() - start < 15
    assert json.loads(log.read_text().splitlines()[0]) == offending


def test_live_gate_passes_a_none_key_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The kill is the gate's doing: a clean init and result finish normally."""
    monkeypatch.setitem(run.TASKS, "auth_task", _RunnerTask())
    result = {"type": "result", "subtype": "success", "is_error": False, "result": "answer", "total_cost_usd": 0.01}
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(f"#!{sys.executable}\nprint({json.dumps(json.dumps(INIT_OK))}, flush=True)\n"
                      f"print({json.dumps(json.dumps(result))}, flush=True)\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")

    row = run._run_single_in_repo("auth_task", "baseline", "sonnet5", 0, tmp_path,
                                  stream_log_path=tmp_path / "ok.jsonl")

    assert row["total_cost_usd"] == 0.01


# --- AC-19/20/13/14 through _run_plan ---


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    return make_world(monkeypatch, tmp_path)


def plan_with_failure(world, monkeypatch: pytest.MonkeyPatch, events: tuple, error: Exception, tmp_path: Path):
    def failing(task_name, mode_name, model_name, repetition, **kwargs):
        stream_with(kwargs["stream_log_path"], *events)
        raise error

    monkeypatch.setattr(run, "run_single", failing)
    ledger = SpendLedger.persistent("plan-test", 100.0)
    specs = [run.CellSpec(task, "baseline", "sonnet5", 0) for task in DEV]
    with pytest.raises(run.PlanStopped) as stopped:
        run.run_plan(specs, panel=world.panel, ledger=ledger, candidate_sha=None, refreeze_baselines=False,
                     cell_estimate_usd=0.9)
    return stopped.value, ledger


@pytest.mark.parametrize("events, source", [
    ((INIT_API_KEY, ASSISTANT), "pricing"),
    ((INIT_API_KEY,), "estimate"),
], ids=["usage-priced", "no-usage-estimate"])
def test_auth_killed_cell_is_stored_charged_and_stops_the_plan(
    world, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, events, source: str,
) -> None:
    stopped, ledger = plan_with_failure(world, monkeypatch, events, auth.ClaudeAuthError("apiKeySource"), tmp_path)

    assert stopped.reason == "auth"
    [row] = world.stored()
    assert row["infra"] == "auth" and row["error"].startswith("infra:auth:")
    assert row["cost_source"] == source
    assert (("total_cost_usd" in row) == (source == "pricing"))
    assert (row["charged_usd"] < 0.9) == (source == "pricing")
    assert row["charged_usd"] > 0
    assert ledger.charges == [(row["charged_usd"], source)]
    [record] = records(budgets(tmp_path) / "plan-test.jsonl")
    assert {k: v for k, v in record.items() if k != "at"} == {
        "run_key": row["run_key"], "charged_usd": row["charged_usd"], "cost_source": source}
    assert ledger.spent == pytest.approx(row["charged_usd"])


def test_non_auth_failed_cell_with_usage_is_priced(world, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def failing(task_name, mode_name, model_name, repetition, **kwargs):
        stream_with(kwargs["stream_log_path"], INIT_OK, ASSISTANT)
        raise RuntimeError("claude -p failed with code 1")

    monkeypatch.setattr(run, "run_single", failing)
    ledger = SpendLedger.persistent("plan-test", 100.0)

    rows = run.run_plan([run.CellSpec(DEV[0], "baseline", "sonnet5", 0)], panel=world.panel, ledger=ledger,
                        candidate_sha=None, refreeze_baselines=False, cell_estimate_usd=0.9)

    [row] = rows
    assert row["cost_source"] == "pricing" and 0 < row["charged_usd"] < 0.9
    assert row["total_cost_usd"] == row["charged_usd"]


# --- budgets through the CLI ---


def run_cli(bench, capsys, *extra: str, tasks: str = "cell_a,cell_b", reps: str = "1", max_usd: str | None = "5"):
    argv = ["--tasks", tasks, *ARGS, "--reps", reps, *(["--max-usd", max_usd] if max_usd else []), *extra]
    code = bench.main(*argv)
    return code, capsys.readouterr().out


def test_start_line_names_budget_and_rerun_shares_it(bench, capsys, tmp_path: Path) -> None:
    bench.runner(lambda _stream: 0.5)
    _, out = run_cli(bench, capsys)
    first_id = budget_id(out)
    bench.store_path.unlink()
    _, again = run_cli(bench, capsys)

    assert first_id.startswith("plan-")
    assert (budgets(tmp_path) / f"{first_id}.jsonl").is_file()
    assert budget_id(again) == first_id and "$1.0000 recorded" in again
    assert len(list(budgets(tmp_path).glob("*.jsonl"))) == 1
    assert [r["charged_usd"] for r in records(only_budget(tmp_path))] == [0.5] * 4
    assert all(r["run_key"] and r["cost_source"] == "native" for r in records(only_budget(tmp_path)))


def test_different_plan_gets_a_different_budget(bench, capsys, tmp_path: Path) -> None:
    bench.runner(lambda _stream: 0.1)
    _, first = run_cli(bench, capsys, tasks="cell_a")
    _, other_tasks = run_cli(bench, capsys, tasks="cell_b")
    _, other_reps = run_cli(bench, capsys, tasks="cell_a", reps="2")

    assert len({budget_id(first), budget_id(other_tasks), budget_id(other_reps)}) == 3
    assert len(list(budgets(tmp_path).glob("*.jsonl"))) == 3


def test_rerun_seeds_recorded_spend(bench, capsys, tmp_path: Path) -> None:
    bench.runner(lambda _stream: 0.5)
    _, first = run_cli(bench, capsys, tasks="cell_a,cell_b,cell_c", max_usd="1.2")
    assert "Budget:" in first and "$0.0000 recorded" in first
    bench.calls.clear()

    _, second = run_cli(bench, capsys, tasks="cell_a,cell_b,cell_c", max_usd="1.2")

    assert "$1.0000 recorded" in second
    assert bench.calls == []
    assert sum(r["charged_usd"] for r in records(only_budget(tmp_path))) == pytest.approx(1.0)


def stopped_run(bench, capsys, tmp_path: Path, **kwargs):
    """Run a three-cell plan that the 1.2 ceiling stops after two cells; return its budget id and output."""
    bench.runner(lambda _stream: 0.6)
    code, out = run_cli(bench, capsys, tasks="cell_a,cell_b,cell_c", max_usd="1.2", **kwargs)
    assert code != 0 and len(bench.calls) == 2
    bench.calls.clear()
    return budget_id(out), out


def test_rerun_with_no_headroom_starts_no_cell_and_names_budget(bench, capsys, tmp_path: Path) -> None:
    bid, first = stopped_run(bench, capsys, tmp_path)
    assert MIN_HEADROOM_USD > 0

    code, out = run_cli(bench, capsys, tasks="cell_a,cell_b,cell_c", max_usd="1.2")

    assert code != 0
    assert [c[0] for c in bench.calls] == []
    assert bid in out and "1.2000 recorded" in out and "--max-usd 1.2" in out


def test_ceiling_stop_message_names_budget_and_fresh_flag(bench, capsys, tmp_path: Path) -> None:
    bid, out = stopped_run(bench, capsys, tmp_path)

    assert f"budget {bid}" in out.split("Stop reason:")[1]
    assert "--fresh-budget" in out
    assert "can spend up to it again" not in out


def test_rerun_with_larger_ceiling_runs_against_recorded_spend(bench, capsys, tmp_path: Path) -> None:
    bid, _ = stopped_run(bench, capsys, tmp_path)

    code, out = run_cli(bench, capsys, tasks="cell_a,cell_b,cell_c", max_usd="3")

    assert code == 0
    assert [c[0] for c in bench.calls] == ["cell_c"]
    assert budget_id(out) == bid
    assert "$1.2000 recorded" in out
    assert sum(r["charged_usd"] for r in records(budgets(tmp_path) / f"{bid}.jsonl")) == pytest.approx(1.8)


def test_fresh_budget_resets_recorded_spend(bench, capsys, tmp_path: Path) -> None:
    bid, _ = stopped_run(bench, capsys, tmp_path)

    code, out = run_cli(bench, capsys, "--fresh-budget", tasks="cell_a,cell_b,cell_c", max_usd="1.2")

    assert budget_id(out) == bid and "$0.0000 recorded" in out
    assert bench.calls
    recs = records(budgets(tmp_path) / f"{bid}.jsonl")
    [marker] = [r for r in recs if r.get("reset")]
    assert set(marker) == {"reset", "at"} and marker["reset"] is True
    assert recs.index(marker) == 2


def test_named_budget_uses_its_own_file(bench, capsys, tmp_path: Path) -> None:
    bench.runner(lambda _stream: 0.25)

    _, out = run_cli(bench, capsys, "--budget-id", "my-budget", tasks="cell_a")

    assert budget_id(out) == "my-budget"
    assert [p.name for p in budgets(tmp_path).glob("*.jsonl")] == ["my-budget.jsonl"]
    assert records(budgets(tmp_path) / "my-budget.jsonl")[0]["charged_usd"] == 0.25


def test_fresh_budget_with_named_budget_resets_it(bench, capsys, tmp_path: Path) -> None:
    bench.runner(lambda _stream: 0.25)
    run_cli(bench, capsys, "--budget-id", "my-budget", tasks="cell_a")
    bench.calls.clear()

    bench.store_path.unlink()
    _, out = run_cli(bench, capsys, "--budget-id", "my-budget", "--fresh-budget", tasks="cell_a")

    assert "$0.0000 recorded" in out
    recs = records(budgets(tmp_path) / "my-budget.jsonl")
    assert [bool(r.get("reset")) for r in recs][:2] == [False, True]
    assert sum(1 for r in recs if r.get("reset")) == 1


def test_second_run_on_a_held_budget_is_refused_before_any_spend(bench, capsys, tmp_path: Path) -> None:
    """A concurrent run with the same --budget-id exits 75 and leaves the live run's output and budget alone."""
    output = tmp_path / "shared.jsonl"
    output.write_text('{"row": "live run"}\n')
    argv = ("--tasks", "cell_a", *ARGS, "--reps", "1", "--max-usd", "5", "--budget-id", "shared", "--output", str(output))
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, spend; lock = spend.hold_run_lock('shared'); print('held', flush=True); sys.stdin.read()"],
        cwd=Path(run.__file__).parent, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline() == "held\n"
        bench.runner(lambda _stream: pytest.fail("model call started"))

        code = bench.main(*argv)
    finally:
        holder.communicate("")

    err = capsys.readouterr().err
    assert code == 75
    assert "budget shared is in use by another run" in err
    assert bench.calls == []
    assert output.read_text() == '{"row": "live run"}\n'
    assert not (budgets(tmp_path) / "shared.jsonl").exists()

    bench.runner(lambda _stream: 0.25)
    assert bench.main(*argv) == 0
    assert len(bench.calls) == 1

# --- AC-15/AC-17 preflight through main ---


def test_without_token_a_token_status_refuses_before_any_cell(bench, monkeypatch: pytest.MonkeyPatch,
                                                               capsys) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    bench.runner(lambda _stream: pytest.fail("model call started"))

    code, _ = run_cli(bench, capsys, tasks="cell_a")

    assert code == 2 and bench.calls == []


def test_non_oauth_auth_status_refuses_before_any_cell(bench, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(auth, "_run_status", lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 0, '{"authMethod": "api_key"}', ""))
    bench.runner(lambda _stream: pytest.fail("model call started"))

    code = bench.main("--tasks", "cell_a", *ARGS, "--reps", "1", "--max-usd", "5")

    assert code == 2 and bench.calls == []
    assert "api_key" in capsys.readouterr().err


# --- cure: a budget line that turns unreadable mid-run, and codex-only plans ---


def tear_budget(path: Path) -> None:
    with open(path, "a") as out:
        out.write('{"run_key": "torn", "charged_usd": 5.0, "cost_sou\n')


def test_unreadable_budget_line_mid_run_stops_cleanly_and_keeps_the_paid_row(
        bench, capsys, tmp_path: Path) -> None:
    path = budgets(tmp_path) / "mid.jsonl"

    def paid_cell(_stream) -> float:
        tear_budget(path)
        return 0.5

    bench.runner(paid_cell)

    code, out = run_cli(bench, capsys, "--budget-id", "mid", tasks="cell_a,cell_b")

    assert code != 0 and len(bench.calls) == 1
    assert "mid.jsonl" in out.split("Stop reason:")[1]
    [row] = run.baselines.load_rows(bench.store_path)
    assert row["charged_usd"] == 0.5
    assert "Spend: $0.5000" in out


def test_unreadable_budget_line_mid_plan_stops_with_the_paid_row_stored(
        world, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = budgets(tmp_path) / "plan-test.jsonl"

    def failing(task_name, mode_name, model_name, repetition, **kwargs):
        stream_with(kwargs["stream_log_path"], INIT_OK, ASSISTANT)
        tear_budget(path)
        raise RuntimeError("claude -p failed with code 1")

    monkeypatch.setattr(run, "run_single", failing)
    ledger = SpendLedger.persistent("plan-test", 100.0)
    specs = [run.CellSpec(task, "baseline", "sonnet5", 0) for task in DEV]

    with pytest.raises(run.PlanStopped) as stopped:
        run.run_plan(specs, panel=world.panel, ledger=ledger, candidate_sha=None, refreeze_baselines=False,
                     cell_estimate_usd=0.9)

    assert stopped.value.reason == "ceiling" and "plan-test.jsonl" in str(stopped.value)
    [row] = world.stored()
    assert row["charged_usd"] > 0
    assert ledger.spent == pytest.approx(row["charged_usd"])


def test_codex_only_plan_ignores_an_anthropic_key_in_the_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SECRETVALUE")

    assert run.claude_auth_targets([("cell_a", "baseline", "gpt5")], bare=True) == []


def test_claude_cell_still_refuses_an_anthropic_key_in_the_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SECRETVALUE")

    with pytest.raises(auth.ClaudeAuthError, match="ANTHROPIC_API_KEY"):
        run.claude_auth_targets([("cell_a", "baseline", "gpt5"), ("cell_a", "baseline", "sonnet5")], bare=True)


def test_isolated_call_target_reuses_one_config_dir_per_process() -> None:
    first, second = run.isolated_call_target(), run.isolated_call_target()

    assert first.env["CLAUDE_CONFIG_DIR"] == second.env["CLAUDE_CONFIG_DIR"]
    assert first == second, "a new dir per call leaks a temp dir and defeats the auth status cache"
