"""A candidate whose change hangs a test fails `just check`, and the proposer prompts keep seed test signatures.

The hang tests run a real subprocess: a fake ``just`` (a small Python script on PATH) prints libtest's hang warning
and then sleeps past a short timeout. No test calls claude, codex, or cargo.
"""

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import run
from evolve import candidate as candidates
from evolve import cli, failures, rust
from evolve.candidate import content_id
from evolve.integrity import InfrastructureFailure
from evolve.loop import TAIL_LINES
from evolve.proposer import PROPOSER_HEADER, REPAIR_NOTE
from evolve_support import build, log_text, seed_candidate
from test_evolve import patch_for, world  # noqa: F401  (world is a fixture)
from test_evolve_repair import Harness, fixed_patch, repair_records

SRC = candidates.SRC_PATCH
HUNG = "mcp::tools::write::tests::rewrite_refused_for_unseen_matches_prints_them_and_retry_applies"
TIMEOUT_S = 3
# A stand-in for `just check`: its worktree's src/lib.rs says whether it hangs with libtest's warning, hangs
# silently, or passes. The noise lines make the output longer than the failure tail.
FAKE_GATE = f"""\
#!/usr/bin/env python3
import sys, time
from pathlib import Path

lib = Path("src/lib.rs").read_text() if Path("src/lib.rs").exists() else ""
if "HUNG" in lib or "SILENT" in lib:
    for n in range(150):
        sys.stderr.write(f"   Compiling dep{{n}} v0.{{n}}.0\\n")
    sys.stderr.flush()
    if "HUNG" in lib:
        print("running 3 tests")
        print("test edit::fast ... ok")
        print("test {HUNG} has been running for over 60 seconds")
        print("test {HUNG} has been running for over 60 seconds", flush=True)
    time.sleep(60)
print("just check: ok")
"""


def fake_gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Put the fake gate on PATH as both ``just`` and ``cargo``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("just", "cargo"):
        (bin_dir / name).write_text(FAKE_GATE)
        (bin_dir / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def gate_worktree(tmp_path: Path, marker: str) -> Path:
    worktree = tmp_path / "wt"
    (worktree / "src").mkdir(parents=True)
    (worktree / "src" / "lib.rs").write_text(f"pub fn lib_marker() -> u8 {{\n    {marker}\n}}\n")
    return worktree


# --- libtest's hang warning ---


def test_hanging_tests_reads_each_hang_warning_in_order_without_repeats() -> None:
    output = ("running 4 tests\n"
              "test a::slow has been running for over 60 seconds\n"
              "test edit::fast ... ok\n"
              "test b::stuck has been running for over 120 seconds\n"
              "test a::slow has been running for over 60 seconds\n"
              # One test thread: the warning follows the `test NAME ... ` that began the line.
              "test c::alone ... test c::alone has been running for over 60 seconds\n")

    assert rust.hanging_tests(output) == ["a::slow", "b::stuck", "c::alone"]


def test_hanging_tests_is_empty_without_a_hang_warning() -> None:
    assert rust.hanging_tests("") == []
    assert rust.hanging_tests("test a::b ... ok\ntest c::d ... FAILED\nthe contest x has been running for over 60 seconds\n"
                              "test e::f has been running for a while\n") == []


# --- the real subprocess: a hang is a verdict, a silent timeout is not ---


def test_a_just_check_timeout_that_names_a_hung_test_is_a_verdict(monkeypatch: pytest.MonkeyPatch,
                                                                  tmp_path: Path) -> None:
    monkeypatch.setattr(run, "RESULTS_DIR", tmp_path / "results")
    fake_gate(monkeypatch, tmp_path)
    worktree = gate_worktree(tmp_path, "HUNG")

    started = time.monotonic()
    ok, output = cli.default_just_check(worktree, timeout_s=TIMEOUT_S)

    assert time.monotonic() - started < 30
    assert ok is False
    # The partial output survives the kill.
    assert "test edit::fast ... ok" in output and "Compiling dep149 v0.149.0" in output
    assert f"test {HUNG} has been running for over 60 seconds" in output
    last = output.splitlines()[-1]
    assert last == (f"error: just check timed out after {TIMEOUT_S} s; these tests hung: {HUNG}. "
                    "The change likely deadlocks or loops forever.")
    # The hung test is not a failing test, so it grants no test edit.
    assert rust.failing_tests(output) == []


def test_a_just_check_timeout_without_a_hang_warning_is_still_infrastructure(monkeypatch: pytest.MonkeyPatch,
                                                                             tmp_path: Path) -> None:
    monkeypatch.setattr(run, "RESULTS_DIR", tmp_path / "results")
    fake_gate(monkeypatch, tmp_path)
    worktree = gate_worktree(tmp_path, "SILENT")

    with pytest.raises(InfrastructureFailure, match=f"just check timed out after {TIMEOUT_S} s"):
        cli.default_just_check(worktree, timeout_s=TIMEOUT_S)


def test_the_integrity_cargo_timeout_stays_infrastructure_even_with_a_hang_warning(monkeypatch: pytest.MonkeyPatch,
                                                                                   tmp_path: Path) -> None:
    monkeypatch.setattr(run, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(cli, "JUST_CHECK_TIMEOUT_S", TIMEOUT_S)
    fake_gate(monkeypatch, tmp_path)
    worktree = gate_worktree(tmp_path, "HUNG")

    with pytest.raises(InfrastructureFailure, match="timed out"):
        cli.default_cargo(worktree, ["test"])


def test_the_just_check_timeout_defaults_to_1800_seconds() -> None:
    assert cli.JUST_CHECK_TIMEOUT_S == 1800


# --- the evolution: the hang verdict is memoized and repaired ---


def test_a_hung_candidate_fails_just_check_once_and_its_repair_runs(world, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
                                                                    tmp_path: Path) -> None:
    hung, fixed = patch_for(world, {"src/lib.rs": "pub fn lib_marker() -> u8 {\n    HUNG\n}\n"}), fixed_patch(world)
    harness = Harness(world, patches=[hung, fixed])
    fake_gate(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "JUST_CHECK_TIMEOUT_S", TIMEOUT_S)
    checked: list[Path] = []

    def just_check(worktree: Path) -> tuple[bool, str]:
        checked.append(worktree)
        return cli.default_just_check(worktree)

    harness.evo.just_check = just_check
    hung_child = {**seed_candidate(), SRC: hung}

    proposal = harness.dispatch()

    # precheck returned the verdict, so the proposer was called again with the hang in its repair record.
    assert proposal == {SRC: fixed}
    [record] = repair_records(harness)
    assert record["repair"]["stage"] == "just check"
    error = record["repair"]["error"]
    assert error.splitlines()[-1].endswith(f"these tests hung: {HUNG}. The change likely deadlocks or loops forever.")
    assert len(error.splitlines()) <= TAIL_LINES
    assert "may_update_tests" not in record["repair"] and "failing_tests" not in record["repair"]
    result = harness.evo.results[content_id(hung_child)]
    assert result.stage == "just check" and result.failing_tests == []
    assert len(checked) == 2  # the hung child and its repair, each once

    # The verdict is memoized: evaluating the hung child again runs no `just check`.
    for task in ("dev_a", "dev_b"):
        score, info = harness.evo.evaluate(hung_child, task)
        assert score == 0.0 and info["stage"] == "just check"
    stage, tail = harness.evo.precheck(hung_child)
    assert stage == "just check" and f"these tests hung: {HUNG}" in tail
    assert len(checked) == 2
    assert log_text(world).count(failures.header(content_id(hung_child), "just check")) == 1
    # The failed-patch record names the hang, not the first line of the output.
    [entry] = [entry for entry in harness.evo.failed.summary() if entry["id"] == content_id(hung_child)[:12]]
    assert entry["stage"] == "just check" and entry["error"].startswith("error: just check timed out after")
    assert HUNG in entry["error"]
    # A resumed run reads the verdict back from log.txt, so it does not check the hung patch again.
    assert build(world).failed.get(content_id(hung_child))[0] == "just check"
    assert "just check infra" not in log_text(world)


# --- the proposer prompts keep every signature that seed tests call ---


def test_the_proposer_keeps_the_signatures_that_seed_tests_call() -> None:
    assert "Keep the signature of every fn that seed tests call; add a new fn or a wrapper instead." in PROPOSER_HEADER
    assert "restore the old signature" in REPAIR_NOTE and "never edit the test to fix it" in REPAIR_NOTE
    assert "says tests hung" in REPAIR_NOTE and "deadlocks or loops forever" in REPAIR_NOTE
    assert REPAIR_NOTE in PROPOSER_HEADER
