"""The preamble arm: ``--preamble`` gives a tilth arm ``prompts/preamble.md`` as its system prompt.

The text comes from the tree under test, so it reaches the Claude argv and the Codex instructions file, and
it keys the cell.
"""

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import run
from conftest import StoreTask

PREAMBLE_A = "# tilth agent preamble\n\n## Tools\n\nUse tilth_search first. MARKER-A\n"
PREAMBLE_B = "# tilth agent preamble\n\n## Tools\n\nUse tilth_read first. MARKER-B\n"
TASK = type("T", (), {"prompt": "<prompt>", "hide_git": False})()


def _git(*args: str, cwd: Path) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, str, str]:
    """A repo with two commits: the preamble is PREAMBLE_A at the first and PREAMBLE_B at the second."""
    repo = tmp_path / "tilth"
    (repo / "prompts").mkdir(parents=True)
    _git("init", "-q", cwd=repo)
    shas = []
    for text in (PREAMBLE_A, PREAMBLE_B):
        (repo / "prompts" / "preamble.md").write_text(text)
        _git("add", "-A", cwd=repo)
        _git("commit", "-q", "-m", "preamble", cwd=repo)
        shas.append(_git("rev-parse", "HEAD", cwd=repo).strip())
    return repo, shas[0], shas[1]


@pytest.fixture(autouse=True)
def results(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(run, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setitem(run.TASKS, "preamble_task", StoreTask())
    return tmp_path / "results"


def _argv(mode: run.ModeConfig, model: str) -> list[str]:
    cmd, _ = run._runner_command(TASK, mode, mode.name, model, Path("/work/repo"), bare=True, reasoning_effort=None,
                                 max_budget_usd=1.0, strict_file_tools=False, skill_paths=[])
    return cmd


def _flag(cmd: list[str], flag: str) -> str:
    return cmd[cmd.index(flag) + 1]


def _config(cmd: list[str], key: str) -> str:
    return next(arg for arg in cmd if arg.startswith(f"{key}=")).removeprefix(f"{key}=")


def test_read_preamble_reads_the_commit_under_test_or_the_checkout(tree) -> None:
    repo, first, second = tree

    assert run.read_preamble(first, repo=repo) == PREAMBLE_A
    assert run.read_preamble(second, repo=repo) == PREAMBLE_B
    assert run.read_preamble(None, repo=repo) == PREAMBLE_B


def test_claude_preamble_arm_replaces_the_system_prompt_with_the_tree_preamble(tree) -> None:
    repo, first, _second = tree
    mode = run.preamble_mode(run.MODES["tilth_forced"], run.read_preamble(first, repo=repo))

    prompt = _flag(_argv(mode, "sonnet5"), "--system-prompt")

    assert prompt == PREAMBLE_A + "\nYour current working directory is: /work/repo"
    assert run.SYSTEM_PROMPT not in prompt
    assert "--append-system-prompt" not in _argv(mode, "sonnet5")
    stock = _flag(_argv(run.MODES["tilth_forced"], "sonnet5"), "--system-prompt")
    assert stock == run.SYSTEM_PROMPT + "\nYour current working directory is: /work/repo"


def test_codex_preamble_arm_sets_the_model_instructions_file(tree) -> None:
    repo, first, _second = tree
    mode = run.preamble_mode(run.MODES["tilth"], run.read_preamble(first, repo=repo))

    cmd = _argv(mode, "luna56")

    instructions_file = Path(json.loads(_config(cmd, "model_instructions_file")))
    assert instructions_file.read_text() == PREAMBLE_A
    assert instructions_file.is_relative_to(run.RESULTS_DIR)
    developer = json.loads(_config(cmd, "developer_instructions"))
    assert developer.startswith("Your current working directory is: /work/repo\n")
    assert run.SYSTEM_PROMPT not in developer
    assert cmd.index("--") > cmd.index(f"model_instructions_file={json.dumps(str(instructions_file))}")
    stock = _argv(run.MODES["tilth"], "luna56")
    assert not any(arg.startswith("model_instructions_file=") for arg in stock)
    assert json.loads(_config(stock, "developer_instructions")) == run.SYSTEM_PROMPT + "\n" + developer


def _identity(monkeypatch: pytest.MonkeyPatch, mode_name: str, model: str, text: str | None) -> dict:
    base = run.MODES[mode_name]
    monkeypatch.setitem(run.MODES, mode_name, base if text is None else run.preamble_mode(base, text))
    try:
        return run.cell_identity("preamble_task", mode_name, model, 0, bare=True, reasoning_effort=None,
                                 max_budget_usd=1.0, strict_file_tools=False)
    finally:
        monkeypatch.setitem(run.MODES, mode_name, base)


@pytest.mark.parametrize(("mode_name", "model"), [("tilth_forced", "sonnet5"), ("tilth", "luna56")])
def test_the_preamble_text_keys_the_cell(monkeypatch: pytest.MonkeyPatch, mode_name: str, model: str) -> None:
    stock = _identity(monkeypatch, mode_name, model, None)
    first = _identity(monkeypatch, mode_name, model, PREAMBLE_A)
    again = _identity(monkeypatch, mode_name, model, PREAMBLE_A)
    second = _identity(monkeypatch, mode_name, model, PREAMBLE_B)

    assert len({stock["run_key"], first["run_key"], second["run_key"]}) == 3
    assert first["run_key"] == again["run_key"]
    assert "preamble_sha256" not in stock
    assert first["preamble_sha256"] != second["preamble_sha256"]


def test_the_key_ignores_where_the_codex_instructions_file_lives(monkeypatch: pytest.MonkeyPatch,
                                                                tmp_path: Path) -> None:
    first = _identity(monkeypatch, "tilth", "luna56", PREAMBLE_A)
    monkeypatch.setattr(run, "RESULTS_DIR", tmp_path / "elsewhere")

    assert _identity(monkeypatch, "tilth", "luna56", PREAMBLE_A)["run_key"] == first["run_key"]


def test_a_preamble_arm_without_text_is_unbuildable() -> None:
    mode = replace(run.MODES["tilth_forced"], prompt_mode="preamble")

    with pytest.raises(ValueError, match="no preamble text"):
        _argv(mode, "sonnet5")


def test_the_cli_flag_reads_the_checkout_and_patches_only_tilth_arms(monkeypatch: pytest.MonkeyPatch, tree) -> None:
    repo, first, _second = tree
    monkeypatch.setattr(run, "REPO_ROOT", repo)
    for name in ("baseline", "tilth", "tilth_forced"):
        monkeypatch.setitem(run.MODES, name, run.MODES[name])

    run._apply_preamble(["baseline", "tilth"], ["sonnet5"], None, None)

    assert run.MODES["tilth"].preamble == PREAMBLE_B
    assert run.MODES["baseline"].prompt_mode == "replace"
    run._apply_preamble(["tilth_forced"], ["sonnet5"], None, first)
    assert run.MODES["tilth_forced"].preamble == PREAMBLE_A


@pytest.mark.parametrize(("modes", "models", "message"), [
    (["tilth"], ["gpt5mini"], "Claude and Codex"),
    (["baseline"], ["sonnet5"], "no selected arm serves tilth"),
])
def test_the_cli_flag_refuses_unsupported_runs(modes, models, message) -> None:
    with pytest.raises(ValueError, match=message):
        run._apply_preamble(modes, models, None, None)
