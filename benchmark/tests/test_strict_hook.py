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
            result = {"type": "tool_result", "tool_use_id": call_id, "is_error": is_error}
            if is_error:
                result["content"] = "PreToolUse:Bash hook error: [/usr/bin/fake-hook hook]: blocked\n"
            events.append({"type": "user", "message": {"content": [result]}})
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


# --- press round 1: attacks on the replay audit and the hook source key ---


def _stream(*events: dict) -> str:
    return "\n".join(map(json.dumps, events))


def _use(call_id: str | None, command: object, **extra) -> dict:
    block = {"type": "tool_use", "name": "Bash", "input": {"command": command}}
    if call_id is not None:
        block["id"] = call_id
    return {"type": "assistant", "message": {"content": [block]}, **extra}


def _result(call_id: str, text: str, is_error: bool = True) -> dict:
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": call_id, "is_error": is_error, "content": text}]}}


def test_a_denied_command_that_ran_and_failed_on_its_own_is_not_counted_as_a_hook_denial(tmp_path: Path) -> None:
    # `cat` is denied by the hook. An is_error result whose text is a plain shell failure shows the
    # command RAN (the cell's hook let it through); only is_error is checked, so the audit fails open.
    raw = _stream(_use("c1", "cat missing.go"), _result("c1", "cat: missing.go: No such file or directory"))

    with pytest.raises(run.HookLoosenedError):
        run._audit_strict_claude(raw, run.MODES["baseline"], _BASELINE_TOOLS, str(fake_hook(tmp_path)))


def test_bash_calls_without_an_id_are_each_audited(tmp_path: Path) -> None:
    # Calls keyed by id: two id-less calls collapse to one and the denied `cat` is overwritten.
    raw = _stream(_use(None, "cat a.go"), _use(None, "go test ./..."))

    with pytest.raises(run.HookLoosenedError):
        run._audit_strict_claude(raw, run.MODES["baseline"], _BASELINE_TOOLS, str(fake_hook(tmp_path)))


def test_a_denied_call_among_parallel_calls_in_one_message_is_audited(tmp_path: Path) -> None:
    blocks = [{"type": "tool_use", "id": f"p{i}", "name": "Bash", "input": {"command": command}}
              for i, command in enumerate(["go build ./...", "cat a.go", "go vet ./..."])]
    raw = _stream({"type": "assistant", "message": {"content": blocks}})

    with pytest.raises(run.HookLoosenedError, match="cat a.go"):
        run._audit_strict_claude(raw, run.MODES["baseline"], _BASELINE_TOOLS, str(fake_hook(tmp_path)))


def test_a_denied_call_inside_a_sidechain_is_audited(tmp_path: Path) -> None:
    raw = _stream(_use("s1", "cat a.go", parent_tool_use_id="toolu_parent", isSidechain=True))

    with pytest.raises(run.HookLoosenedError):
        run._audit_strict_claude(raw, run.MODES["baseline"], _BASELINE_TOOLS, str(fake_hook(tmp_path)))


def test_a_newline_smuggled_denied_command_and_unicode_reach_the_hook_intact(tmp_path: Path) -> None:
    hook = fake_hook(tmp_path)

    with pytest.raises(run.HookLoosenedError):
        run._audit_strict_claude(_stream(_use("n1", "go test ./...\ncat a.go")), run.MODES["baseline"],
                                 _BASELINE_TOOLS, str(hook))
    assert run._audit_strict_claude(_stream(_use("u1", "go test ./... # héllo ☃")), run.MODES["baseline"],
                                    _BASELINE_TOOLS, str(hook)) == 0


def test_a_hook_binary_path_with_spaces_is_replayed(tmp_path: Path) -> None:
    spaced = tmp_path / "dir with spaces"
    spaced.mkdir()
    hook = fake_hook(spaced)

    assert run._hook_denies(str(hook), "cat a.go") is True
    assert run._hook_denies(str(hook), "go test ./...") is False


def test_the_replay_sends_the_exact_live_event_shape(tmp_path: Path) -> None:
    seen = tmp_path / "seen.json"
    spy = tmp_path / "spy"
    spy.write_text(f"#!{sys.executable}\nimport sys\nopen({str(seen)!r}, 'w').write(sys.stdin.read())\n"
                   f"open({str(seen) + '.argv'!r}, 'w').write(' '.join(sys.argv[1:]))\n")
    spy.chmod(0o755)

    run._hook_denies(str(spy), "go test ./...")

    assert json.loads(seen.read_text()) == {"tool_name": "Bash", "tool_input": {"command": "go test ./..."}}
    assert Path(str(seen) + ".argv").read_text() == "hook --hint native"


def test_a_non_string_command_does_not_pass_the_audit_silently(tmp_path: Path) -> None:
    raw = _stream(_use("x1", ["cat", "a.go"]))

    with pytest.raises((run.HookLoosenedError, RuntimeError)):
        run._audit_strict_claude(raw, run.MODES["baseline"], _BASELINE_TOOLS, str(fake_hook(tmp_path)))


def test_hook_source_key_is_the_same_for_a_branch_name_a_full_sha_and_a_clone(
    hook_history: dict, tmp_path: Path,
) -> None:
    repo = hook_history["repo"]
    git("branch", "tip", hook_history["C"], cwd=repo)
    clone = tmp_path / "elsewhere" / "clone"
    clone.parent.mkdir()
    git("clone", "-q", str(repo), str(clone), cwd=tmp_path)
    by_sha = run._hook_source_key(hook_history["C"], repo)

    assert run._hook_source_key("tip", repo) == by_sha
    assert run._hook_source_key(hook_history["C"], clone) == by_sha


def test_a_dirty_working_tree_does_not_change_the_hook_source_key(hook_history: dict) -> None:
    repo = hook_history["repo"]
    before = run._hook_source_key(hook_history["D"], repo)
    (repo / "src" / "hook" / "x.rs").write_text("pub fn deny() { /* uncommitted */ }\n")
    (repo / "Cargo.lock").write_text("# uncommitted\n")

    assert run._hook_source_key(hook_history["D"], repo) == before


def test_a_hook_sha_without_src_hook_fails_loudly_instead_of_keying(tmp_path: Path) -> None:
    repo = tmp_path / "old-seed"
    sha = make_repo(repo, seed_files())

    with pytest.raises(Exception) as caught:
        run._hook_source_key(sha, repo)
    assert "src/hook" in str(caught.value) or "src/hook" in str(getattr(caught.value, "stderr", b""))


def test_a_hook_sha_that_looks_like_an_option_is_refused(hook_history: dict) -> None:
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        run._hook_source_key("--all", hook_history["repo"])


# --- press round 2: attacks on the denial-text classifier, with shapes recorded in real Oct-20 strict runs ---
#
# Real shapes (benchmark/results/strict_go_f4db1e6_sonnet_streams, 182 Bash results, all string content):
#   hook denial  is_error=True  "PreToolUse:Bash hook error: [<python> <guard>]: <reason>\n"   (83 of 83 start with the marker)
#   cmd failure  is_error=True  "Exit code N\n<combined output>"   (115 - 83 = 32; none empty, none bare of output)
# A silent non-zero exit is "Exit code N", never empty text.

_REAL_DENIAL = ("PreToolUse:Bash hook error: [/usr/bin/python3 /x/guard.py]: "
                "Only go test, go build, go vet, and gofmt -w on local Go files are allowed.\n")


def _audit(raw: str, tmp_path: Path) -> int:
    return run._audit_strict_claude(raw, run.MODES["baseline"], _BASELINE_TOOLS, str(fake_hook(tmp_path)))


def test_the_recorded_real_denial_text_counts_as_a_denial(tmp_path: Path) -> None:
    assert _audit(_stream(_use("c1", "cat a.go"), _result("c1", _REAL_DENIAL)), tmp_path) == 1


def test_a_silent_exit_one_from_a_denied_class_command_is_hook_loosened(tmp_path: Path) -> None:
    # `grep -q x f` exiting 1 with no output reaches the stream as "Exit code 1", not as empty text.
    raw = _stream(_use("c1", "grep -q x f"), _result("c1", "Exit code 1"))

    with pytest.raises(run.HookLoosenedError):
        _audit(raw, tmp_path)


def test_a_denied_command_whose_failed_output_quotes_the_marker_is_hook_loosened(tmp_path: Path) -> None:
    # `cat notes.md; false` ran, printed a file that quotes the marker, then exited 1. Real failures
    # start with "Exit code N"; real denials start with the marker. A substring match misreads this as a denial.
    text = "Exit code 1\nnotes: PreToolUse:Bash hook error: blocked earlier\n"
    raw = _stream(_use("c1", "cat notes.md; false"), _result("c1", text))

    with pytest.raises(run.HookLoosenedError):
        _audit(raw, tmp_path)


def test_a_denied_command_that_succeeded_printing_the_marker_is_hook_loosened(tmp_path: Path) -> None:
    raw = _stream(_use("c1", "cat notes.md"), _result("c1", "PreToolUse:Bash hook error: in the notes", is_error=False))

    with pytest.raises(run.HookLoosenedError):
        _audit(raw, tmp_path)


def test_a_denial_delivered_as_a_list_of_text_blocks_counts(tmp_path: Path) -> None:
    blocks = [{"type": "text", "text": "PreToolUse:Bash hook error: "}, {"type": "text", "text": "blocked"}]
    raw = _stream(_use("c1", "cat a.go"), {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "c1", "is_error": True, "content": blocks}]}})

    assert _audit(raw, tmp_path) == 1


def test_a_failure_delivered_as_a_list_of_text_blocks_is_hook_loosened(tmp_path: Path) -> None:
    blocks = [{"type": "text", "text": "Exit code 1\ncat: missing.go: No such file or directory"}]
    raw = _stream(_use("c1", "cat missing.go"), {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "c1", "is_error": True, "content": blocks}]}})

    with pytest.raises(run.HookLoosenedError):
        _audit(raw, tmp_path)


def test_a_list_with_non_dict_parts_does_not_crash_the_classifier(tmp_path: Path) -> None:
    raw = _stream(_use("c1", "cat a.go"), {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "c1", "is_error": True,
         "content": ["stray", {"type": "text", "text": _REAL_DENIAL}]}]}})

    assert _audit(raw, tmp_path) == 1


def test_interleaved_id_and_id_less_calls_audit_each_by_its_own_key(tmp_path: Path) -> None:
    denied = _result("c1", _REAL_DENIAL)
    allowed_only = _stream(_use("c1", "cat a.go"), denied, _use(None, "go test ./..."), _use("c2", "go vet ./..."))

    assert _audit(allowed_only, tmp_path) == 1


def test_an_id_less_denied_call_beside_a_valid_denial_is_hook_loosened(tmp_path: Path) -> None:
    raw = _stream(_use("c1", "cat a.go"), _result("c1", _REAL_DENIAL), _use(None, "cat b.go"))

    with pytest.raises(run.HookLoosenedError, match="cat b.go"):
        _audit(raw, tmp_path)


def test_a_result_cannot_be_paired_to_a_different_call_by_position(tmp_path: Path) -> None:
    # c1 (denied class) has no result; the only result belongs to c2. c2's denial must not cover c1.
    raw = _stream(_use("c1", "cat a.go"), _use("c2", "cat b.go"), _result("c2", _REAL_DENIAL))

    with pytest.raises(run.HookLoosenedError, match="cat a.go"):
        _audit(raw, tmp_path)


def test_empty_error_text_is_not_a_denial_and_is_hook_loosened(tmp_path: Path) -> None:
    # No real run produced empty error text; with nothing to show the hook fired, the call ran (fail closed).
    with pytest.raises(run.HookLoosenedError):
        _audit(_stream(_use("c1", "cat a.go"), _result("c1", "")), tmp_path)
