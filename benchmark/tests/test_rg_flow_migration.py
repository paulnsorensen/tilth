import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
# The benchmark.* imports below resolve from the repository root.
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from benchmark.tasks import TASKS
from benchmark.tasks import rg_flow_migration_tasks as module
from benchmark.tasks.rg_flow_migration_tasks import (
    FIXTURE, HELD_OUT, INJECTED_TESTS, PACKAGES, TRUSTED_TESTS, _classify, _grade_commands, _warm_command,
)
from benchmark.tasks.rg_flow_migration_fixtures.preflight import main as preflight

PATCH = Path(module.FIXTURES) / "reference.patch"


def test_flow_migration_task_contract():
    task = TASKS["rg_edit_flow_migration"]
    assert task.repo == "ripgrep"
    assert task.task_type == "edit"
    assert task.capability == "fix"
    assert task.mutations == []
    for needle in ("Flow", "Continue", "Stop", "Sink", "grep-matcher", "grep-printer", "predicate"):
        assert needle in task.prompt, needle


def test_prompt_names_no_tool_or_held_out_test():
    prompt = TASKS["rg_edit_flow_migration"].prompt.lower()
    for word in ("tilth", "flow_heldout", "held-out", "mcp"):
        assert word not in prompt, word


def test_reference_patch_scope():
    text = PATCH.read_text()
    files = [line.split(" b/")[-1] for line in text.splitlines() if line.startswith("diff --git")]
    hunks = sum(1 for line in text.splitlines() if line.startswith("@@"))
    crates = {path.split("/")[1] for path in files}
    assert len(files) >= 10
    assert hunks >= 40
    assert len(crates) >= 2
    assert crates <= module.SOURCE_CRATES


def test_held_out_files_exist_and_target_integration_tests():
    for relative, source in {**INJECTED_TESTS, **TRUSTED_TESTS}.items():
        assert source.is_file(), source
        assert "/tests/" in relative
    assert HELD_OUT.is_dir()


def test_grader_commands_use_bounded_jobs_and_cover_every_crate():
    assert PACKAGES == ["grep-matcher", "grep-regex", "grep-pcre2", "grep-searcher", "grep-printer"]
    flags = ["-p", "grep-matcher", "-p", "grep-regex", "-p", "grep-pcre2", "-p", "grep-searcher", "-p", "grep-printer"]
    assert _warm_command() == ["cargo", "test", "-j4", "--no-run", *flags]
    assert _grade_commands() == [
        ["cargo", "check", "-j4", "--workspace", "--lib", "--bins", "--tests", "--examples"],
        ["cargo", "test", "-j4", *flags],
    ]
    assert module._environment()["CARGO_BUILD_JOBS"] == "4"


def test_classify_limits_changes_to_rust_sources(tmp_path, monkeypatch):
    fixture = tmp_path / "fixture"
    candidate = tmp_path / "candidate"
    for root in (fixture, candidate):
        for relative in ("crates/matcher/src/lib.rs", "crates/matcher/tests/tests.rs", "crates/ignore/src/lib.rs"):
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_text("")
    (candidate / "crates/searcher/src").mkdir(parents=True)
    (candidate / "crates/searcher/src/helper.rs").write_text("")
    (candidate / "crates/searcher/Cargo.toml").write_text("")
    (candidate / "crates/searcher/src/link.rs").symlink_to(candidate / "crates/matcher/src/lib.rs")
    monkeypatch.setattr(module, "FIXTURE", fixture)

    assert _classify(candidate, "crates/matcher/src/lib.rs") == "source"
    assert _classify(candidate, "crates/matcher/tests/tests.rs") == "test"
    assert _classify(candidate, "crates/searcher/src/helper.rs") == "source"
    assert _classify(candidate, "crates/searcher/Cargo.toml") is None
    assert _classify(candidate, "crates/searcher/src/link.rs") is None
    assert _classify(candidate, "crates/ignore/src/lib.rs") is None
    assert _classify(candidate, "crates/matcher/../../etc.rs") is None
    assert _classify(candidate, "Cargo.lock") is None


# The preflight builds the workspace ten times, so it runs only on request: RG_FLOW_PREFLIGHT=1.
@pytest.mark.skipif(
    not FIXTURE.is_dir() or not os.environ.get("RG_FLOW_PREFLIGHT"),
    reason="pinned ripgrep fixture missing or RG_FLOW_PREFLIGHT unset",
)
def test_grader_reference_and_negative_controls():
    preflight()


def _fake_roots(tmp_path, monkeypatch):
    fixture = tmp_path / "fixture"
    candidate = tmp_path / "candidate"
    for root in (fixture, candidate):
        (root / "crates/matcher/src").mkdir(parents=True)
        (root / "crates/matcher/src/lib.rs").write_text("")
    monkeypatch.setattr(module, "FIXTURE", fixture)
    monkeypatch.setattr(module, "_inject_tests", lambda *args, **kwargs: None)
    return candidate


def test_grader_timeout_is_inconclusive_not_a_test_failure(tmp_path, monkeypatch):
    candidate = _fake_roots(tmp_path, monkeypatch)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs.get("timeout")))
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0, stdout=" M crates/matcher/src/lib.rs\n", stderr="")
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    ok, reason = module.RgFlowMigrationTask().check_correctness("", str(candidate))

    assert not ok
    assert reason.startswith("Grader timeout"), reason
    assert "tests failed" not in reason
    cargo_calls = [call for call in calls if call[0][0] == "cargo"]
    assert cargo_calls[0][0] == _warm_command(), "build is warmed before graded commands"
    assert all(timeout >= 900 for _, timeout in cargo_calls)


def test_grading_stops_at_one_total_deadline(tmp_path, monkeypatch):
    candidate = _fake_roots(tmp_path, monkeypatch)
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    cargo_timeouts = []

    def fake_run(command, **kwargs):
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0, stdout=" M crates/matcher/src/lib.rs\n", stderr="")
        cargo_timeouts.append((kwargs["timeout"], module.GRADE_DEADLINE_S - clock[0]))
        clock[0] += 800  # every cargo command passes, slowly
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    ok, reason = module.RgFlowMigrationTask().check_correctness("", str(candidate))

    assert not ok
    assert reason.startswith(module.TIMEOUT_REASON), reason
    assert "deadline" in reason
    assert all(0 < timeout <= remaining for timeout, remaining in cargo_timeouts), cargo_timeouts
    assert len(cargo_timeouts) < 20, "grading stopped before every command ran"


def test_failed_command_is_a_test_failure(tmp_path, monkeypatch):
    candidate = _fake_roots(tmp_path, monkeypatch)

    def fake_run(command, **kwargs):
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0, stdout=" M crates/matcher/src/lib.rs\n", stderr="")
        return subprocess.CompletedProcess(command, 101, stdout="", stderr="error[E0308]")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    ok, reason = module.RgFlowMigrationTask().check_correctness("", str(candidate))

    assert not ok
    assert reason.startswith("Trusted tests failed"), reason
    assert not reason.startswith(module.TIMEOUT_REASON)
