"""Tests for ModeConfig.plugin_dir / prompt_mode: argv wiring and the
skill-availability guard (AC-2)."""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import run
from config import ModeConfig
from parse import RunResult
from tasks.base import TaskSource

FIXTURE_PLUGIN_DIR = str(Path(__file__).parent / "fixtures" / "probe_plugin")


@dataclass
class _PluginTask:
    repo: str = "synthetic"
    prompt: str = "Answer the task."
    capability: str = "trace"
    source: TaskSource = TaskSource(
        origin="fixture",
        license="MIT",
        commit_or_tag="test-pin",
        transformation="test-only",
    )

    def check_correctness(self, result_text: str, repo_path: str) -> tuple[bool, str]:
        return True, "expected answer"


def _fake_run_result(*, skills=None, plugins=None) -> RunResult:
    return RunResult(
        session_id="session",
        turns=[],
        num_turns=1,
        total_cost_usd=0.01,
        duration_ms=1,
        duration_api_ms=1,
        total_input_tokens=1,
        total_output_tokens=2,
        total_cache_creation_tokens=0,
        total_cache_read_tokens=0,
        result_text="correct answer",
        available_tools=["Read", "Skill"],
        mcp_servers=[],
        skills=skills if skills is not None else [],
        plugins=plugins if plugins is not None else [],
    )


def _install(monkeypatch, tmp_path, *, mode: ModeConfig, run_result: RunResult):
    task = _PluginTask()
    monkeypatch.setitem(run.TASKS, "plugin_task", task)
    monkeypatch.setitem(run.MODELS, "haiku", "configured-claude-model")
    monkeypatch.setitem(run.RUNNERS, "haiku", "claude")
    monkeypatch.setitem(run.MODES, mode.name, mode)
    monkeypatch.setattr(run, "get_repo_path", lambda _: tmp_path)
    monkeypatch.setattr(run, "parse_stream_json", lambda _: run_result)
    captured_cmds: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        captured_cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    monkeypatch.setattr(run.subprocess, "run", fake_run)
    return task, captured_cmds


def test_expected_plugin_skills_reads_plugin_manifest() -> None:
    assert run._expected_plugin_skills(FIXTURE_PLUGIN_DIR) == ["probe:demo"]


def test_plugin_dir_injects_flag_and_adds_skill_tool(monkeypatch, tmp_path) -> None:
    mode = ModeConfig(
        name="plugin_mode",
        tools=["Read"],
        mcp_config_path=None,
        description="plugin-armed test mode",
        plugin_dir=FIXTURE_PLUGIN_DIR,
    )
    _, captured_cmds = _install(
        monkeypatch, tmp_path,
        mode=mode,
        run_result=_fake_run_result(skills=["probe:demo"]),
    )

    run.run_single("plugin_task", "plugin_mode", "haiku", 0)

    cmd = captured_cmds[0]
    assert "--plugin-dir" in cmd
    assert cmd[cmd.index("--plugin-dir") + 1] == FIXTURE_PLUGIN_DIR
    tools_index = cmd.index("--tools")
    assert cmd[tools_index + 1] == "Read,Skill"


def test_plugin_dir_absent_leaves_argv_unchanged(monkeypatch, tmp_path) -> None:
    mode = ModeConfig(
        name="no_plugin_mode",
        tools=["Read"],
        mcp_config_path=None,
        description="no plugin",
    )
    _, captured_cmds = _install(
        monkeypatch, tmp_path,
        mode=mode,
        run_result=_fake_run_result(),
    )

    run.run_single("plugin_task", "no_plugin_mode", "haiku", 0)

    cmd = captured_cmds[0]
    assert "--plugin-dir" not in cmd
    assert "--system-prompt" in cmd
    tools_index = cmd.index("--tools")
    assert cmd[tools_index + 1] == "Read"


def test_skill_unavailable_raises_when_expected_skill_missing(monkeypatch, tmp_path) -> None:
    mode = ModeConfig(
        name="plugin_mode_missing",
        tools=["Read"],
        mcp_config_path=None,
        description="plugin-armed test mode",
        plugin_dir=FIXTURE_PLUGIN_DIR,
    )
    _install(
        monkeypatch, tmp_path,
        mode=mode,
        run_result=_fake_run_result(skills=[]),
    )

    with pytest.raises(run.SkillUnavailableError, match="probe:demo"):
        run.run_single("plugin_task", "plugin_mode_missing", "haiku", 0)


def test_row_records_skills_plugins_and_plugin_mode_fields(monkeypatch, tmp_path) -> None:
    mode = ModeConfig(
        name="plugin_mode_ok",
        tools=["Read"],
        mcp_config_path=None,
        description="plugin-armed test mode",
        plugin_dir=FIXTURE_PLUGIN_DIR,
        prompt_mode="append",
    )
    _install(
        monkeypatch, tmp_path,
        mode=mode,
        run_result=_fake_run_result(skills=["probe:demo"], plugins=[{"name": "probe"}]),
    )

    result = run.run_single("plugin_task", "plugin_mode_ok", "haiku", 0)

    assert result["skills"] == ["probe:demo"]
    assert result["plugins"] == [{"name": "probe"}]
    assert result["plugin_dir"] == FIXTURE_PLUGIN_DIR
    assert result["prompt_mode"] == "append"


def test_prompt_mode_append_uses_append_system_prompt_only(monkeypatch, tmp_path) -> None:
    mode = ModeConfig(
        name="append_mode",
        tools=["Read"],
        mcp_config_path=None,
        description="append prompt mode",
        prompt_mode="append",
    )
    _, captured_cmds = _install(
        monkeypatch, tmp_path,
        mode=mode,
        run_result=_fake_run_result(),
    )

    run.run_single("plugin_task", "append_mode", "haiku", 0)

    cmd = captured_cmds[0]
    assert "--system-prompt" not in cmd
    assert "--append-system-prompt" in cmd
    appended = cmd[cmd.index("--append-system-prompt") + 1]
    assert appended.startswith("Your current working directory is: ")
    assert "--append-system-prompt" not in cmd[: cmd.index("--append-system-prompt")]