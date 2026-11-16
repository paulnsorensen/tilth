"""Every tilth arm is tilth-only: no hybrid mode, strict by default on Claude, a real Codex arm."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BENCH = Path(__file__).parent.parent
sys.path.insert(0, str(BENCH))

import config
import run


@pytest.fixture
def catalog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "catalog-abc123.json"
    monkeypatch.setattr(run, "_codex_catalog_file", lambda: path)
    return path


def _codex_argv(mode_name: str) -> list[str]:
    task = SimpleNamespace(prompt="find it", hide_git=False)
    mode = run.replace(run.MODES[mode_name], binary_path="/opt/tilth/bin/tilth")
    return run._codex_command(task, mode, "gpt-5.6-sol", Path("/work/repo"), reasoning_effort=None, skill_paths=[])


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
    assert "/opt/tilth/bin/tilth hook --host codex --hint tilth" in hook
    assert "--dangerously-bypass-hook-trust" in argv
    assert argv[argv.index("--disable") + 1] != "hooks"
    assert not any(argv[i:i + 2] == ["--disable", "hooks"] for i in range(len(argv)))


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


def test_strict_file_tools_refuses_non_claude_and_asks_for_a_choice_on_mixed_runs() -> None:
    with pytest.raises(ValueError, match="requires Claude"):
        run.resolve_strict_file_tools(True, ["luna56"])
    with pytest.raises(ValueError, match="no-strict-file-tools"):
        run.resolve_strict_file_tools(None, ["sonnet5", "luna56"])
    assert run.resolve_strict_file_tools(False, ["sonnet5", "luna56"]) is False


def test_help_offers_the_strict_opt_out() -> None:
    out = subprocess.run([sys.executable, str(BENCH / "run.py"), "--help"], capture_output=True, text=True, check=True).stdout

    assert "--no-strict-file-tools" in out
