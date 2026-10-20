"""Strict cells are guarded by the tilth hook and replayed through the hook binary built at ``hook_sha``.

The replay audit runs a real subprocess: a fake hook script (exit 2 on a file read) stands in for the
built binary in every test, and a real ``tilth`` build joins when one exists in this checkout.
"""

import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import run
from config import REPO_ROOT, ModeConfig
from conftest import StoreTask
from evolve_support import DEV, MODEL, git, make_repo, make_world, seed_files
from spend import SpendLedger

_FAKE_HOOK = """#!{python}
import json, re, sys
command = json.load(sys.stdin)["tool_input"]["command"]
sys.exit(2 if re.search(r"\\b(cat|grep|sed|perl)\\b", command) else 0)
"""


def fake_hook(tmp_path: Path) -> Path:
    path = tmp_path / "fake-tilth"
    path.write_text(_FAKE_HOOK.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def install_fake_hook(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Make ``build_candidate`` return the fake hook for any sha and key every hook source as one value."""
    hook = fake_hook(tmp_path)
    monkeypatch.setattr(run, "build_candidate", lambda sha, repo=None: run.CandidateBuild(
        git_sha=sha, binary_path=str(hook), binary_sha256="fake-hook"))
    monkeypatch.setattr(run, "_hook_source_key", lambda _sha, _repo=None: "tree:lock")
    return hook


def route_hook_to_real_process(monkeypatch: pytest.MonkeyPatch, fake_run) -> None:
    """Send ``<binary> hook ...`` replays to a real process and everything else to ``fake_run``."""
    real_run = subprocess.run

    def routed(cmd, **kwargs):
        return real_run(cmd, **kwargs) if len(cmd) > 1 and cmd[1] == "hook" else fake_run(cmd, **kwargs)

    monkeypatch.setattr(run.subprocess, "run", routed)


def _real_tilth() -> Path | None:
    return next((path for path in (REPO_ROOT / "target" / "debug" / "tilth", REPO_ROOT / "target" / "release" / "tilth")
                 if path.is_file()), None)


@pytest.fixture(params=["fake", "real"])
def hook_binary(request, tmp_path: Path) -> str:
    if request.param == "fake":
        return str(fake_hook(tmp_path))
    real = _real_tilth()
    if real is None:
        pytest.skip("no built tilth in target/")
    return str(real)


def _trajectory(*calls: tuple[str, str, bool | None]) -> str:
    """stream-json lines: one Bash call per (id, command, is_error); is_error None means no result row."""
    events = []
    for call_id, command, is_error in calls:
        events.append({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": call_id, "name": "Bash", "input": {"command": command}}]}})
        if is_error is not None:
            events.append({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": call_id, "is_error": is_error}]}})
    return "\n".join(map(json.dumps, events))


_BASELINE_TOOLS = ["Read", "Edit", "Write", "Grep", "Glob", "Bash"]
_ARMS = [("baseline", _BASELINE_TOOLS), ("tilth_forced", ["Bash", "mcp__tilth__tilth_read"])]


# --- AC-11: the seed-hook replay audit ---


@pytest.mark.parametrize("mode_name,tools", _ARMS)
def test_a_ran_call_the_hook_denies_fails_the_cell_as_hook_loosened(
    hook_binary: str, mode_name: str, tools: list[str],
) -> None:
    raw = _trajectory(("c1", "cat src/a.go", False))

    with pytest.raises(run.HookLoosenedError, match="hook_loosened.*cat src/a.go"):
        run._audit_strict_claude(raw, run.MODES[mode_name], tools, hook_binary)


@pytest.mark.parametrize("mode_name,tools", _ARMS)
def test_a_call_that_never_returned_a_result_also_counts_as_run(
    hook_binary: str, mode_name: str, tools: list[str],
) -> None:
    raw = _trajectory(("c1", "cat src/a.go", None))

    with pytest.raises(run.HookLoosenedError):
        run._audit_strict_claude(raw, run.MODES[mode_name], tools, hook_binary)


@pytest.mark.parametrize("mode_name,tools", _ARMS)
def test_denied_and_allowed_calls_pass_and_denials_are_counted(
    hook_binary: str, mode_name: str, tools: list[str],
) -> None:
    raw = _trajectory(("c1", "cat src/a.go", True), ("c2", "go test ./...", False), ("c3", "go vet ./...", True))

    denied = run._audit_strict_claude(raw, run.MODES[mode_name], tools, hook_binary)

    assert denied == 1  # c1 only: c3 failed but the hook allows it


def test_a_missing_hook_binary_fails_the_audit_instead_of_allowing(tmp_path: Path) -> None:
    raw = _trajectory(("c1", "go test ./...", False))

    with pytest.raises(RuntimeError, match="could not run the hook"):
        run._audit_strict_claude(raw, run.MODES["baseline"], _BASELINE_TOOLS, str(tmp_path / "absent"))


def test_a_crashing_hook_fails_the_audit_instead_of_allowing(tmp_path: Path) -> None:
    crash = tmp_path / "crash"
    crash.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(1)\n")
    crash.chmod(0o755)

    with pytest.raises(RuntimeError, match="hook exited 1"):
        run._audit_strict_claude(_trajectory(("c1", "ls", False)), run.MODES["baseline"], _BASELINE_TOOLS, str(crash))


# --- AC-9: the registered hook command ---


def test_strict_settings_register_an_exec_form_blocking_hook() -> None:
    settings = json.loads(run._strict_bash_settings("/bin/h", "native"))

    assert settings == {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{
        "type": "command", "command": "/bin/h", "args": ["hook", "--hint", "native"], "onFailure": "block"}]}]}}


def test_hook_spec_is_the_mode_binary_for_tilth_and_the_hook_binary_for_baseline() -> None:
    tilth = ModeConfig("tilth_forced", [], "mcp.json", "t", binary_path="/mode/tilth")

    assert run._strict_hook_spec(tilth, "/seed/tilth") == ("/mode/tilth", "tilth")
    assert run._strict_hook_spec(run.MODES["baseline"], "/seed/tilth") == ("/seed/tilth", "native")
    with pytest.raises(ValueError, match="hook binary"):
        run._strict_hook_spec(run.MODES["baseline"], None)


def test_no_python_bash_guard_remains_in_the_benchmark_tree() -> None:
    needle = "claude_" + "bash_guard"
    benchmark = REPO_ROOT / "benchmark"
    hits = [str(path.relative_to(REPO_ROOT)) for path in benchmark.rglob("*")
            if path.is_file() and "results" not in path.relative_to(benchmark).parts
            and path.suffix in {".py", ".md", ".json", ".yaml", ".yml", ".txt"}
            and needle in path.read_text(errors="ignore")]

    assert hits == []


# --- AC-10: the hook source key ---


@pytest.fixture
def hook_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    files = {**seed_files(), "src/hook/x.rs": "pub fn deny() {}\n"}
    repo = tmp_path / "hook-repo"
    shas = {"A": make_repo(repo, files)}
    (repo / "README.md").write_text("docs only\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "B", cwd=repo)
    shas["B"] = git("rev-parse", "HEAD", cwd=repo).strip()
    (repo / "src" / "hook" / "x.rs").write_text("pub fn deny() { /* tighter */ }\n")
    git("commit", "-q", "-am", "C", cwd=repo)
    shas["C"] = git("rev-parse", "HEAD", cwd=repo).strip()
    (repo / "Cargo.lock").write_text("# lock bumped\n")
    git("commit", "-q", "-am", "D", cwd=repo)
    shas["D"] = git("rev-parse", "HEAD", cwd=repo).strip()
    monkeypatch.setitem(run.TASKS, "hook_task", StoreTask())
    return {"repo": repo, **shas}


def _key(history: dict, sha: str, mode: str = "baseline") -> str:
    return run.cell_identity("hook_task", mode, "sonnet5", 0, bare=True, reasoning_effort=None, max_budget_usd=1.0,
                             strict_file_tools=True, hook_sha=history[sha], hook_repo=history["repo"])["run_key"]


def test_baseline_key_follows_only_the_hook_tree_and_cargo_lock(hook_history: dict) -> None:
    assert _key(hook_history, "A") == _key(hook_history, "B")
    assert _key(hook_history, "B") != _key(hook_history, "C")
    assert _key(hook_history, "C") != _key(hook_history, "D")
    assert _key(hook_history, "A") != _key(hook_history, "D")


def test_tilth_arm_key_still_follows_its_binary(hook_history: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace
    mode = run.MODES["tilth_forced"]
    monkeypatch.setitem(run.MODES, "tilth_forced", replace(mode, binary_sha256="binary-1"))
    first = _key(hook_history, "A", "tilth_forced")
    monkeypatch.setitem(run.MODES, "tilth_forced", replace(mode, binary_sha256="binary-2"))

    assert _key(hook_history, "A", "tilth_forced") != first
    assert _key(hook_history, "A", "tilth_forced") == _key(hook_history, "B", "tilth_forced")


def test_hook_source_key_names_the_tree_and_lock_hashes(hook_history: dict) -> None:
    key = run._hook_source_key(hook_history["A"], hook_history["repo"])
    tree = git("rev-parse", f"{hook_history['A']}:src/hook", cwd=hook_history["repo"]).strip()

    assert key.startswith(f"{tree}:") and len(key) == len(tree) + 1 + 64


# --- AC-9: run_plan forwards strict mode to every cell ---


def _strict_world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    return make_world(monkeypatch, tmp_path, {**seed_files(), "src/hook/x.rs": "pub fn deny() {}\n"})


def _plan(world, specs, **kwargs) -> list[dict]:
    return run.run_plan(specs, panel=world.panel, ledger=SpendLedger(100.0), candidate_sha=world.seed_sha,
                        refreeze_baselines=False, repo=world.repo, **kwargs)


def test_run_plan_forwards_strict_mode_and_hook_sha_to_every_cell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    world = _strict_world(monkeypatch, tmp_path)
    seen: list[tuple[str, dict]] = []

    def recording(task, mode, model, rep, **kwargs):
        seen.append((mode, kwargs))
        return world.fake_run_single(task, mode, model, rep, **kwargs)

    monkeypatch.setattr(run, "run_single", recording)
    specs = [run.CellSpec(task, mode, MODEL, 0) for task in DEV for mode in ("baseline", "tilth_forced")]
    rows = _plan(world, specs, strict_file_tools=True, hook_sha=world.seed_sha)

    assert len(seen) == len(specs) == 4
    for _mode, kwargs in seen:
        assert kwargs["strict_file_tools"] is True and kwargs["hook_sha"] == world.seed_sha
        assert kwargs["hook_repo"] == world.repo and kwargs["bare"] is True
    source_key = run._hook_source_key(world.seed_sha, world.repo)
    assert {row["hook_source_key"] for row in rows} == {source_key}
    assert all(row["strict_file_tools"] is True for row in rows)


def test_strict_plan_keys_differ_from_the_non_strict_plan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = _strict_world(monkeypatch, tmp_path)
    cell = run.CellSpec(DEV[0], "baseline", MODEL, 0)
    plain = run.planned_identity(cell, max_budget_usd=1.0)
    strict = run.planned_identity(cell, max_budget_usd=1.0, strict_file_tools=True, hook_sha=world.seed_sha,
                                  hook_repo=world.repo)

    assert plain["run_key"] != strict["run_key"]
    assert "hook_source_key" not in plain and strict["strict_file_tools"] is True


def test_non_strict_plan_does_not_pass_strict_arguments(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = _strict_world(monkeypatch, tmp_path)
    seen: list[dict] = []
    monkeypatch.setattr(run, "run_single", lambda *a, **kw: (seen.append(kw), world.fake_run_single(*a, **kw))[1])
    _plan(world, [run.CellSpec(DEV[0], "baseline", MODEL, 0)])

    assert len(seen) == 1 and "strict_file_tools" not in seen[0] and "hook_sha" not in seen[0]


def test_strict_plan_without_hook_sha_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = _strict_world(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="hook_sha"):
        _plan(world, [run.CellSpec(DEV[0], "baseline", MODEL, 0)], strict_file_tools=True)
