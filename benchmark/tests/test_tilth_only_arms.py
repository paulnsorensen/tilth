"""Every tilth arm is tilth-only: no hybrid mode, strict by default on Claude, a real Codex arm."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BENCH = Path(__file__).parent.parent
sys.path.insert(0, str(BENCH))

import codex_probe
import config
import run
from conftest import StoreTask
from config import ModeConfig
from test_strict_hook import fake_hook, install_fake_hook, route_hook_to_real_process

PINNED_HOOK = "/pinned/seed/tilth"


@pytest.fixture
def catalog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "catalog-abc123.json"
    monkeypatch.setattr(run, "_codex_catalog_file", lambda: path)
    return path


def _codex_argv(mode_name: str, binary: str = "/opt/tilth/bin/tilth", hook_binary: str | None = PINNED_HOOK) -> list[str]:
    task = SimpleNamespace(prompt="find it", hide_git=False)
    mode = run.replace(run.MODES[mode_name], binary_path=binary)
    return run._codex_command(task, mode, "gpt-5.6-sol", Path("/work/repo"), reasoning_effort=None, skill_paths=[],
                              hook_binary=hook_binary)


def _flag_values(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, part in enumerate(argv[:-1]) if part == flag]


def _config_values(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, part in enumerate(argv[:-1]) if part == "-c"]


def test_the_hybrid_tilth_mode_is_gone() -> None:
    assert "tilth" not in run.MODES
    assert "tilth_forced" in run.MODES
    with pytest.raises(ValueError, match="modes"):
        run.select_modes("tilth", None)


def test_opencode_offers_only_the_baseline_arm() -> None:
    assert set(config.OPENCODE_CONFIGS) == {"baseline"}
    assert not (config.FIXTURES_DIR / "opencode_tilth.json").exists()


def test_codex_tilth_only_argv_removes_patch_tool_and_guards_the_shell(catalog: Path) -> None:
    argv = _codex_argv("tilth_forced")
    values = _config_values(argv)

    assert f"model_catalog_json={json.dumps(str(catalog))}" in values
    hook = next(value for value in values if value.startswith("hooks.PreToolUse="))
    assert 'matcher = "^Bash$"' in hook
    assert f"{PINNED_HOOK} hook --host codex --hint tilth" in hook
    assert "/opt/tilth/bin/tilth hook" not in hook
    assert "--dangerously-bypass-hook-trust" in argv
    assert "hooks" not in _flag_values(argv, "--disable")
    assert "hooks" in _flag_values(argv, "--enable")
    assert f"mcp_servers.tilth.command={json.dumps('/opt/tilth/bin/tilth')}" in argv


def test_codex_tilth_only_argv_without_a_pinned_hook_is_refused(catalog: Path) -> None:
    with pytest.raises(ValueError, match="hook"):
        _codex_argv("tilth_forced", hook_binary=None)


def test_every_experiment_arm_runs_the_one_pinned_hook(catalog: Path) -> None:
    hooks = set()
    for name, binary in ("upstream", "/arms/upstream/tilth"), ("fork", "/arms/fork/tilth"):
        mode = run.replace(run.MODES["tilth_forced"], name=name, binary_path=binary)
        task = SimpleNamespace(prompt="find it", hide_git=False)
        argv = run._codex_command(task, mode, "gpt-5.6-sol", Path("/work/repo"), reasoning_effort=None,
                                  skill_paths=[], hook_binary=PINNED_HOOK)
        hooks.add(next(v for v in _config_values(argv) if v.startswith("hooks.PreToolUse=")))
        assert f"mcp_servers.tilth.command={json.dumps(binary)}" in argv
    assert len(hooks) == 1
    assert PINNED_HOOK in hooks.pop()


def test_codex_baseline_argv_keeps_hooks_off_and_has_no_catalog(catalog: Path) -> None:
    argv = _codex_argv("baseline")

    assert any(argv[i:i + 2] == ["--disable", "hooks"] for i in range(len(argv)))
    assert "--dangerously-bypass-hook-trust" not in argv
    assert not any(value.startswith(("model_catalog_json=", "hooks.")) for value in _config_values(argv))


def test_codex_catalog_content_feeds_the_key_but_its_path_does_not(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def template(stem: str, directory: str) -> list[str]:
        monkeypatch.setattr(run, "_codex_catalog_file", lambda: tmp_path / directory / f"{stem}.json")
        task = SimpleNamespace(prompt="p", hide_git=False)
        return run._command_template(task, run.MODES["tilth_forced"], "tilth_forced", "luna56", bare=False,
                                     reasoning_effort=None, max_budget_usd=1.0, strict_file_tools=False)

    assert template("catalog-aaa", "one") == template("catalog-aaa", "two")
    assert template("catalog-aaa", "one") != template("catalog-bbb", "one")
    assert not any(str(tmp_path) in part for part in template("catalog-aaa", "one"))


def _stream(*items: dict) -> str:
    return "\n".join(json.dumps({"type": "item.completed", "item": item}) for item in items)


@pytest.mark.parametrize("kind", ["file_change", "file_edit", "file_write"])
def test_codex_stream_rejects_native_file_items_on_the_tilth_only_arm(kind: str) -> None:
    raw = _stream({"type": kind, "changes": []})

    with pytest.raises(run.InvalidCodexCellError, match="native file tool"):
        run._validate_codex_stream(raw, False, [], [], tilth_only=True)
    run._validate_codex_stream(raw, False, [], [])


def _prompt_input(monkeypatch: pytest.MonkeyPatch, tools: list[dict]) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps([{"tools": tools}]), stderr="")

    monkeypatch.setattr(run.subprocess, "run", fake_run)
    return calls


def test_preflight_refuses_a_surface_with_apply_patch(monkeypatch: pytest.MonkeyPatch, catalog: Path) -> None:
    _prompt_input(monkeypatch, [{"name": "functions", "tools": [{"name": "exec_command"}, {"name": "apply_patch"}]}])

    with pytest.raises(run.InvalidCodexCellError, match="tool surface"):
        run.check_codex_tool_surface(_codex_argv("tilth_forced"), {}, Path("/work/repo"))


def test_preflight_accepts_the_shell_without_a_patch_tool_and_reuses_the_flags(
    monkeypatch: pytest.MonkeyPatch, catalog: Path
) -> None:
    calls = _prompt_input(monkeypatch, [{"name": "functions", "tools": [{"name": "exec_command"}]}])

    names = run.check_codex_tool_surface(_codex_argv("tilth_forced"), {}, Path("/work/repo"))

    assert "exec_command" in names and "apply_patch" not in names
    argv = calls[0]
    assert argv[:3] == ["codex", "debug", "prompt-input"]
    assert argv[-2:] == ["--", "preflight"]
    assert 'model="gpt-5.6-sol"' in _config_values(argv)
    assert any(value.startswith("model_catalog_json=") for value in _config_values(argv))
    assert "--sandbox" not in argv and "-m" not in argv


@pytest.mark.parametrize("model, requested, expected", [
    ("sonnet5", None, True),
    ("sonnet5", False, False),
    ("sonnet5", True, True),
    ("luna56", None, False),
    ("luna56", False, False),
    ("gpt5mini", None, False),
])
def test_strict_file_tools_default_follows_the_runner(model: str, requested: bool | None, expected: bool) -> None:
    assert run.resolve_strict_file_tools(requested, [model]) is expected


def test_strict_file_tools_is_per_runner_on_mixed_runs() -> None:
    with pytest.raises(ValueError, match="requires a Claude model"):
        run.resolve_strict_file_tools(True, ["luna56"])
    assert run.resolve_strict_file_tools(None, ["sonnet5", "luna56"]) is True
    assert run.resolve_strict_file_tools(True, ["sonnet5", "luna56"]) is True
    assert run.resolve_strict_file_tools(False, ["sonnet5", "luna56"]) is False


def test_help_offers_the_strict_opt_out() -> None:
    out = subprocess.run([sys.executable, str(BENCH / "run.py"), "--help"], capture_output=True, text=True, check=True).stdout

    assert "--no-strict-file-tools" in out


# --- cell identity: the pinned hook, the catalog, and the telemetry join the key and the row ---


@pytest.fixture
def arm_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(run.TASKS, "arm_task", StoreTask())


def _identity(model: str = "luna56", mode: str = "tilth_forced", hook_sha: str | None = "seed") -> dict:
    return run.cell_identity("arm_task", mode, model, 0, bare=False, reasoning_effort=None, max_budget_usd=1.0,
                             strict_file_tools=False, hook_sha=hook_sha)


def test_codex_tilth_only_row_records_the_hook_source_catalog_and_restriction(
    monkeypatch: pytest.MonkeyPatch, catalog: Path, arm_task: None
) -> None:
    monkeypatch.setattr(run, "_hook_source_key", lambda _sha, _repo=None: "tree:lock")

    identity = _identity()

    assert identity["hook_source_key"] == "tree:lock"
    assert identity["tool_restriction"] == "codex-hook+catalog"
    assert identity["codex_catalog_sha256"] == "abc123"
    baseline = _identity(mode="baseline", hook_sha=None)
    assert not {"hook_source_key", "tool_restriction", "codex_catalog_sha256"} & set(baseline)


def test_codex_tilth_only_run_key_follows_the_pinned_hook_source(
    monkeypatch: pytest.MonkeyPatch, catalog: Path, arm_task: None
) -> None:
    source = {"key": "tree-a:lock"}
    monkeypatch.setattr(run, "_hook_source_key", lambda _sha, _repo=None: source["key"])
    before = _identity()["run_key"]
    source["key"] = "tree-b:lock"

    assert _identity()["run_key"] != before


def test_codex_tilth_only_cell_without_a_hook_sha_is_refused(catalog: Path, arm_task: None) -> None:
    with pytest.raises(ValueError, match="hook_sha"):
        _identity(hook_sha=None)


def test_strict_flag_keys_a_claude_cell_only_in_a_mixed_run(
    monkeypatch: pytest.MonkeyPatch, catalog: Path, arm_task: None
) -> None:
    monkeypatch.setattr(run, "_hook_source_key", lambda _sha, _repo=None: "tree:lock")
    kwargs = dict(bare=True, reasoning_effort=None, max_budget_usd=1.0, hook_sha="seed")

    codex_strict = run.cell_identity("arm_task", "baseline", "luna56", 0, strict_file_tools=True, **kwargs)
    codex_plain = run.cell_identity("arm_task", "baseline", "luna56", 0, strict_file_tools=False, **kwargs)
    claude_strict = run.cell_identity("arm_task", "baseline", "sonnet5", 0, strict_file_tools=True, **kwargs)

    assert codex_strict["run_key"] == codex_plain["run_key"]
    assert "hook_source_key" in claude_strict


# --- catalog payload shape ---


@pytest.mark.parametrize("payload", ["not json", "[]", '{"models": []}', '{"models": [1]}', '{"other": 1}'])
def test_codex_catalog_payload_shape_mismatch_names_the_codex_version(
    monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    run._codex_catalog_file.cache_clear()
    monkeypatch.setattr(run.subprocess, "run", lambda cmd, **_k: SimpleNamespace(returncode=0, stdout=payload, stderr=""))
    monkeypatch.setattr(run, "cli_version", lambda name, **_k: "9.9.9")
    try:
        with pytest.raises(RuntimeError, match=r"unexpected payload \(codex 9\.9\.9\)"):
            run._codex_catalog_file()
    finally:
        run._codex_catalog_file.cache_clear()


# --- the stream audit replays every command through the pinned hook ---


def _command(command: str) -> dict:
    return {"type": "command_execution", "command": command, "status": "completed", "aggregated_output": ""}


def test_audit_passes_allowed_commands_and_counts_them(tmp_path: Path) -> None:
    raw = _stream(_command("/usr/bin/zsh -lc ls"), _command("/usr/bin/zsh -lc 'git status'"),
                  {"type": "agent_message", "text": "cat"})

    assert run._audit_codex_commands(raw, str(fake_hook(tmp_path))) == 2


def test_audit_invalidates_a_cell_that_ran_a_denied_file_read(tmp_path: Path) -> None:
    raw = _stream(_command("/usr/bin/zsh -lc ls"), _command("/usr/bin/zsh -lc 'cat src/main.rs'"))

    with pytest.raises(run.CodexHookLoosenedError, match="hook_loosened.*cat src/main.rs"):
        run._audit_codex_commands(raw, str(fake_hook(tmp_path)))
    assert issubclass(run.CodexHookLoosenedError, run.InvalidCodexCellError)


def test_audit_refuses_a_command_item_without_text(tmp_path: Path) -> None:
    with pytest.raises(run.CodexHookLoosenedError, match="no command text"):
        run._audit_codex_commands(_stream({"type": "command_execution"}), str(fake_hook(tmp_path)))


# --- the pre-spend live check ---


def _report(**over: object) -> dict:
    report = {"argv_enables_hooks": True, "argv_disables_hooks": False, "cat_denied": True, "secret_leaked": False,
              "ls_ran": True, "apply_patch_in_request": False}
    return {**report, **over}


def test_probe_problems_is_empty_for_an_enforcing_report() -> None:
    assert codex_probe.problems(_report()) == []


@pytest.mark.parametrize("over, text", [
    ({"cat_denied": False}, "did not deny"),
    ({"secret_leaked": True}, "reached the model"),
    ({"ls_ran": False}, "benign command"),
    ({"apply_patch_in_request": True}, "apply_patch"),
    ({"argv_enables_hooks": False}, "enable hooks"),
    ({"argv_disables_hooks": True}, "enable hooks"),
])
def test_probe_problems_names_each_way_the_hook_fails(over: dict, text: str) -> None:
    assert any(text in problem for problem in codex_probe.problems(_report(**over)))


def test_live_check_fails_the_run_when_the_probe_finds_a_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_probe, "probe", lambda *_a, **_k: _report(cat_denied=False))

    with pytest.raises(run.InvalidCodexCellError, match="live check failed.*did not deny"):
        run.check_codex_hook_live(run.MODES["tilth_forced"], "/pinned/seed/tilth")


def test_preflight_runs_the_live_check_for_every_arm_with_the_pinned_hook(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(run, "check_codex_hook_live", lambda mode, hook: seen.append((mode.name, hook)))
    monkeypatch.setitem(run.MODES, "fork_arm", run.replace(run.MODES["tilth_forced"], name="fork_arm"))

    run.preflight_codex_hooks({"tilth_forced", "fork_arm"}, str(fake_hook(tmp_path)))

    assert seen == [("fork_arm", str(tmp_path / "fake-tilth")), ("tilth_forced", str(tmp_path / "fake-tilth"))]


def test_a_hook_binary_without_a_hook_subcommand_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A clap usage error exits 2, the deny code: the allow probe must see exit 0 and refuse the binary."""
    stub = tmp_path / "no-hook"
    stub.write_text(f"#!{sys.executable}\nimport sys\nsys.stderr.write('error: unrecognized subcommand')\nsys.exit(2)\n")
    stub.chmod(0o755)
    monkeypatch.setattr(run, "check_codex_hook_live", lambda *_a: pytest.fail("the live check ran"))

    with pytest.raises(run.HookProbeFailed, match="expected 0"):
        run.preflight_codex_hooks({"tilth_forced"}, str(stub))


# --- wiring: the cell and the plan reach the preflight, the audit, and the isolated home ---


def _codex_cell_stream(command: str) -> str:
    events = [
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "tilth", "tool": "tilth_read",
            "status": "completed", "result": {"content": []}, "error": None, "arguments": {"paths": ["a.py"]}}},
        {"type": "item.completed", "item": _command(command)},
        {"type": "turn.completed", "usage": {}},
    ]
    return "\n".join(map(json.dumps, events))


@pytest.fixture
def codex_cell(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: Path, arm_task: None) -> dict:
    monkeypatch.setitem(run.TASKS, "arm_task", StoreTask(hide_git=True))
    monkeypatch.setitem(run.MODES, "arm", ModeConfig(
        name="arm", tools=[], mcp_config_path="/tmp/tilth.json", description="test",
        binary_path="/arms/own/tilth", tilth_version="test"))
    install_fake_hook(monkeypatch, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", "/saved/codex")
    state: dict = {"stream": _codex_cell_stream("/usr/bin/zsh -lc ls"), "preflight": [], "cmds": []}

    def fake_run(cmd, **kwargs):
        state["cmds"].append((cmd, kwargs.get("env")))
        return subprocess.CompletedProcess(cmd, 0, stdout=state["stream"], stderr="")

    route_hook_to_real_process(monkeypatch, fake_run)
    monkeypatch.setattr(run, "check_codex_tool_surface",
                        lambda cmd, env, cwd, agent_venv=None: state["preflight"].append((cmd, dict(env))))
    return state


def _run_cell(tmp_path: Path, mode: str, **kwargs) -> dict:
    return run._run_single_in_repo("arm_task", mode, "gpt5", 0, tmp_path, **kwargs)


def test_a_codex_tilth_only_cell_runs_the_preflight_audit_and_isolated_home(codex_cell: dict, tmp_path: Path) -> None:
    row = _run_cell(tmp_path, "arm", hook_sha="seed")

    assert len(codex_cell["preflight"]) == 1
    preflight_cmd, preflight_env = codex_cell["preflight"][0]
    assert preflight_env["CODEX_HOME"] != "/saved/codex"
    hook = next(v for v in _config_values(preflight_cmd) if v.startswith("hooks.PreToolUse="))
    assert str(tmp_path / "fake-tilth") in hook and "/arms/own/tilth hook" not in hook
    assert row["codex_audited_commands"] == 1
    assert row["tool_restriction"] == "codex-hook+catalog"
    assert row["hook_source_key"] == "tree:lock"


def test_a_codex_tilth_only_cell_that_ran_a_denied_command_is_invalid(codex_cell: dict, tmp_path: Path) -> None:
    codex_cell["stream"] = _codex_cell_stream("/usr/bin/zsh -lc 'cat src/main.rs'")

    with pytest.raises(run.CodexHookLoosenedError, match="hook_loosened"):
        _run_cell(tmp_path, "arm", hook_sha="seed")


def test_a_codex_tilth_only_cell_without_a_hook_sha_never_starts(codex_cell: dict, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="hook_sha"):
        _run_cell(tmp_path, "arm")
    assert codex_cell["cmds"] == []


def test_every_codex_cell_runs_under_an_isolated_home_that_keeps_auth(
    monkeypatch: pytest.MonkeyPatch, codex_cell: dict, tmp_path: Path
) -> None:
    saved = tmp_path / "saved"
    saved.mkdir()
    (saved / "auth.json").write_text('{"token": "t"}')
    monkeypatch.setenv("CODEX_HOME", str(saved))
    codex_cell["stream"] = _codex_cell_stream("/usr/bin/zsh -lc ls").replace('"server": "tilth"', '"server": "tilth"')
    seen: list[str] = []

    def probe_env(cmd, **kwargs):
        if kwargs.get("env"):
            home = Path(kwargs["env"]["CODEX_HOME"])
            seen.append(f"{home}|{(home / 'auth.json').read_text()}")
        return subprocess.CompletedProcess(cmd, 0, stdout=codex_cell["stream"], stderr="")

    route_hook_to_real_process(monkeypatch, probe_env)
    monkeypatch.setitem(run.MODES, "baseline_codex", run.replace(run.MODES["baseline"]))
    monkeypatch.setattr(run, "_validate_codex_stream", lambda *_a, **_k: 0)

    _run_cell(tmp_path, "baseline")

    assert seen and seen[0].split("|")[0] != str(saved)
    assert seen[0].endswith('|{"token": "t"}')


def test_run_plan_runs_the_codex_preflight_before_any_cell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: Path, arm_task: None
) -> None:
    monkeypatch.setitem(run.MODES, "arm", ModeConfig(
        name="arm", tools=[], mcp_config_path="/tmp/tilth.json", description="test",
        binary_path="/arms/own/tilth", tilth_version="test"))
    install_fake_hook(monkeypatch, tmp_path)
    monkeypatch.setattr(run, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(run, "cli_version", lambda *_a, **_k: "1.0")
    events: list[str] = []

    def refuse(modes, hook):
        events.append(f"preflight {sorted(modes)} {Path(hook).name}")
        raise run.InvalidCodexCellError("hook not enforcing")

    monkeypatch.setattr(run, "preflight_codex_hooks", refuse)
    monkeypatch.setattr(run, "run_single", lambda *_a, **_k: events.append("cell") or {})

    with pytest.raises(run.InvalidCodexCellError, match="not enforcing"):
        run._run_plan([run.CellSpec("arm_task", "arm", "gpt5", 0)], panel=None, ledger=run.SpendLedger(max_usd=5.0),
                      refreeze_baselines=False, output=None, cell_estimate_usd=1.0, max_budget_usd=1.0,
                      store_only=False, hook_sha="seed")

    assert events == ["preflight ['arm'] fake-tilth"]


# --- the hook denies a bare shell, so write_stdin cannot start an unguarded one ---


@pytest.mark.skipif(not (config.REPO_ROOT / "target" / "debug" / "tilth").is_file(), reason="needs a debug tilth build")
@pytest.mark.parametrize("command", ["bash", "sh", "python3", "/usr/bin/zsh -lc bash", "cat src/main.rs"])
def test_the_codex_hook_denies_bare_shells_and_file_reads(command: str) -> None:
    binary = str(config.REPO_ROOT / "target" / "debug" / "tilth")

    assert run._hook_replay(binary, command, hint="tilth", host="codex") is True
    assert run._hook_replay(binary, "/usr/bin/zsh -lc ls", hint="tilth", host="codex") is False


# --- reporting: a missing experiment mode and an unknown legacy mode stay visible ---


def test_paired_warns_and_lists_the_modes_present_when_no_row_matches(capsys: pytest.CaptureFixture) -> None:
    import paired

    runs = [{"mode": "tilth"}, {"mode": "baseline"}]

    assert paired.warn_if_mode_absent(runs, "tilth_forced") is True
    err = capsys.readouterr().err
    assert "tilth_forced" in err and "baseline, tilth" in err
    assert paired.warn_if_mode_absent([{"mode": "tilth_forced"}], "tilth_forced") is False
    assert capsys.readouterr().err == ""


def test_analyze_renders_a_legacy_mode_with_a_note_and_no_alias() -> None:
    import analyze

    row = {"task": "t", "mode": "tilth", "model": "sonnet5", "repetition": 0, "correct": True,
           "total_cost_usd": 0.1, "num_turns": 1, "repo": "synthetic"}

    report = analyze.generate_report([row, {**row, "mode": "baseline"}])

    assert "unrecognized mode(s) tilth" in report and "no alias" in report
    assert "tilth_forced" not in report
