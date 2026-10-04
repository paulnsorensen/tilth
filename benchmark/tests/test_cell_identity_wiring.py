"""Run-key wiring through the real identity helpers (bench-result-substrate cure).

Each test changes one harness, task, environment, or MCP input and reads the run
key back through ``run.cell_identity`` (or ``run.env_fingerprint``), never through
hand-built digest dicts, so a helper that stops feeding the key fails here.
"""

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
import run
from conftest import StoreTask


def _key(task: str = "wiring_task", mode: str = "baseline", model: str = "sonnet5", *,
         bare: bool = False, strict: bool = False) -> str:
    return run.cell_identity(task, mode, model, 0, bare=bare, reasoning_effort=None,
                             max_budget_usd=1.0, strict_file_tools=strict)["run_key"]


@pytest.fixture(autouse=True)
def wiring_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(run.TASKS, "wiring_task", StoreTask())


# --- harness digest: the runner command template (finding 7) ---


def test_codex_developer_instructions_feed_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    before = _key(model="luna56")
    monkeypatch.setattr(run, "_CODEX_GUIDANCE", run._CODEX_GUIDANCE + " Prefer grep.")

    assert _key(model="luna56") != before


def test_codex_tilth_instructions_feed_the_tilth_key(monkeypatch: pytest.MonkeyPatch) -> None:
    before = _key(mode="tilth", model="luna56")
    monkeypatch.setattr(run, "_CODEX_TILTH_GUIDANCE", " Use tilth for everything.")

    assert _key(mode="tilth", model="luna56") != before


def test_disallowed_tools_feed_the_strict_key(monkeypatch: pytest.MonkeyPatch) -> None:
    before = _key(mode="tilth", strict=True)
    monkeypatch.setattr(run, "_STRICT_NATIVE_FILE_TOOLS", run._STRICT_NATIVE_FILE_TOOLS - {"LS"})

    assert _key(mode="tilth", strict=True) != before


def test_strict_bash_settings_feed_the_strict_key(monkeypatch: pytest.MonkeyPatch) -> None:
    before = _key(strict=True)
    monkeypatch.setattr(run, "_strict_bash_settings", lambda *_args: json.dumps({"hooks": {}}))

    assert _key(strict=True) != before


def test_bash_guard_source_feeds_only_the_strict_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Same path, new content: only the guard's content hash can move the key."""
    guard = tmp_path / "claude_bash_guard.py"
    guard.write_text(run._BASH_GUARD.read_text())
    monkeypatch.setattr(run, "_BASH_GUARD", guard)
    strict_before, plain_before = _key(strict=True), _key()
    guard.write_text(guard.read_text() + "\n# allow cat\n")

    assert _key(strict=True) != strict_before
    assert _key() == plain_before


def test_strict_key_ignores_interpreter_and_checkout_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    before = _key(strict=True)
    moved = tmp_path / "other-checkout" / "claude_bash_guard.py"
    moved.parent.mkdir()
    moved.write_text(run._BASH_GUARD.read_text())
    monkeypatch.setattr(run, "_BASH_GUARD", moved)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "venv" / "bin" / "python3"))

    assert _key(strict=True) == before
    template = run._command_template(StoreTask(), run.MODES["baseline"], "baseline", "sonnet5", bare=True,
                                     reasoning_effort=None, max_budget_usd=1.0, strict_file_tools=True)
    assert not any(str(tmp_path) in arg for arg in template)


def test_setting_sources_reach_the_hashed_command() -> None:
    bare = run._command_template(StoreTask(), run.MODES["baseline"], "baseline", "sonnet5", bare=True,
                                 reasoning_effort=None, max_budget_usd=1.0, strict_file_tools=False)
    plain = run._command_template(StoreTask(), run.MODES["baseline"], "baseline", "sonnet5", bare=False,
                                  reasoning_effort=None, max_budget_usd=1.0, strict_file_tools=False)

    assert "--setting-sources" in bare and "--setting-sources" not in plain


def test_any_runner_command_change_feeds_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    before = _key()
    original = run._runner_command

    def with_extra_flag(*args, **kwargs):
        cmd, opencode_config = original(*args, **kwargs)
        return [*cmd[:-2], "--setting-sources", "user", *cmd[-2:]], opencode_config

    monkeypatch.setattr(run, "_runner_command", with_extra_flag)

    assert _key() != before


def test_command_template_normalizes_repo_and_binary_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = replace(run.MODES["tilth"], binary_path="/builds/one/tilth", git_sha="a" * 40,
                        binary_sha256="b" * 64)
    monkeypatch.setitem(run.MODES, "tilth", candidate)
    first = {model: _key(mode="tilth", model=model) for model in ("sonnet5", "luna56")}
    monkeypatch.setitem(run.MODES, "tilth", replace(candidate, binary_path="/builds/two/tilth"))

    assert {model: _key(mode="tilth", model=model) for model in ("sonnet5", "luna56")} == first
    template = run._command_template(StoreTask(), candidate, "tilth", "luna56", bare=False,
                                     reasoning_effort=None, max_budget_usd=1.0, strict_file_tools=False)
    assert not any("/builds/" in arg for arg in template)


# --- MCP shape and task digest wiring (finding 13) ---


def test_mcp_server_args_feed_the_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mcp_config = tmp_path / "tilth_mcp.json"
    mcp_config.write_text(json.dumps({"mcpServers": {"tilth": {"command": "tilth", "args": ["--mcp", "--edit"]}}}))
    monkeypatch.setitem(run.MODES, "tilth", replace(run.MODES["tilth"], mcp_config_path=str(mcp_config)))
    before = _key(mode="tilth")
    mcp_config.write_text(json.dumps({"mcpServers": {"tilth": {"command": "tilth", "args": ["--mcp"]}}}))

    assert _key(mode="tilth") != before


def test_task_ground_truth_feeds_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from tasks.base import GroundTruth

    before = _key()
    monkeypatch.setitem(run.TASKS, "wiring_task",
                        StoreTask(ground_truth=GroundTruth(required_strings=["handler_4"])))

    assert _key() != before


# --- environment fingerprint (finding 6) ---


@pytest.fixture
def repos(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    monkeypatch.setattr(config, "REPOS_DIR", tmp_path / "repos")
    monkeypatch.setattr(run, "SYNTHETIC_REPO", tmp_path / "synthetic")
    paths = {name: run.get_repo_path(name) for name in (*config.REPOS, "synthetic")}
    for path in paths.values():
        path.mkdir(parents=True)
    return paths


@pytest.fixture
def toolchains(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    versions = {"rustc": "rustc 1.90", "cargo": "cargo 1.90", "go": "go1.26", "node": "v24",
                "python3": "Python 3.12", "uv": "uv 0.9"}
    monkeypatch.setattr(run, "_probe_version", lambda argv: versions.get(argv[0]))
    return versions


def _fingerprints() -> dict[str, str]:
    return {name: run.env_fingerprint(name) for name in (*config.REPOS, "synthetic")}


@pytest.mark.parametrize(("tool", "affected"), [
    ("rustc", {"ripgrep"}),
    ("cargo", {"ripgrep"}),
    ("go", {"gin"}),
    ("node", {"express"}),
    ("python3", {"fastapi", "synthetic"}),
    ("uv", {"fastapi", "synthetic"}),
])
def test_toolchain_bump_changes_only_its_languages(repos, toolchains, tool: str, affected: set[str]) -> None:
    before = _fingerprints()
    toolchains[tool] += "-bumped"
    after = _fingerprints()

    assert {name for name in before if before[name] != after[name]} == affected


def test_lockfileless_node_repo_hashes_package_manifest_and_installed_tree(repos, toolchains) -> None:
    express = repos["express"]
    (express / "package.json").write_text('{"dependencies": {"debug": "2.6.9"}}')
    first = run.env_fingerprint("express")
    (express / "package.json").write_text('{"dependencies": {"debug": "4.4.0"}}')
    second = run.env_fingerprint("express")
    (express / "node_modules").mkdir()
    (express / "node_modules" / ".package-lock.json").write_text('{"packages": {}}')
    third = run.env_fingerprint("express")
    (express / "node_modules" / ".package-lock.json").write_text('{"packages": {"node_modules/debug": {}}}')

    assert len({first, second, third, run.env_fingerprint("express")}) == 4
