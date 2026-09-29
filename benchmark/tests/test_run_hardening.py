"""Focused tests for runner isolation and result metadata."""

import hashlib
import io
import json
import os
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import run
from config import ModeConfig
from parse import RunResult, Turn
from tasks.base import TaskSource

_RUNTIME_KEYS = {
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
    "TERM",
    "LANG",
    "LANGUAGE",
    "XDG_DATA_HOME",
    "GOCACHE",
}
_AUTH_KEYS = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_CUSTOM_AUTH",
    "OPENAI_API_KEY",
    "OPENAI_ORG_ID",
    "OPENROUTER_API_KEY",
    "OPENROUTER_SITE_URL",
    "CODEX_API_KEY",
}


@pytest.mark.parametrize(
    ("runner", "opencode_config", "bare"),
    [
        ("claude", None, False),
        ("codex", None, False),
        ("opencode", "/controlled/opencode.json", False),
        ("opencode", "/controlled/opencode.json", True),
    ],
)
def test_build_runner_env_allowlists_ambient_environment(
    monkeypatch: pytest.MonkeyPatch,
    runner: str,
    opencode_config: str | None,
    bare: bool,
) -> None:
    """Each lane gets runtime/auth values but no unrelated host secret."""
    values = {key: f"value-for-{key}" for key in _RUNTIME_KEYS | _AUTH_KEYS}
    values["PATH"] = "/usr/bin"
    values["LC_CUSTOM"] = "custom-locale"
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    monkeypatch.setenv("SENTINEL_SECRET", "do-not-forward")
    monkeypatch.setenv("CLAUDECODE", "nested-session")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "claude-only-token")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/controlled/claude-config")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "do-not-forward")
    monkeypatch.setenv("MCP_SERVER_SECRET", "do-not-forward")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/ambient/config")
    monkeypatch.setenv("OPENCODE_CONFIG", "/ambient/opencode.json")
    monkeypatch.setattr(run, "TILTH_BIN", "/opt/tilth/bin/tilth")

    ambient = dict(os.environ)
    expected = {
        key: value
        for key, value in ambient.items()
        if key in _RUNTIME_KEYS
        or key == "CODEX_API_KEY"
        or key.startswith(("ANTHROPIC_", "OPENAI_", "OPENROUTER_", "LC_"))
    }
    expected["PATH"] = f"/opt/tilth/bin{os.pathsep}{ambient['PATH']}"
    if runner == "claude":
        expected["CLAUDE_CODE_OAUTH_TOKEN"] = "claude-only-token"
        expected["CLAUDE_CONFIG_DIR"] = "/controlled/claude-config"
    if runner == "opencode":
        expected["OPENCODE_CONFIG"] = opencode_config
        expected["XDG_CONFIG_HOME"] = str(run.OPENCODE_CONFIG_HOME)
    if bare:
        expected.update(
            {
                "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
                "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
                "OPENCODE_DISABLE_CLAUDE_CODE": "1",
                "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
            }
        )

    env = run.build_runner_env(
        runner,
        opencode_config=opencode_config,
        bare=bare,
    )

    assert env == expected
    assert env["GOCACHE"] == "value-for-GOCACHE"
    assert "SENTINEL_SECRET" not in env
    assert "CLAUDECODE" not in env
    assert "MCP_SERVER_SECRET" not in env
    assert not any(key.startswith("CLAUDE_") and key not in {"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"} for key in env)
    if runner != "opencode":
        assert "OPENCODE_CONFIG" not in env
        assert "XDG_CONFIG_HOME" not in env


def test_no_tilth_arm_does_not_prepend_a_tilth_binary_to_path() -> None:
    env = run.build_runner_env(
        "claude",
        ambient={"PATH": "/usr/bin"},
        tilth_bin=None,
    )

    assert env["PATH"] == "/usr/bin"


def test_agent_repo_export_excludes_repository_metadata(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / ".git").mkdir()
    (source / ".git_hidden").mkdir()
    tracked = source / "tracked.py"
    tracked.write_text("mutated")

    with run._agent_repo(source, hide_git=True) as workspace:
        assert workspace != source
        assert (workspace / "tracked.py").read_text() == "mutated"
        assert not (workspace / ".git").exists()
        assert not (workspace / ".git_hidden").exists()
        (workspace / "tracked.py").write_text("agent fix")

    assert tracked.read_text() == "mutated"


def test_agent_repo_is_fresh_even_when_git_remains_visible(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    tracked = source / "tracked.py"
    tracked.write_text("clean")

    with run._agent_repo(source, hide_git=False) as workspace:
        assert workspace != source
        (workspace / "tracked.py").write_text("agent edit")

    assert tracked.read_text() == "clean"



def test_agent_repo_uses_disposable_git_worktree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    tracked = source / "tracked.py"
    tracked.write_text("clean")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(
        ["git", "-C", str(source), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(source), "config", "user.name", "Benchmark Test"],
        check=True,
    )
    subprocess.run(["git", "-C", str(source), "add", "tracked.py"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "fixture"], check=True)

    with run._agent_repo(source, hide_git=False) as workspace:
        assert (workspace / ".git").is_file()
        (workspace / "tracked.py").write_text("agent edit")

    assert tracked.read_text() == "clean"

@dataclass
class _RunnerTask:
    repo: str = "synthetic"
    prompt: str = "Answer the task."
    capability: str = "trace"
    hide_git: bool = False
    source: TaskSource = TaskSource(
        origin="fixture",
        license="MIT",
        commit_or_tag="test-pin",
        transformation="test-only",
    )

    def check_correctness(self, result_text: str, repo_path: str) -> tuple[bool, str]:
        return True, "expected answer"


@pytest.mark.parametrize(
    ("alias", "runner", "model_id", "parser_name"),
    [
        ("haiku", "claude", "configured-claude-model", "parse_stream_json"),
        ("gpt5", "codex", "configured-codex-model", "parse_codex_json"),
        ("gpt5mini", "opencode", "configured-opencode-model", "parse_opencode_json"),
    ],
)
def test_run_single_uses_allowlisted_env_and_preserves_runner_flags(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    alias: str,
    runner: str,
    model_id: str,
    parser_name: str,
) -> None:
    mcp_path = tmp_path / "tilth_mcp.json"
    mcp_path.write_text(json.dumps({"mcpServers": {"tilth": {"command": "tilth", "args": ["--mcp", "--edit"]}}}))
    task = _RunnerTask()
    monkeypatch.setitem(run.TASKS, "runner_test_task", task)
    monkeypatch.setitem(run.MODELS, alias, model_id)
    monkeypatch.setitem(run.RUNNERS, alias, runner)
    monkeypatch.setitem(
        run.MODES,
        "runner_test_mode",
        ModeConfig(
            name="runner_test_mode",
            tools=["Read", "Edit"],
            mcp_config_path=str(mcp_path),
            description="test mode",
            binary_path="/opt/tilth/bin/tilth",
            repository="https://github.com/example/tilth",
            git_ref="feature/candidate",
            git_sha="a" * 40,
            binary_sha256="b" * 64,
            tilth_version="9.9.9",
            rustc_version="rustc 1.99.0",
        ),
    )
    monkeypatch.setitem(run.OPENCODE_CONFIGS, "runner_test_mode", "/controlled/opencode.json")
    monkeypatch.setattr(run, "get_repo_path", lambda _: tmp_path)
    monkeypatch.setattr(run, "_tilth_version", lambda: "test-version")
    monkeypatch.setenv("SENTINEL_SECRET", "do-not-forward")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(run, "TILTH_BIN", "/opt/tilth/bin/tilth")

    parsed = RunResult(
        session_id="session",
        turns=[
            Turn(
                index=0,
                input_tokens=3,
                output_tokens=17,
                cache_creation_tokens=2,
                cache_read_tokens=1,
                cache_creation_5m_tokens=1,
                cache_creation_1h_tokens=1,
            ),
        ],
        num_turns=1,
        total_cost_usd=0.01,
        duration_ms=12,
        duration_api_ms=10,
        total_input_tokens=3,
        total_output_tokens=17,
        total_cache_creation_tokens=2,
        total_cache_read_tokens=1,
        result_text="correct answer",
        available_tools=["Read", "Edit", "mcp__tilth__tilth_search"],
        mcp_servers=[{"name": "tilth", "status": "connected"}],
    )
    parser_calls: list[tuple] = []

    def fake_parser(*args):
        parser_calls.append(args)
        return parsed

    monkeypatch.setattr(run, parser_name, fake_parser)
    captured: dict[str, object] = {}

    def fake_subprocess_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        captured["stdin"] = kwargs.get("stdin")
        raw = json.dumps({"type": "result", "subtype": "success", "is_error": False}) if runner == "claude" else "{}"
        if runner == "codex":
            raw = json.dumps({"type": "item.completed", "item": {
                "type": "mcp_tool_call", "server": "tilth", "tool": "tilth_read",
                "status": "completed", "result": {"content": []}, "error": None,
                "arguments": {"paths": ["a.py", "b.py"]},
            }})
        return run.subprocess.CompletedProcess(cmd, 0, stdout=raw, stderr="")

    monkeypatch.setattr(run.subprocess, "run", fake_subprocess_run)

    result = run.run_single(
        "runner_test_task",
        "runner_test_mode",
        alias,
        0,
        bare=True,
    )

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["PATH"] == f"/opt/tilth/bin{os.pathsep}/usr/bin"
    assert "SENTINEL_SECRET" not in env
    assert captured["stdin"] is run.subprocess.DEVNULL
    if runner == "codex":
        assert parser_calls == [(json.dumps({"type": "item.completed", "item": {
            "type": "mcp_tool_call", "server": "tilth", "tool": "tilth_read",
            "status": "completed", "result": {"content": []}, "error": None,
            "arguments": {"paths": ["a.py", "b.py"]},
        }}), model_id)]
    elif runner == "claude":
        assert parser_calls == [(json.dumps({"type": "result", "subtype": "success", "is_error": False}),)]
    else:
        assert parser_calls == [("{}",)]

    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    if runner == "claude":
        assert cmd[:2] == ["claude", "-p"]
        assert "--output-format" in cmd and "stream-json" in cmd
        assert "--strict-mcp-config" in cmd
        config = json.loads(cmd[cmd.index("--mcp-config") + 1])
        assert config["mcpServers"]["tilth"]["command"] == "/opt/tilth/bin/tilth"
        sources_index = cmd.index("--setting-sources")
        assert cmd[sources_index + 1] == ""
        assert "--safe-mode" not in cmd
        assert "--bare" not in cmd
        assert not any("approval_mode" in arg for arg in cmd)
    elif runner == "codex":
        assert cmd[:3] == ["codex", "exec", "--json"]
        assert "--full-auto" not in cmd
        assert "--ephemeral" in cmd and "--ignore-user-config" in cmd
        assert "--ignore-rules" in cmd
        assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
        assert "mcp_servers={}" in cmd
        assert 'mcp_servers.tilth.required=true' in cmd
        assert any("mcp_servers.tilth.command" in arg for arg in cmd)
        assert 'mcp_servers.tilth.tools.tilth_write.approval_mode="approve"' in cmd
        assert not any(arg.startswith("approval_policy=") for arg in cmd)
        assert "skill_search" in cmd and "skip_host_skill_discovery" in cmd
        instructions = next(arg for arg in cmd if arg.startswith("developer_instructions="))
        assert "Use tilth MCP first" in instructions
        assert "Batch independent files" in instructions
        assert "Native tools remain available" in instructions
        assert cmd[-1] == task.prompt
        assert result["successful_tilth_mcp_calls"] == 1
    else:
        assert cmd[:4] == ["opencode", "run", "--format", "json"]
        assert "--dir" in cmd and "--model" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "--pure" in cmd
        assert not any("approval_mode" in arg for arg in cmd)

    assert result["model"] == model_id
    assert result["model_alias"] == alias
    assert result["capability"] == "trace"
    assert result["source"] == {
        "origin": "fixture",
        "license": "MIT",
        "commit_or_tag": "test-pin",
        "transformation": "test-only",
    }
    assert result["per_turn_output_tokens"] == [17]
    assert result["cache_creation_5m_tokens"] == 1
    assert result["cache_creation_1h_tokens"] == 1
    assert result["per_turn_token_usage"] == [
        {
            "input_tokens": 3,
            "cache_creation_tokens": 2,
            "cache_creation_5m_tokens": 1,
            "cache_creation_1h_tokens": 1,
            "cache_read_tokens": 1,
            "output_tokens": 17,
        }
    ]
    assert result["variant"] == {
        "label": "runner_test_mode",
        "plugin_dir": None,
        "plugin_version": None,
        "plugin_git_sha": None,
        "repository": "https://github.com/example/tilth",
        "git_ref": "feature/candidate",
        "git_sha": "a" * 40,
        "binary_path": "/opt/tilth/bin/tilth",
        "binary_sha256": "b" * 64,
        "tilth_version": "9.9.9",
        "rustc_version": "rustc 1.99.0",
    }




def test_codex_selection_rejects_non_codex_models_and_forced_mode():
    assert run.select_models("all", "codex") == [name for name in run.MODELS if run.RUNNERS[name] == "codex"]
    assert run.select_models(None, "codex") == ["gpt5"]
    assert run.select_modes(None, "codex") == ["baseline", "tilth"]
    with pytest.raises(ValueError, match="not a codex model"):
        run.select_models("sonnet", "codex")
    with pytest.raises(ValueError, match="tilth_forced"):
        run.select_modes("tilth_forced", "codex")


@pytest.mark.parametrize("returncode", [0, 1])
def test_codex_stream_is_saved_even_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, returncode: int,
) -> None:
    monkeypatch.setitem(run.TASKS, "codex_stream_task", _RunnerTask())
    events = [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "answer"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]
    raw = "\n".join(json.dumps(event) for event in events) + "\n"

    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = io.StringIO(raw)
            self.stderr = io.StringIO("failure" if returncode else "")
            self.returncode = returncode

        def wait(self) -> None:
            return None

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr(run.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())
    log_path = tmp_path / "codex.jsonl"
    if returncode:
        with pytest.raises(RuntimeError, match="codex exec failed"):
            run._run_single_in_repo("codex_stream_task", "baseline", "gpt5", 0, tmp_path, stream_log_path=log_path)
    else:
        result = run._run_single_in_repo("codex_stream_task", "baseline", "gpt5", 0, tmp_path, stream_log_path=log_path)
        assert result["result_text"] == "answer"
    assert log_path.read_text() == raw
    assert log_path.with_suffix(".stderr").read_text() == ("failure" if returncode else "")


def test_codex_config_escapes_paths_and_preserves_auth_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    task = _RunnerTask(hide_git=True)
    monkeypatch.setitem(run.TASKS, "codex_quoted_task", task)
    quoted_bin = '/tmp/tilth "quoted"/bin/tilth'
    monkeypatch.setitem(run.MODES, "codex_quoted_mode", ModeConfig(
        name="codex_quoted_mode", tools=[], mcp_config_path="/tmp/tilth.json",
        description="test", binary_path=quoted_bin, tilth_version="test",
    ))
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        events = [
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "tilth",
                "tool": "tilth_read", "status": "completed", "result": {"content": []},
                "error": None, "arguments": {"paths": ["a.py", "b.py"]}}},
            {"type": "turn.completed", "usage": {}},
        ]
        return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(map(json.dumps, events)), stderr="")

    monkeypatch.setattr(run.subprocess, "run", fake_run)
    home = tmp_path / 'home "quoted"'
    skill = home / ".agents" / "skills" / "my skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("skill")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", "/saved/codex")
    run._run_single_in_repo("codex_quoted_task", "codex_quoted_mode", "gpt5", 0, tmp_path)
    assert "--skip-git-repo-check" in captured["cmd"]
    assert f"mcp_servers.tilth.command={json.dumps(quoted_bin)}" in captured["cmd"]
    assert captured["env"]["CODEX_HOME"] == "/saved/codex"
    skill_override = f'{{path={json.dumps(str(skill.resolve()))},enabled=false}}'
    assert any(skill_override in arg for arg in captured["cmd"])
    assert captured["env"]["HOME"] == str(home)


def test_codex_skill_discovery_follows_links_and_deduplicates(tmp_path: Path) -> None:
    home = tmp_path / "home"
    skill = home / ".agents" / "skills" / "cook"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("skill")
    (home / ".agents" / "skills" / "alias").symlink_to(skill)
    (skill / "loop").symlink_to(home / ".agents" / "skills")
    repo = tmp_path / "repo"
    repo.mkdir()
    project_skill = repo / ".agents" / "skills" / "project" / "SKILL.md"
    project_skill.parent.mkdir(parents=True)
    project_skill.write_text("project")
    paths = run._codex_skill_paths(repo, {"HOME": str(home), "CODEX_HOME": str(home / ".codex")})
    assert paths == sorted([str((skill / "SKILL.md").resolve()), str(project_skill.resolve())])


def test_codex_raw_stream_validity_checks_completed_calls_and_skill_access(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    skill = root / "cook" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text("skill")
    call = {"type": "mcp_tool_call", "server": "tilth", "tool": "tilth_read",
            "arguments": {"cwd": str(tmp_path), "paths": ["a.py", "b.py"]},
            "status": "completed", "result": {"content": []}, "error": None}
    def raw(event_type, item):
        return json.dumps({"type": event_type, "item": item})
    for item in [dict(call, status="failed"), dict(call, result={"isError": True}),
                 dict(call, error="server error")]:
        with pytest.raises(run.InvalidCodexCellError, match="successful tilth"):
            run._validate_codex_stream(raw("item.completed", item), True, [str(skill)], [str(root)])
    with pytest.raises(run.InvalidCodexCellError, match="successful tilth"):
        run._validate_codex_stream(raw("item.started", call), True, [str(skill)], [str(root)])
    assert run._validate_codex_stream(raw("item.completed", call), False, [str(skill)], [str(root)]) == 1
    assert run._validate_codex_stream(raw("item.completed", call), True, [str(skill)], [str(root)]) == 1
    host_call = {"type": "command_execution", "command": f"cat {skill}"}
    with pytest.raises(run.InvalidCodexCellError, match="host skill"):
        run._validate_codex_stream(raw("item.started", host_call), False, [str(skill)], [str(root)])
    for relative in (".agents/skills/project/SKILL.md", "../.agents/skills/project/SKILL.md",
                     "../../.codex/skills/project/SKILL.md"):
        item = {"type": "command_execution", "command": f"cat {relative}"}
        with pytest.raises(run.InvalidCodexCellError, match="host skill"):
            run._validate_codex_stream(raw("item.started", item), False, [str(skill)], [str(root)])
    prose = {"type": "agent_message", "text": "I did not read .agents/skills/project/SKILL.md"}
    assert run._validate_codex_stream(raw("item.completed", prose), False, [str(skill)], [str(root)]) == 0


def test_codex_invalid_cell_aborts_schedule(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "synthetic"
    source.mkdir()
    monkeypatch.setitem(run.TASKS, "invalid_task", _RunnerTask())
    monkeypatch.setattr(run, "SYNTHETIC_REPO", source)
    monkeypatch.setattr(run, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(run, "reset_repo", lambda: None)
    calls = []
    def invalid(*_args, **_kwargs):
        calls.append(1)
        raise run.InvalidCodexCellError("no successful tilth call")
    monkeypatch.setattr(run, "run_single", invalid)
    monkeypatch.setattr(sys, "argv", ["run.py", "--runner", "codex", "--models", "luna56",
        "--tasks", "invalid_task", "--modes", "tilth", "--reps", "2", "--max-cells", "2"])
    with pytest.raises(SystemExit) as error:
        run.main()
    assert error.value.code == 1
    assert calls == [1]
    row = json.loads(next((tmp_path / "results").glob("benchmark_*.jsonl")).read_text())
    assert row["error"].startswith("invalid_codex_cell:")


def test_codex_preflight_checks_selected_binary_not_claude_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mode = ModeConfig(
        name="codex_preflight_mode", tools=[], mcp_config_path="/missing/claude.json",
        description="test", binary_path="/selected/tilth",
    )
    monkeypatch.setitem(run.MODES, mode.name, mode)
    assert run.mcp_server_commands(mode.name, "codex") == {"tilth": "/selected/tilth"}


def test_codex_reasoning_effort_reaches_command_and_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setitem(run.TASKS, "luna_task", _RunnerTask())
    captured = {}

    def fake_run(cmd, **_kwargs):
        captured["cmd"] = cmd
        events = [
            {"type": "turn.started"},
            {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
        ]
        return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(json.dumps(event) for event in events), stderr="")

    monkeypatch.setattr(run.subprocess, "run", fake_run)
    result = run._run_single_in_repo("luna_task", "baseline", "luna56", 0, tmp_path, reasoning_effort="xhigh")
    assert 'model_reasoning_effort="xhigh"' in captured["cmd"]
    assert result["reasoning_effort"] == "xhigh"
    assert not any("approval_mode" in arg for arg in captured["cmd"])
    instructions = next(arg for arg in captured["cmd"] if arg.startswith("developer_instructions="))
    assert "Batch independent source reads" in instructions
    assert "Use tilth MCP first" not in instructions
    assert result["successful_tilth_mcp_calls"] == 0


def test_reasoning_effort_rejects_opencode_before_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(run, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(sys, "argv", ["run.py", "--models", "gpt5mini", "--reasoning-effort", "xhigh"])
    monkeypatch.setattr(run, "run_single", lambda *_args, **_kwargs: pytest.fail("model call started"))
    with pytest.raises(SystemExit) as error:
        run.main()
    assert error.value.code == 2
    assert "--reasoning-effort is not supported by OpenCode" in capsys.readouterr().err


@pytest.mark.parametrize("fails", [False, True])
def test_luna_reasoning_effort_is_recorded_for_each_cell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fails: bool,
) -> None:
    source = tmp_path / "synthetic"
    source.mkdir()
    output_dir = tmp_path / "results"
    monkeypatch.setitem(run.TASKS, "luna_task", _RunnerTask())
    monkeypatch.setattr(run, "SYNTHETIC_REPO", source)
    monkeypatch.setattr(run, "RESULTS_DIR", output_dir)
    monkeypatch.setattr(run, "reset_repo", lambda: None)
    seen = []

    def fake_run_single(*_args, **kwargs):
        seen.append(kwargs["reasoning_effort"])
        if fails:
            raise RuntimeError("test failure")
        return {
            "model": "gpt-5.6-luna",
            "correct": True, "num_turns": 1, "context_tokens": 1,
            "output_tokens": 1, "total_cost_usd": 0.0, "duration_ms": 1,
        }

    monkeypatch.setattr(run, "run_single", fake_run_single)
    monkeypatch.setattr(sys, "argv", [
        "run.py", "--runner", "codex", "--models", "luna56",
        "--tasks", "luna_task", "--modes", "baseline", "--reps", "1",
        "--max-cells", "1", "--reasoning-effort", "xhigh",
    ])
    run.main()
    row = json.loads(next(output_dir.glob("benchmark_*.jsonl")).read_text())
    assert seen == ["xhigh"]
    assert row["model"] == "gpt-5.6-luna"
    assert row["reasoning_effort"] == "xhigh"
    assert ("error" in row) is fails


def test_luna_xhigh_cli_reaches_codex_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    source = tmp_path / "synthetic"
    source.mkdir()
    (source / "fixture.txt").write_text("fixture")
    output_dir = tmp_path / "results"
    monkeypatch.setitem(run.TASKS, "luna_task", _RunnerTask())
    monkeypatch.setattr(run, "SYNTHETIC_REPO", source)
    monkeypatch.setattr(run, "RESULTS_DIR", output_dir)
    monkeypatch.setattr(run, "reset_repo", lambda: None)
    captured = {}

    class FakeProcess:
        def __init__(self) -> None:
            events = [
                {"type": "turn.started"},
                {"type": "item.completed", "item": {"type": "agent_message", "text": "answer"}},
                {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
            ]
            self.stdout = io.StringIO("\n".join(json.dumps(event) for event in events) + "\n")
            self.stderr = io.StringIO("")
            self.returncode = 0

        def wait(self) -> None:
            return None

        def kill(self) -> None:
            self.returncode = -9

    def fake_popen(command, **_kwargs):
        captured["command"] = command
        return FakeProcess()

    monkeypatch.setattr(run.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(sys, "argv", [
        "run.py", "--runner", "codex", "--models", "luna56",
        "--tasks", "luna_task", "--modes", "baseline", "--reps", "1",
        "--max-cells", "1", "--reasoning-effort", "xhigh",
    ])
    run.main()
    row = json.loads(next(output_dir.glob("benchmark_*.jsonl")).read_text())
    assert 'model_reasoning_effort="xhigh"' in captured["command"]
    assert row["reasoning_effort"] == "xhigh"
    assert row["correct"] is True


def test_claude_streaming_run_does_not_inherit_stdin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    task = _RunnerTask()
    monkeypatch.setitem(run.TASKS, "runner_stream_task", task)
    monkeypatch.setitem(run.MODELS, "haiku", "configured-claude-model")
    monkeypatch.setitem(run.RUNNERS, "haiku", "claude")
    monkeypatch.setitem(
        run.MODES,
        "runner_stream_mode",
        ModeConfig(
            name="runner_stream_mode",
            tools=["Read"],
            mcp_config_path=None,
            description="streaming test",
        ),
    )
    monkeypatch.setattr(run, "get_repo_path", lambda _: tmp_path)
    monkeypatch.setattr(run, "parse_stream_json", lambda _: RunResult(
        session_id="session",
        turns=[Turn(index=0, input_tokens=1, output_tokens=2, cache_creation_tokens=0, cache_read_tokens=0)],
        num_turns=1,
        total_cost_usd=0.01,
        duration_ms=1,
        duration_api_ms=1,
        total_input_tokens=1,
        total_output_tokens=2,
        total_cache_creation_tokens=0,
        total_cache_read_tokens=0,
        result_text="correct answer",
    ))

    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = io.StringIO('{"type":"result","subtype":"success","is_error":false}\n')
            self.stderr = io.StringIO("")
            self.returncode = 0

        def wait(self) -> None:
            return None

        def kill(self) -> None:
            self.returncode = -9

    process = FakeProcess()
    captured: dict[str, object] = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return process

    monkeypatch.setattr(run.subprocess, "Popen", fake_popen)

    result = run.run_single(
        "runner_stream_task",
        "runner_stream_mode",
        "haiku",
        0,
        stream_log_path=tmp_path / "stream.jsonl",
    )

    assert captured["stdin"] is run.subprocess.DEVNULL
    assert (tmp_path / "stream.jsonl").read_text() == '{"type":"result","subtype":"success","is_error":false}\n'
    assert result["task"] == "runner_stream_task"
    assert result["model"] == "configured-claude-model"


def test_mcp_armed_claude_cell_without_tilth_tools_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A tilth-armed cell whose session exposes no mcp__tilth__ tools ran
    native-only (exactly what --safe-mode caused) and must fail loudly, not
    produce a silently invalid comparison row."""
    task = _RunnerTask()
    monkeypatch.setitem(run.TASKS, "runner_mcp_task", task)
    monkeypatch.setitem(run.MODELS, "haiku", "configured-claude-model")
    monkeypatch.setitem(run.RUNNERS, "haiku", "claude")
    monkeypatch.setitem(
        run.MODES,
        "runner_mcp_mode",
        ModeConfig(
            name="runner_mcp_mode",
            tools=["Read"],
            mcp_config_path=str(tmp_path / "tilth_mcp.json"),
            description="mcp fail-fast test",
            binary_path="/opt/tilth/bin/tilth",
            tilth_version="9.9.9",
        ),
    )
    monkeypatch.setattr(run, "get_repo_path", lambda _: tmp_path)
    monkeypatch.setattr(run, "parse_stream_json", lambda _: RunResult(
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
        available_tools=["Bash", "Edit", "Glob", "Grep", "Read"],
        mcp_servers=[],
    ))
    (tmp_path / "tilth_mcp.json").write_text(json.dumps({"mcpServers": {"tilth": {"command": "tilth"}}}))
    monkeypatch.setattr(
        run.subprocess,
        "run",
        lambda *args, **kwargs: run.subprocess.CompletedProcess(
            args, 0, stdout=json.dumps({"type": "result", "subtype": "success", "is_error": False}), stderr="",
        ),
    )

    with pytest.raises(run.McpUnavailableError, match="runner_mcp_mode"):
        run.run_single("runner_mcp_task", "runner_mcp_mode", "haiku", 0)


def test_cell_ceiling_rejects_an_expanded_experiment() -> None:
    planned = run.planned_cell_count(
        task_count=2,
        mode_count=3,
        model_count=1,
        repetitions=2,
    )

    assert planned == 12
    with pytest.raises(
        ValueError,
        match=r"planned 12 benchmark cells exceeds --max-cells 11",
    ):
        run.enforce_cell_ceiling(planned, maximum=11)


def test_experiment_scheduler_randomizes_matched_blocks_and_records_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "synthetic"
    source.mkdir()
    results_dir = tmp_path / "results"
    manifest_path = tmp_path / "experiment.json"
    manifest_path.write_text(json.dumps({
        "arm_order_seed": 42,
        "variants": [
            {"name": "no_tilth"},
            {
                "name": "upstream",
                "repository": "https://github.com/jahala/tilth",
                "git_sha": "a" * 40,
            },
            {
                "name": "fork",
                "repository": "https://github.com/example/tilth",
                "git_sha": "b" * 40,
            },
        ],
    }))
    task = _RunnerTask()
    monkeypatch.setitem(run.TASKS, "runner_test_task", task)
    monkeypatch.setitem(run.MODELS, "runner-model", "configured-model")
    monkeypatch.setitem(run.RUNNERS, "runner-model", "claude")
    monkeypatch.setattr(run, "SYNTHETIC_REPO", source)
    monkeypatch.setattr(run, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(run, "reset_repo", lambda: None)
    monkeypatch.setattr(run, "rustc_version", lambda: "rustc test")
    monkeypatch.setattr(
        run,
        "hydrate_mode_metadata",
        lambda mode, compiler: replace(
            mode,
            binary_sha256="c" * 64 if mode.binary_path else None,
            tilth_version="test" if mode.binary_path else None,
            rustc_version=compiler,
        ),
    )
    monkeypatch.setenv("TILTH_BENCH_VARIANT_ROOT", str(tmp_path / "variants"))
    for name, sha in (("upstream", "a" * 40), ("fork", "b" * 40)):
        binary = tmp_path / "variants" / f"{name}-{sha}" / "bin" / "tilth"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\nprintf 'tilth test\\n'\n")
        binary.chmod(0o755)
    calls: list[tuple[str, int, bool]] = []

    def fake_run_single(task_name, mode_name, model_name, repetition, **kwargs):
        calls.append((mode_name, repetition, kwargs["bare"]))
        mode = run.MODES[mode_name]
        return {
            "task": task_name,
            "mode": mode_name,
            "model": run.MODELS[model_name],
            "model_alias": model_name,
            "repetition": repetition,
            "correct": True,
            "correctness_reason": "expected",
            "num_turns": 1,
            "context_tokens": 1,
            "output_tokens": 1,
            "total_cost_usd": 0.0,
            "duration_ms": 1,
            "variant": {
                "label": mode.name,
                "repository": mode.repository,
                "git_sha": mode.git_sha,
                "binary_path": mode.binary_path,
                "binary_sha256": mode.binary_sha256,
                "tilth_version": mode.tilth_version,
                "rustc_version": mode.rustc_version,
            },
        }

    monkeypatch.setattr(run, "run_single", fake_run_single)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run.py",
            "--experiment",
            str(manifest_path),
            "--models",
            "runner-model",
            "--tasks",
            "runner_test_task",
            "--reps",
            "2",
            "--max-cells",
            "6",
        ],
    )

    run.main()

    for repetition in range(2):
        expected = run.randomized_arm_order(
            ["no_tilth", "upstream", "fork"],
            seed=42,
            task="runner_test_task",
            model="runner-model",
            repetition=repetition,
        )
        assert [
            mode
            for mode, rep, _bare in calls
            if rep == repetition
        ] == expected
    assert all(bare for _mode, _rep, bare in calls)

    result_path = next(results_dir.glob("benchmark_*.jsonl"))
    records = [json.loads(line) for line in result_path.read_text().splitlines()]
    for record in records:
        assert record["experiment_manifest"] == str(manifest_path.resolve())
        assert record["arm_order_seed"] == 42
        assert record["arm_order"][record["arm_order_index"]] == record["mode"]


def test_claude_result_requires_success_even_if_grader_would_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setitem(run.TASKS, "claude_failure_task", _RunnerTask())
    events = [
        {"type": "system", "subtype": "init", "tools": []},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "answer"}]}},
        {"type": "result", "subtype": "error_max_budget_usd", "is_error": True, "result": "answer"},
    ]
    monkeypatch.setattr(run.subprocess, "run", lambda cmd, **kwargs: subprocess.CompletedProcess(
        cmd, 0, stdout="\n".join(map(json.dumps, events)), stderr="",
    ))
    with pytest.raises(RuntimeError, match="error_max_budget_usd"):
        run._run_single_in_repo("claude_failure_task", "baseline", "sonnet5", 0, tmp_path)


def test_claude_runtime_options_and_local_tilth_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setitem(run.TASKS, "claude_options_task", _RunnerTask())
    monkeypatch.setattr(run, "TILTH_BIN", "/release/tilth")
    monkeypatch.setitem(run.MODES, "tilth", replace(run.MODES["tilth"], binary_path="/release/tilth"))
    captured = {}
    events = [{"type": "system", "subtype": "init", "tools": ["mcp__tilth__tilth_read"]},
              {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.01}]
    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(map(json.dumps, events)), stderr="")
    monkeypatch.setattr(run.subprocess, "run", fake_run)
    monkeypatch.setattr(run, "_tilth_version", lambda *_args: "test-version")
    result = run._run_single_in_repo("claude_options_task", "tilth", "sonnet5", 0, tmp_path,
                                     bare=True, reasoning_effort="high", max_budget_usd=2.5)
    cmd = captured["cmd"]
    assert cmd[cmd.index("--effort") + 1] == "high"
    assert cmd[cmd.index("--max-budget-usd") + 1] == "2.5"
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert "--disable-slash-commands" in cmd
    assert "Agent,Task" == cmd[cmd.index("--disallowedTools") + 1]
    config = json.loads(cmd[cmd.index("--mcp-config") + 1])
    assert config["mcpServers"]["tilth"]["command"] == "/release/tilth"
    assert result["max_budget_usd"] == 2.5


def test_wozcode_mode_config_uses_explicit_mcp_and_plugin_identity(tmp_path: Path) -> None:
    plugin = tmp_path / "wozcode"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "wozcode", "version": "0.3.92"}))
    (plugin / "servers").mkdir()
    (plugin / "servers" / "code-server.cjs").write_text("// fixture")
    mode = run.wozcode_mode(plugin)
    assert mode.name == "wozcode"
    assert mode.plugin_version == "0.3.92"
    config = run.claude_mcp_config(mode)
    server = config["mcpServers"]["plugin_woz_code"]
    assert server["command"] == "node"
    assert server["args"] == ["--no-warnings=ExperimentalWarning", str(plugin / "servers" / "code-server.cjs")]
    assert server["env"]["WOZCODE_MCP_CWD_HOOK_INJECTED"] == "1"
    assert server["env"]["WOZCODE_BUNDLE_VERSION"] == "0.3.92"
    assert server["env"]["CLAUDE_PLUGIN_ROOT"] == str(plugin)


def test_wozcode_rejects_missing_manifest(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="plugin manifest"):
        run.wozcode_mode(tmp_path)


def test_wozcode_claude_command_requires_its_mcp_tools(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    plugin = tmp_path / "plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "woz", "version": "1.2.3"}))
    (plugin / "servers").mkdir()
    (plugin / "servers" / "code-server.cjs").write_text("// fixture")
    monkeypatch.setitem(run.MODES, "wozcode", run.wozcode_mode(plugin))
    monkeypatch.setitem(run.TASKS, "woz_task", _RunnerTask())
    captured = {}
    tools = ["mcp__plugin_woz_code__Search"]
    events = [{"type": "system", "subtype": "init", "tools": tools},
              {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.01}]
    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(map(json.dumps, events)), stderr="")
    monkeypatch.setattr(run.subprocess, "run", fake_run)
    result = run._run_single_in_repo("woz_task", "wozcode", "sonnet5", 0, tmp_path)
    cmd = captured["cmd"]
    assert cmd[cmd.index("--plugin-dir") + 1] == str(plugin)
    assert cmd[cmd.index("--agent") + 1] == "woz:code"
    assert "--strict-mcp-config" in cmd
    assert json.loads(cmd[cmd.index("--mcp-config") + 1])["mcpServers"].keys() == {"plugin_woz_code"}
    assert result["variant"]["plugin_version"] == "1.2.3"
    tools.clear()
    with pytest.raises(run.McpUnavailableError, match="mcp__plugin_woz_code__"):
        run._run_single_in_repo("woz_task", "wozcode", "sonnet5", 0, tmp_path)


def test_claude_cell_config_copies_only_woz_auth_and_refreshes_seed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    seed = tmp_path / "seed"
    (seed / "wozcode").mkdir(parents=True)
    auth = seed / "wozcode" / "auth.json"
    auth.write_text("old-test-auth")
    (seed / "settings.json").write_text("do-not-copy")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(seed))
    mode = ModeConfig("wozcode", [], "manifest", "test", plugin_dir=str(tmp_path))
    with run._cell_claude_config("sonnet5", mode) as cell:
        assert isinstance(cell, Path)
        assert cell != seed
        assert (cell / "wozcode" / "auth.json").read_text() == "old-test-auth"
        assert sorted(str(path.relative_to(cell)) for path in cell.rglob("*") if path.is_file()) == ["wozcode/auth.json"]
        (cell / "wozcode" / "auth.json").write_text("refreshed-test-auth")
    assert auth.read_text() == "refreshed-test-auth"
    assert auth.stat().st_mode & 0o777 == 0o600


def test_legacy_claude_keeps_existing_oauth_config_without_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    with run._cell_claude_config("sonnet5", run.MODES["baseline"]) as config_dir:
        assert config_dir is None
    mode = ModeConfig("wozcode", [], "manifest", "test", plugin_dir="/plugin")
    with pytest.raises(ValueError, match="explicit CLAUDE_CONFIG_DIR"):
        with run._cell_claude_config("sonnet5", mode):
            pytest.fail("Woz Code started without an isolated auth seed")


def test_legacy_arm_seed_and_local_binary_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    binary = tmp_path / "tilth"
    binary.write_text("#!/bin/sh\nprintf 'tilth test\\n'\n")
    binary.chmod(0o755)
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {"tilth": {"command": "tilth"}}}))
    monkeypatch.setitem(run.MODES, "tilth", replace(run.MODES["tilth"],
                        mcp_config_path=str(config), binary_path=str(binary), binary_sha256=None))
    monkeypatch.setitem(run.TASKS, "seed_task", _RunnerTask())
    monkeypatch.setattr(run, "SYNTHETIC_REPO", source)
    monkeypatch.setattr(run, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(run, "reset_repo", lambda: None)
    seen = []
    def fake_run_single(task_name, mode_name, model_name, repetition, **kwargs):
        seen.append((mode_name, repetition, kwargs["max_budget_usd"]))
        return {"mode": mode_name, "variant": run._variant_metadata(run.MODES[mode_name]),
                "correct": True, "num_turns": 1, "context_tokens": 1,
                "output_tokens": 1, "total_cost_usd": 0.0, "duration_ms": 1}
    monkeypatch.setattr(run, "run_single", fake_run_single)
    monkeypatch.setattr(sys, "argv", ["run.py", "--tasks", "seed_task", "--models", "sonnet5",
                        "--modes", "baseline,tilth", "--reps", "2", "--max-cells", "4",
                        "--arm-order-seed", "42", "--max-budget-usd", "2.5"])
    run.main()
    rows = [json.loads(line) for line in next((tmp_path / "results").glob("benchmark_*.jsonl")).read_text().splitlines()]
    for repetition in range(2):
        expected = run.randomized_arm_order(["baseline", "tilth"], seed=42,
                    task="seed_task", model="sonnet5", repetition=repetition)
        assert [mode for mode, rep, budget in seen if rep == repetition] == expected
    assert all(budget == 2.5 for _, _, budget in seen)
    assert all(row["arm_order_seed"] == 42 and row["max_budget_usd"] == 2.5 for row in rows)
    assert run.MODES["tilth"].binary_sha256 == hashlib.sha256(binary.read_bytes()).hexdigest()
    assert next(row for row in rows if row["mode"] == "tilth")["variant"]["binary_sha256"] == run.MODES["tilth"].binary_sha256


@pytest.mark.parametrize("mode_name,expected_tools", [
    ("baseline", "Read,Edit,Write,Grep,Glob,Bash"),
    ("tilth", "Bash"),
    ("wozcode", "Bash"),
])
def test_strict_file_tools_configures_equal_mcp_restrictions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode_name: str, expected_tools: str,
) -> None:
    monkeypatch.setitem(run.TASKS, "strict_task", _RunnerTask())
    monkeypatch.setitem(run.MODES, "wozcode", ModeConfig("wozcode", ["Read"], "manifest", "test",
                        plugin_dir=str(tmp_path), plugin_version="1.0"))
    monkeypatch.setattr(run, "_tilth_version", lambda *_args: "test")
    available = expected_tools.split(",")
    if mode_name != "baseline":
        available.append("mcp__plugin_woz_code__Search" if mode_name == "wozcode" else "mcp__tilth__tilth_read")
    events = [{"type": "system", "subtype": "init", "tools": available},
              {"type": "result", "subtype": "success", "is_error": False}]
    captured = {}
    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(map(json.dumps, events)), stderr="")
    monkeypatch.setattr(run.subprocess, "run", fake_run)
    result = run._run_single_in_repo("strict_task", mode_name, "sonnet5", 0, tmp_path,
                                     strict_file_tools=True)
    cmd = captured["cmd"]
    assert cmd[cmd.index("--tools") + 1] == expected_tools
    disallowed = cmd[cmd.index("--disallowedTools") + 1].split(",")
    assert {"Agent", "Task"} <= set(disallowed)
    if mode_name != "baseline":
        assert {"Read", "Edit", "Write", "Grep", "Glob"} <= set(disallowed)
    settings = json.loads(cmd[cmd.index("--settings") + 1])
    hook = settings["hooks"]["PreToolUse"][0]
    assert hook["matcher"] == "Bash"
    assert "claude_bash_guard.py" in hook["hooks"][0]["command"]
    assert result["strict_file_tools"] is True


@pytest.mark.parametrize("denied", [False, True])
def test_strict_file_tools_rejects_successful_forbidden_bash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, denied: bool,
) -> None:
    monkeypatch.setitem(run.TASKS, "strict_task", _RunnerTask())
    events = [
        {"type": "system", "subtype": "init", "tools": ["Read", "Edit", "Write", "Grep", "Glob", "Bash"]},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "call-1", "name": "Bash", "input": {"command": "cat context.go"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "call-1", "is_error": denied}]}},
        {"type": "result", "subtype": "success", "is_error": False},
    ]
    monkeypatch.setattr(run.subprocess, "run", lambda cmd, **kwargs: subprocess.CompletedProcess(
        cmd, 0, stdout="\n".join(map(json.dumps, events)), stderr=""))
    if denied:
        result = run._run_single_in_repo("strict_task", "baseline", "sonnet5", 0, tmp_path,
                                         strict_file_tools=True)
        assert result["strict_file_tools"] is True
    else:
        with pytest.raises(RuntimeError, match="forbidden Bash"):
            run._run_single_in_repo("strict_task", "baseline", "sonnet5", 0, tmp_path,
                                    strict_file_tools=True)


def test_strict_mcp_audit_rejects_native_file_tool_use() -> None:
    mode = ModeConfig("tilth", ["Bash"], "mcp.json", "test")
    raw = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "read-1", "name": "Read", "input": {"file_path": "context.go"}},
    ]}})
    with pytest.raises(RuntimeError, match="forbidden native tool"):
        run._audit_strict_claude(raw, mode, ["Bash", "mcp__tilth__tilth_read"])


def test_strict_timeout_row_retains_mode_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setitem(run.TASKS, "strict_timeout_task", _RunnerTask())
    monkeypatch.setattr(run, "SYNTHETIC_REPO", source)
    monkeypatch.setattr(run, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(run, "reset_repo", lambda: None)
    seen = []
    def timeout(*_args, **kwargs):
        seen.append(kwargs["strict_file_tools"])
        raise subprocess.TimeoutExpired(["claude"], 600)
    monkeypatch.setattr(run, "run_single", timeout)
    monkeypatch.setattr(sys, "argv", ["run.py", "--tasks", "strict_timeout_task",
                        "--models", "sonnet5", "--modes", "baseline", "--reps", "1",
                        "--max-cells", "1", "--strict-file-tools"])
    run.main()
    row = json.loads(next((tmp_path / "results").glob("benchmark_*.jsonl")).read_text())
    assert seen == [True]
    assert row["strict_file_tools"] is True
    assert row["error"] == "timeout"
