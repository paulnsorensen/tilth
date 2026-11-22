"""A cell's leftover processes are killed, and a sharkdp/bat cell gets its agent-shell setting."""

import io
import os
import shutil
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import baselines
import run
from test_run_hardening import _STREAMS, _ReplayProcess, _RunnerTask

BAT_SETTING = {"CARGO_PROFILE_DEV_DEBUG_ASSERTIONS": "false"}
needs_proc = pytest.mark.skipif(not (Path("/proc").is_dir() and hasattr(os, "pidfd_open")),
                                reason="the sweep reads /proc and kills through a pidfd")
STREAM = (_STREAMS / "claude_native_cost.jsonl").read_text()
# The real Popen: some tests patch subprocess.Popen to stand in for the runner.
_POPEN = subprocess.Popen


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


@pytest.fixture
def processes() -> Iterator[list[subprocess.Popen]]:
    """Collect the test's own sleeps and kill any that are still alive at the end."""
    started: list[subprocess.Popen] = []
    yield started
    for process in started:
        process.kill()
        process.wait()


@pytest.fixture
def bat_cell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(run.TASKS, "bat_task", _BatTask())
    monkeypatch.setattr(run, "cli_version", lambda _runner: "2.1.0")
    monkeypatch.setattr(run, "env_fingerprint", lambda _task: "env-a")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # No test here may start the real claude CLI, even when a stand-in misses a call.
    monkeypatch.setattr(run, "resolve_claude_binary", lambda: "/nonexistent/claude")


def _sleep(processes: list[subprocess.Popen], cwd: Path | str, env: dict[str, str] | None = None,
           exe: str = "sleep") -> subprocess.Popen:
    """Start a sleep in a new session, as Claude Code starts a Bash command."""
    process = _POPEN([exe, "300"], cwd=cwd, env=env, start_new_session=True)
    processes.append(process)
    return process


class _HangingProcess:
    """A runner stand-in whose stdout stays open until the harness kills it."""

    def __init__(self) -> None:
        self.killed = threading.Event()
        self.stderr = io.StringIO("")
        self.stdout = self
        self.returncode: int | None = None

    def __iter__(self) -> Iterator[str]:
        self.killed.wait(30)
        return iter(())

    def close(self) -> None:
        return None

    def wait(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9
        self.killed.set()


@needs_proc
@pytest.mark.usefixtures("bat_cell")
def test_a_bat_cell_gets_the_setting_and_its_leftover_process_is_killed(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, processes: list[subprocess.Popen]) -> None:
    runner_env: dict[str, str] = {}

    def fake_claude(_cmd, **kwargs):
        runner_env.update(kwargs["env"])
        # The leftover runs outside the workspace, so only the cell marker in its env identifies it.
        _sleep(processes, "/", env=kwargs["env"])
        return _ReplayProcess(STREAM)

    monkeypatch.setattr(run.subprocess, "Popen", fake_claude)
    row = run._run_single_in_repo("bat_task", "baseline", "sonnet5", 0, tmp_path,
                                  stream_log_path=tmp_path / "streams" / "01_bat_task.jsonl")

    assert runner_env["CARGO_PROFILE_DEV_DEBUG_ASSERTIONS"] == "false"
    assert len(runner_env[run._CELL_MARKER]) == 32
    assert processes[0].wait(timeout=5) == -signal.SIGKILL
    assert row["correct"] is True


@needs_proc
@pytest.mark.usefixtures("bat_cell")
def test_a_timed_out_cell_kills_its_leftover_process(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, processes: list[subprocess.Popen]) -> None:
    def hanging_claude(_cmd, **kwargs):
        _sleep(processes, "/", env=kwargs["env"])
        return _HangingProcess()

    monkeypatch.setattr(run.subprocess, "Popen", hanging_claude)
    monkeypatch.setattr(run, "_task_timeout", lambda _task: 0.2)
    with pytest.raises(subprocess.TimeoutExpired):
        run._run_single_in_repo("bat_task", "baseline", "sonnet5", 0, tmp_path,
                                stream_log_path=tmp_path / "streams" / "01_bat_task.jsonl")

    assert processes[0].wait(timeout=5) == -signal.SIGKILL


@needs_proc
@pytest.mark.usefixtures("bat_cell")
def test_a_cell_without_a_stream_log_kills_its_leftover_process(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, processes: list[subprocess.Popen]) -> None:
    real_run = subprocess.run
    # A missing path, so a call this fake does not catch can never start a real runner.
    runner = str(tmp_path / "no-such-claude")
    monkeypatch.setattr(run, "resolve_claude_binary", lambda: runner)

    def fake_run(cmd, *args, **kwargs):
        if cmd[0] != runner:
            return real_run(cmd, *args, **kwargs)
        _sleep(processes, "/", env=kwargs["env"])
        return subprocess.CompletedProcess(cmd, 0, stdout=STREAM, stderr="")

    monkeypatch.setattr(run.subprocess, "run", fake_run)
    row = run._run_single_in_repo("bat_task", "baseline", "sonnet5", 0, tmp_path, stream_log_path=None)

    assert processes[0].wait(timeout=5) == -signal.SIGKILL
    assert row["correct"] is True


@needs_proc
def test_the_sweep_kills_only_the_cell_processes(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, processes: list[subprocess.Popen]) -> None:
    workspace, elsewhere = tmp_path / "repo", tmp_path / "other"
    workspace.mkdir()
    elsewhere.mkdir()
    built = shutil.copy(shutil.which("sleep"), workspace / "sleep")
    marked = _sleep(processes, elsewhere, env={**os.environ, run._CELL_MARKER: "cell-a"})
    other_cell = _sleep(processes, elsewhere, env={**os.environ, run._CELL_MARKER: "cell-b"})
    # A developer shell that is cd'd into the workspace is not part of the cell.
    visitor = _sleep(processes, workspace)
    # A binary the cell built, run with a cleared env, is part of the cell.
    cleared = _sleep(processes, elsewhere, env={}, exe=str(built))

    assert run._reap_cell_processes(workspace, "cell-a") == 2
    assert marked.wait(timeout=5) == -signal.SIGKILL
    assert cleared.wait(timeout=5) == -signal.SIGKILL
    assert other_cell.poll() is None and visitor.poll() is None


@needs_proc
def test_the_executable_rule_needs_a_root_strictly_under_the_temp_dir(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, processes: list[subprocess.Popen]) -> None:
    # tmp_path stands in for the temp dir, so a broken guard can reach only this test's own sleep.
    monkeypatch.setattr(run.tempfile, "gettempdir", lambda: str(tmp_path))
    built = shutil.copy(shutil.which("sleep"), tmp_path / "sleep")
    cleared = _sleep(processes, tmp_path, env={}, exe=str(built))

    assert run._reap_cell_processes(tmp_path, "no-such-cell") == 0
    assert cleared.poll() is None
