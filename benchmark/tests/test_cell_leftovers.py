"""A cell's leftover processes are killed, and a sharkdp/bat cell gets its agent-shell setting."""

import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import baselines
import run
from test_run_hardening import _STREAMS, _ReplayProcess, _RunnerTask

BAT_SETTING = {"CARGO_PROFILE_DEV_DEBUG_ASSERTIONS": "false"}
needs_proc = pytest.mark.skipif(not Path("/proc").is_dir(), reason="the sweep reads /proc")


@dataclass
class _BatTask(_RunnerTask):
    name: str = "bat_task"
    upstream_repo: str = "sharkdp/bat"


def _harness_digest(task_name: str) -> str:
    return run.cell_identity(task_name, "baseline", "sonnet5", 0, bare=False, reasoning_effort=None,
                             max_budget_usd=1.0, strict_file_tools=False)["harness_digest"]


def test_only_a_bat_task_gets_the_debug_assertion_setting() -> None:
    assert run._agent_env(SimpleNamespace(upstream_repo="sharkdp/bat")) == BAT_SETTING
    assert run._agent_env(SimpleNamespace(upstream_repo="tokio-rs/tokio")) == {}
    assert run._agent_env(_RunnerTask()) == {}


def test_the_setting_changes_the_key_of_a_bat_cell_only(monkeypatch: pytest.MonkeyPatch) -> None:
    common = dict(system_prompt="p", tools=["Bash"], strict_file_tools=False, bare=False,
                  max_budget_usd=1.0, mcp_shape={})
    plain = baselines.harness_digest(**common)
    assert baselines.harness_digest(**common, agent_env={}) == plain
    assert baselines.harness_digest(**common, agent_env=BAT_SETTING) != plain

    monkeypatch.setattr(run, "cli_version", lambda _runner: "2.1.0")
    monkeypatch.setattr(run, "env_fingerprint", lambda _task: "env-a")
    monkeypatch.setitem(run.TASKS, "bat_task", _BatTask())
    monkeypatch.setitem(run.TASKS, "plain_task", _RunnerTask(name="plain_task"))
    bat_with, plain_with = _harness_digest("bat_task"), _harness_digest("plain_task")
    monkeypatch.setattr(run, "_AGENT_ENV_BY_REPO", {})

    assert _harness_digest("bat_task") != bat_with
    assert _harness_digest("plain_task") == plain_with


@needs_proc
def test_a_bat_cell_gets_the_setting_and_its_leftover_process_is_killed(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(run.TASKS, "bat_task", _BatTask())
    monkeypatch.setattr(run, "cli_version", lambda _runner: "2.1.0")
    monkeypatch.setattr(run, "env_fingerprint", lambda _task: "env-a")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    stream = (_STREAMS / "claude_native_cost.jsonl").read_text()
    real_popen = subprocess.Popen
    runner_env: dict[str, str] = {}
    leftovers: list[subprocess.Popen] = []

    def fake_claude(_cmd, **kwargs):
        runner_env.update(kwargs["env"])
        # Claude Code runs a Bash command in a new session; a test it runs can leave a process there.
        leftovers.append(real_popen(["sleep", "300"], cwd=kwargs["cwd"], start_new_session=True))
        return _ReplayProcess(stream)

    monkeypatch.setattr(run.subprocess, "Popen", fake_claude)
    try:
        row = run._run_single_in_repo("bat_task", "baseline", "sonnet5", 0, tmp_path,
                                      stream_log_path=tmp_path / "streams" / "01_bat_task.jsonl")

        assert runner_env["CARGO_PROFILE_DEV_DEBUG_ASSERTIONS"] == "false"
        assert leftovers[0].wait(timeout=5) == -signal.SIGKILL
        assert row["correct"] is True
    finally:
        for process in leftovers:
            process.kill()
            process.wait()


@needs_proc
def test_the_sweep_kills_only_processes_under_the_workspace(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    inside, outside = tmp_path / "repo", tmp_path / "other"
    inside.mkdir()
    outside.mkdir()
    leftover = subprocess.Popen(["sleep", "300"], cwd=inside, start_new_session=True)
    neighbour = subprocess.Popen(["sleep", "300"], cwd=outside, start_new_session=True)
    try:
        assert run._reap_workspace_processes(inside) == 1
        assert leftover.wait(timeout=5) == -signal.SIGKILL
        assert neighbour.poll() is None
        # A root outside the temp dir, or the temp dir itself, is never swept. tmp_path stands in for
        # the temp dir, so a broken guard can reach only this test's own processes.
        monkeypatch.setattr(run.tempfile, "gettempdir", lambda: str(tmp_path))
        assert run._reap_workspace_processes(Path("/")) == 0
        assert run._reap_workspace_processes(tmp_path) == 0
        assert neighbour.poll() is None
    finally:
        for process in (leftover, neighbour):
            process.kill()
            process.wait()
