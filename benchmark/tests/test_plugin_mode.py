"""Tests for the skill-plugin arm: ModeConfig.plugin_dir / prompt_mode argv wiring, the
skill-availability guard, run keys, the OAuth guard, the --modes-json CLI fit and its caps.

Test names carry the acceptance-criterion id (ac3, ac4, ac5, ac6, ac13, ac16) the landing plan selects with -k.
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import baselines
import run
from config import DEFAULT_MAX_BUDGET_USD, ModeConfig
from parse import parse_stream_json
from spend import SpendLedger
from tasks.base import GroundTruth, Task, TaskSource

FIXTURE_PLUGIN_DIR = str(Path(__file__).parent / "fixtures" / "probe_plugin")
STREAMS = Path(__file__).parent / "fixtures" / "streams"
LOADED_STREAM = (STREAMS / "plugin_probe2_skills_loaded.jsonl").read_text()
STRIPPED_STREAM = (STREAMS / "plugin_probe1_skills_stripped.jsonl").read_text()
STOCK_STREAM = (STREAMS / "claude_native_cost.jsonl").read_text()


class _PluginTask(Task):
    """A local read task over the synthetic repo whose answer always passes."""

    name = "plugin_task"
    prompt = "Answer the task."
    capability = "trace"
    ground_truth = GroundTruth()
    source = TaskSource(
        origin="fixture",
        license="MIT",
        commit_or_tag="test-pin",
        transformation="test-only",
    )

    def check_correctness(self, result_text: str, repo_path: str) -> tuple[bool, str]:
        return True, "expected answer"


def _skill_mode(name: str = "skill_mode", **fields) -> ModeConfig:
    return ModeConfig(
        name=name, tools=["Read"], mcp_config_path=None, description="skill-plugin test arm",
        plugin_dir=FIXTURE_PLUGIN_DIR, plugin_sha256="plugin-tree-sha", **fields,
    )


def _stream_with_init(**init_fields) -> str:
    """The committed probe 2 stream with its init event fields overridden."""
    lines = [json.loads(line) for line in LOADED_STREAM.splitlines() if line.strip()]
    lines[0].update(init_fields)
    return "".join(json.dumps(line) + "\n" for line in lines)


@pytest.fixture(autouse=True)
def oauth_only(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in run._API_BILLING_KEYS:
        monkeypatch.delenv(key, raising=False)


class _Calls:
    def __init__(self) -> None:
        self.cmds: list[list[str]] = []
        self.kwargs: list[dict] = []


def _install(monkeypatch, tmp_path, *, mode: ModeConfig, stdout: str) -> _Calls:
    """Run ``run.run_single`` against a fake claude process that prints ``stdout``."""
    monkeypatch.setitem(run.TASKS, "plugin_task", _PluginTask())
    monkeypatch.setitem(run.MODELS, "haiku", "configured-claude-model")
    monkeypatch.setitem(run.RUNNERS, "haiku", "claude")
    monkeypatch.setitem(run.MODES, mode.name, mode)
    monkeypatch.setattr(run, "get_repo_path", lambda _: tmp_path)
    calls = _Calls()

    def fake_run(cmd, *args, **kwargs):
        calls.cmds.append(cmd)
        calls.kwargs.append(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(run.subprocess, "run", fake_run)
    return calls


def _flag_value(cmd: list[str], flag: str) -> str:
    return cmd[cmd.index(flag) + 1]


# --- manifest and argv wiring ---


def test_expected_plugin_skills_reads_plugin_manifest() -> None:
    assert run._expected_plugin_skills(FIXTURE_PLUGIN_DIR) == ["probe:demo"]


def test_ac3_skill_arm_passes_plugin_dir_and_skill_tool(monkeypatch, tmp_path) -> None:
    calls = _install(monkeypatch, tmp_path, mode=_skill_mode(), stdout=LOADED_STREAM)

    run.run_single("plugin_task", "skill_mode", "haiku", 0)

    cmd = calls.cmds[0]
    assert _flag_value(cmd, "--plugin-dir") == FIXTURE_PLUGIN_DIR
    assert _flag_value(cmd, "--tools") == "Read,Skill"
    assert "--agent" not in cmd
    assert "--mcp-config" not in cmd


def test_ac3_skill_arm_drops_disable_slash_commands_other_arms_keep_it(monkeypatch, tmp_path) -> None:
    """Probe 1: with --disable-slash-commands the plugin loads but init skills stay empty."""
    woz = ModeConfig(
        name="woz_like", tools=["Read"], mcp_config_path=str(Path(tmp_path) / "plugin.json"),
        description="woz-shaped arm", plugin_dir=str(tmp_path),
    )
    stock = ModeConfig(name="stock_like", tools=["Read"], mcp_config_path=None, description="stock arm")
    monkeypatch.setitem(run.MODELS, "haiku", "configured-claude-model")
    monkeypatch.setitem(run.RUNNERS, "haiku", "claude")
    task = _PluginTask()

    def argv(mode: ModeConfig) -> list[str]:
        return run._runner_command(
            task, mode, mode.name, "haiku", Path("<repo>"), bare=False, reasoning_effort=None,
            max_budget_usd=1.0, strict_file_tools=False, skill_paths=[],
        )[0]

    assert "--disable-slash-commands" not in argv(_skill_mode())
    assert "--disable-slash-commands" in argv(stock)
    woz_cmd = argv(woz)
    assert "--disable-slash-commands" in woz_cmd
    assert _flag_value(woz_cmd, "--agent") == "woz:code"


def test_plugin_dir_absent_leaves_argv_unchanged(monkeypatch, tmp_path) -> None:
    mode = ModeConfig(name="no_plugin_mode", tools=["Read"], mcp_config_path=None, description="no plugin")
    calls = _install(monkeypatch, tmp_path, mode=mode, stdout=STOCK_STREAM)

    run.run_single("plugin_task", "no_plugin_mode", "haiku", 0)

    cmd = calls.cmds[0]
    assert "--plugin-dir" not in cmd
    assert "--system-prompt" in cmd
    assert _flag_value(cmd, "--tools") == "Read"


def test_prompt_mode_append_uses_append_system_prompt_only(monkeypatch, tmp_path) -> None:
    mode = ModeConfig(
        name="append_mode", tools=["Read"], mcp_config_path=None, description="append prompt mode",
        prompt_mode="append",
    )
    calls = _install(monkeypatch, tmp_path, mode=mode, stdout=STOCK_STREAM)

    run.run_single("plugin_task", "append_mode", "haiku", 0)

    cmd = calls.cmds[0]
    assert "--system-prompt" not in cmd
    assert cmd.count("--append-system-prompt") == 1
    assert _flag_value(cmd, "--append-system-prompt").startswith("Your current working directory is: ")


# --- the skill-load check (probe fixtures) ---


def test_ac3_loaded_skill_stream_is_accepted() -> None:
    """The positive fixture is probe 2: a live stream of the shipped plugin-arm argv."""
    parsed = parse_stream_json(LOADED_STREAM)

    assert "probe:demo" in parsed.skills
    assert "Skill" in parsed.available_tools
    assert parsed.plugin_errors == []
    run._require_plugin_skills("skill_mode", _skill_mode(), parsed)


def test_ac3_stripped_skill_stream_fails_closed() -> None:
    """The negative fixture is probe 1: the plugin loaded but --disable-slash-commands stripped its skills."""
    parsed = parse_stream_json(STRIPPED_STREAM)

    assert parsed.skills == []
    assert any(plugin["name"] == "probe" for plugin in parsed.plugins)
    with pytest.raises(run.SkillUnavailableError, match="probe:demo"):
        run._require_plugin_skills("skill_mode", _skill_mode(), parsed)


def test_ac3_missing_skill_fails_the_cell_through_run_single(monkeypatch, tmp_path) -> None:
    _install(monkeypatch, tmp_path, mode=_skill_mode(), stdout=STRIPPED_STREAM)

    with pytest.raises(run.SkillUnavailableError, match="probe:demo"):
        run.run_single("plugin_task", "skill_mode", "haiku", 0)


def test_ac3_other_plugin_skill_does_not_satisfy_the_check(monkeypatch, tmp_path) -> None:
    stream = _stream_with_init(skills=["other:demo", "demo"])
    _install(monkeypatch, tmp_path, mode=_skill_mode(), stdout=stream)

    with pytest.raises(run.SkillUnavailableError):
        run.run_single("plugin_task", "skill_mode", "haiku", 0)


def test_ac3_manifest_without_skills_fails_closed(tmp_path) -> None:
    plugin = tmp_path / "plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "bare"}))
    mode = replace(_skill_mode(), plugin_dir=str(plugin))

    with pytest.raises(run.SkillUnavailableError, match="declares none"):
        run._require_plugin_skills("skill_mode", mode, parse_stream_json(LOADED_STREAM))


def test_ac3_init_event_need_not_be_the_first_line() -> None:
    hook = json.dumps({"type": "system", "subtype": "hook_started", "skills": ["decoy:skill"]})
    parsed = parse_stream_json(hook + "\n" + LOADED_STREAM)

    assert "decoy:skill" not in parsed.skills
    assert "probe:demo" in parsed.skills


def test_ac3_init_event_keeps_only_name_and_path_of_plugins() -> None:
    parsed = parse_stream_json(_stream_with_init(
        plugins=[{"name": "probe", "path": "/p", "source": "probe@inline", "extra": 1}, "junk"],
        plugin_errors=[{"plugin": "probe", "error": "bad"}],
        skills=["probe:demo", 7, None],
    ))

    assert parsed.plugins == [{"name": "probe", "path": "/p"}]
    assert parsed.skills == ["probe:demo"]
    assert parsed.plugin_errors == [{"plugin": "probe", "error": "bad"}]


def test_row_records_skills_plugins_and_prompt_mode_fields(monkeypatch, tmp_path) -> None:
    mode = _skill_mode(prompt_mode="append")
    _install(monkeypatch, tmp_path, mode=mode, stdout=LOADED_STREAM)

    result = run.run_single("plugin_task", "skill_mode", "haiku", 0)

    assert "probe:demo" in result["skills"]
    assert [plugin for plugin in result["plugins"] if plugin["name"] == "probe"]
    assert all(set(plugin) == {"name", "path"} for plugin in result["plugins"])
    assert result["plugin_errors"] == []
    assert result["prompt_mode"] == "append"
    assert result["variant"]["plugin_dir"] == FIXTURE_PLUGIN_DIR


def test_stock_row_has_no_plugin_fields(monkeypatch, tmp_path) -> None:
    mode = ModeConfig(name="no_plugin_mode", tools=["Read"], mcp_config_path=None, description="no plugin")
    _install(monkeypatch, tmp_path, mode=mode, stdout=STOCK_STREAM)

    result = run.run_single("plugin_task", "no_plugin_mode", "haiku", 0)

    assert not {"skills", "plugins", "plugin_errors", "prompt_mode"} & set(result)


# --- run keys (D-2, D-3) ---


@pytest.fixture
def keyed(bench, monkeypatch: pytest.MonkeyPatch):
    """Read run keys through ``run.cell_identity`` with the ``bench`` probes stubbed."""

    def identity(mode: ModeConfig, *, strict: bool = False, bare: bool = False) -> dict:
        monkeypatch.setitem(run.MODES, mode.name, mode)
        return run.cell_identity("cell_a", mode.name, "sonnet5", 0, bare=bare, reasoning_effort=None,
                                 max_budget_usd=DEFAULT_MAX_BUDGET_USD, strict_file_tools=strict)

    return identity


def test_ac4_prompt_mode_changes_a_plugin_cells_run_key(keyed) -> None:
    replaced = keyed(_skill_mode(prompt_mode="replace"))
    appended = keyed(_skill_mode(prompt_mode="append"))

    assert replaced["run_key"] != appended["run_key"]
    assert replaced["harness_digest"] != appended["harness_digest"]


_STOCK_PREFIX = [
    "claude", "-p", "--output-format", "stream-json", "--verbose", "--model",
]


def _golden_stock_argv(*, bare: bool, budget: str) -> list[str]:
    """The claude argv of a stock arm on origin/main (1cbebb3), written out by hand."""
    return [
        *_STOCK_PREFIX, run.MODELS["sonnet5"], "--max-budget-usd", budget,
        "--disable-slash-commands", "--disallowedTools", "Agent,Task",
        "--no-session-persistence", "--dangerously-skip-permissions", "--strict-mcp-config",
        "--system-prompt", run.SYSTEM_PROMPT + "\nYour current working directory is: <repo>",
        *(["--setting-sources", ""] if bare else []),
        "--tools", "Read", "--", "<prompt>",
    ]


@pytest.mark.parametrize("bare", [False, True])
def test_ac5_stock_arm_argv_is_byte_identical_to_main(bare) -> None:
    mode = ModeConfig(name="stock_like", tools=["Read"], mcp_config_path=None, description="stock arm")
    task = type("T", (), {"prompt": "<prompt>", "hide_git": False})()

    cmd, _ = run._runner_command(
        task, mode, mode.name, "sonnet5", Path("<repo>"), bare=bare, reasoning_effort=None,
        max_budget_usd=1.0, strict_file_tools=False, skill_paths=[],
    )

    assert cmd == _golden_stock_argv(bare=bare, budget="1.0000")


def test_ac5_stock_arm_run_key_inputs_are_unchanged(keyed) -> None:
    mode = ModeConfig(name="stock_like", tools=["Read"], mcp_config_path=None, description="stock arm")
    identity = keyed(mode)

    golden_digest = baselines.harness_digest(
        system_prompt=run.SYSTEM_PROMPT, tools=["Read"], strict_file_tools=False, bare=False,
        max_budget_usd=DEFAULT_MAX_BUDGET_USD, mcp_shape={},
        command=_golden_stock_argv(bare=False, budget="<max-budget>"),
    )
    assert identity["harness_digest"] == golden_digest
    assert not {"plugin_sha256", "prompt_mode"} & set(identity)
    # No None-valued key field was added: the stock key still hashes these ten fields.
    assert baselines.CELL_KEY_FIELDS == (
        "task", "model", "cli_version", "reasoning_effort", "timeout_s", "mode",
        "repetition", "git_sha", "binary_sha256", "plugin_sha256",
    )
    assert baselines.DIGEST_FIELDS == ("harness_digest", "task_digest", "env_fingerprint")


def test_ac5_explicit_replace_prompt_mode_leaves_a_stock_key_unchanged(keyed) -> None:
    unset = ModeConfig(name="stock_like", tools=["Read"], mcp_config_path=None, description="stock arm")
    explicit = replace(unset, prompt_mode="replace")

    assert keyed(unset)["run_key"] == keyed(explicit)["run_key"]


def test_ac5_appended_prompt_text_changes_a_plugin_cells_run_key(keyed, monkeypatch) -> None:
    before = keyed(_skill_mode(prompt_mode="append"), strict=True)["run_key"]
    monkeypatch.setattr(run, "_STRICT_SYSTEM_GUIDANCE", run._STRICT_SYSTEM_GUIDANCE + " Prefer skills.")

    assert keyed(_skill_mode(prompt_mode="append"), strict=True)["run_key"] != before


def test_ac5_replaced_prompt_text_changes_a_plugin_cells_run_key(keyed, monkeypatch) -> None:
    before = keyed(_skill_mode(prompt_mode="replace"))["run_key"]
    monkeypatch.setattr(run, "SYSTEM_PROMPT", run.SYSTEM_PROMPT + " Use the skill.")

    assert keyed(_skill_mode(prompt_mode="replace"))["run_key"] != before


def test_ac5_plugin_content_and_flags_change_a_plugin_cells_run_key(keyed) -> None:
    base = keyed(_skill_mode())

    assert keyed(replace(_skill_mode(), plugin_sha256="another-tree"))["run_key"] != base["run_key"]
    # The dropped --disable-slash-commands flag is part of the hashed command, unlike a stock arm's.
    stock_twin = replace(_skill_mode(), plugin_dir=None, plugin_sha256=None)
    assert keyed(stock_twin)["harness_digest"] != base["harness_digest"]


# --- OAuth guard, no real --bare, no autoupdater (D-1, D-6, D-8) ---


@pytest.mark.parametrize("bare", [False, True])
def test_ac6_plugin_argv_never_passes_bare_and_uses_empty_setting_sources(monkeypatch, tmp_path, bare) -> None:
    calls = _install(monkeypatch, tmp_path, mode=_skill_mode(), stdout=LOADED_STREAM)

    run.run_single("plugin_task", "skill_mode", "haiku", 0, bare=bare)

    cmd = calls.cmds[0]
    assert "--bare" not in cmd
    assert cmd.count("--setting-sources") == 1
    assert _flag_value(cmd, "--setting-sources") == ""


def test_ac6_plugin_cell_env_pins_the_cli_and_keeps_oauth(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token")
    calls = _install(monkeypatch, tmp_path, mode=_skill_mode(), stdout=LOADED_STREAM)

    run.run_single("plugin_task", "skill_mode", "haiku", 0)

    env = calls.kwargs[0]["env"]
    assert env["DISABLE_AUTOUPDATER"] == "1"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-token"
    assert not set(run._API_BILLING_KEYS) & set(env)


@pytest.mark.parametrize("key", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"])
def test_ac6_plugin_cell_refuses_api_billing_credentials(monkeypatch, tmp_path, key) -> None:
    calls = _install(monkeypatch, tmp_path, mode=_skill_mode(), stdout=LOADED_STREAM)
    monkeypatch.setenv(key, "sk-test")

    with pytest.raises(run.ClaudeAuthError, match=key):
        run.run_single("plugin_task", "skill_mode", "haiku", 0)
    assert calls.cmds == []


@pytest.mark.parametrize("key", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"])
def test_ac6_run_plan_refuses_a_plugin_cell_before_spawning(bench, monkeypatch, key) -> None:
    monkeypatch.setitem(run.MODES, "skills", _skill_mode("skills"))
    bench.runner(lambda _stream: 0.1)
    monkeypatch.setenv(key, "sk-test")

    with pytest.raises(run.ClaudeAuthError):
        run.run_plan([run.CellSpec("cell_a", "skills", "sonnet5", 0)], panel=_Panel(), ledger=SpendLedger(10.0),
                     candidate_sha=None, refreeze_baselines=False)
    assert bench.calls == []


# --- skill-unavailable rows (AC-3) ---


class _Panel:
    """The panel surface ``run.run_plan`` needs, with no stamp."""

    name = "plugin-mode-panel"

    def register(self, tasks) -> None:
        pass

    def stamp(self, task: str) -> dict:
        return {}


def test_ac3_skill_unavailable_writes_a_failed_row_and_stops_the_plan(bench, monkeypatch) -> None:
    monkeypatch.setitem(run.MODES, "skills", _skill_mode("skills"))

    def missing_skill(*_args, **_kwargs):
        raise run.SkillUnavailableError("mode 'skills' expects probe:demo")

    monkeypatch.setattr(run, "run_single", missing_skill)

    with pytest.raises(run.PlanStopped) as stopped:
        run.run_plan([run.CellSpec("cell_a", "skills", "sonnet5", 0)], panel=_Panel(), ledger=SpendLedger(10.0),
                     candidate_sha=None, refreeze_baselines=False)

    assert stopped.value.reason == "skill-unavailable"
    [row] = bench.stored_rows()
    assert row["correct"] is False
    assert row["error"].startswith("skill_unavailable: ")
    assert row["mode"] == "skills"


# --- --modes-json CLI fit (AC-13) ---


def _refusal(capsys, *argv: str) -> str:
    with pytest.raises(SystemExit) as exit_info:
        run.main(list(argv))
    assert exit_info.value.code == 2
    return capsys.readouterr().err


def test_ac13_modes_json_excludes_modes_and_experiment(capsys, tmp_path) -> None:
    modes_json = str(tmp_path / "modes.json")

    assert "not allowed with" in _refusal(capsys, "--modes-json", modes_json, "--modes", "baseline")
    assert "not allowed with" in _refusal(capsys, "--modes-json", modes_json, "--experiment", str(tmp_path / "e.json"))


def test_ac13_modes_json_refuses_panel(capsys, tmp_path) -> None:
    err = _refusal(capsys, "--modes-json", str(tmp_path / "modes.json"), "--panel", str(tmp_path / "panel.json"))

    assert "--modes-json cannot be combined with --panel" in err


def test_ac13_tasks_json_refuses_panel(capsys, tmp_path) -> None:
    err = _refusal(capsys, "--tasks-json", str(tmp_path / "tasks.json"), "--panel", str(tmp_path / "panel.json"))

    assert "--tasks-json cannot be combined with --panel" in err


@pytest.mark.parametrize("tasks", ["rg_search_dispatch", "all,x"])
def test_ac13_tasks_json_refuses_a_tasks_value_other_than_all(capsys, tmp_path, tasks) -> None:
    err = _refusal(capsys, "--tasks-json", str(tmp_path / "tasks.json"), "--tasks", tasks)

    assert "--tasks-json selects its own tasks" in err


def test_ac13_main_without_argv_reads_sys_argv(monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.setattr(sys, "argv", ["run.py", "--modes-json", str(tmp_path / "m.json"),
                                      "--panel", str(tmp_path / "p.json")])
    with pytest.raises(SystemExit) as exit_info:
        run.main()

    assert exit_info.value.code == 2
    assert "--modes-json cannot be combined with --panel" in capsys.readouterr().err


def test_ac13_modes_json_loads_skill_arms_in_file_order(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(run, "MODES", dict(run.MODES))
    path = tmp_path / "modes.json"
    path.write_text(json.dumps([
        {"name": "skills_b", "tools": ["Read"], "description": "b", "plugin_dir": FIXTURE_PLUGIN_DIR},
        {"name": "skills_a", "tools": ["Read"], "description": "a", "plugin_dir": FIXTURE_PLUGIN_DIR,
         "prompt_mode": "append"},
    ]))

    assert run.load_modes_json(path) == ["skills_b", "skills_a"]
    assert run.MODES["skills_a"].prompt_mode == "append"
    assert run.MODES["skills_b"].plugin_sha256 == run._tree_sha256(Path(FIXTURE_PLUGIN_DIR))


def test_ac13_modes_json_refuses_a_plugin_arm_with_an_mcp_server(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(run, "MODES", dict(run.MODES))
    path = tmp_path / "modes.json"
    path.write_text(json.dumps([{"name": "skills_mcp", "tools": ["Read"], "description": "d",
                                 "plugin_dir": FIXTURE_PLUGIN_DIR, "mcp_config_path": "x.json"}]))

    with pytest.raises(ValueError, match="serves no MCP server"):
        run.load_modes_json(path)


# --- per-cell caps stay with the harness (AC-16) ---


def _modes_json_plugin_cell(monkeypatch, tmp_path) -> str:
    monkeypatch.setattr(run, "MODES", dict(run.MODES))
    path = tmp_path / "modes.json"
    path.write_text(json.dumps([{"name": "skills_ac16", "tools": ["Read"], "description": "d",
                                 "plugin_dir": FIXTURE_PLUGIN_DIR}]))
    [name] = run.load_modes_json(path)
    return name


@pytest.mark.parametrize(("ledger", "expected"), [
    (SpendLedger(1.0, reserve=0.7), 0.3),
    (SpendLedger(100.0), DEFAULT_MAX_BUDGET_USD),
    (SpendLedger(None), DEFAULT_MAX_BUDGET_USD),
])
def test_ac16_modes_json_plugin_cell_budget_is_the_default_clamped_to_headroom(bench, monkeypatch, tmp_path,
                                                                              ledger, expected) -> None:
    name = _modes_json_plugin_cell(monkeypatch, tmp_path)
    bench.runner(lambda _stream: 0.1)
    budgets: list[float] = []
    runner = run.run_single

    def recording(*args, **kwargs):
        budgets.append(kwargs["max_budget_usd"])
        return runner(*args, **kwargs)

    monkeypatch.setattr(run, "run_single", recording)
    [row] = run.run_plan([run.CellSpec("cell_a", name, "sonnet5", 0)], panel=_Panel(), ledger=ledger,
                         candidate_sha=None, refreeze_baselines=False, cell_estimate_usd=0.05)

    assert DEFAULT_MAX_BUDGET_USD == 1.0
    assert budgets == [pytest.approx(expected)]
    assert row["applied_max_budget_usd"] == pytest.approx(expected)
    assert row["applied_max_budget_usd"] <= 1.0
    assert row["max_budget_usd"] == DEFAULT_MAX_BUDGET_USD
    assert row["timeout_s"] == run.CELL_TIMEOUT_S == 600


def test_ac16_modes_json_plugin_cell_runs_under_the_default_budget_and_timeout(monkeypatch, tmp_path) -> None:
    name = _modes_json_plugin_cell(monkeypatch, tmp_path)
    calls = _install(monkeypatch, tmp_path, mode=run.MODES[name], stdout=LOADED_STREAM)

    run.run_single("plugin_task", name, "haiku", 0)

    assert _flag_value(calls.cmds[0], "--max-budget-usd") == f"{DEFAULT_MAX_BUDGET_USD:.4f}"
    assert calls.kwargs[0]["timeout"] == 600


@pytest.mark.parametrize("field", ["max_budget_usd", "applied_max_budget_usd", "timeout_s", "timeout", "budget"])
def test_ac16_no_per_arm_field_can_raise_a_cap(monkeypatch, tmp_path, field) -> None:
    monkeypatch.setattr(run, "MODES", dict(run.MODES))
    path = tmp_path / "modes.json"
    path.write_text(json.dumps([{"name": "skills_cap", "tools": ["Read"], "description": "d",
                                 "plugin_dir": FIXTURE_PLUGIN_DIR, field: 99}]))

    with pytest.raises(ValueError, match="unknown mode field"):
        run.load_modes_json(path)
    assert "skills_cap" not in run.MODES
