"""Evolve remembers the patches that failed a gate, so a re-proposed patch costs no check, repair, or cell.

No test calls claude, codex, or cargo: the proposer is a stub and ``just check`` fails any worktree that holds BROKEN.
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import run
from evolve import candidate as candidates
from evolve import failures
from evolve.candidate import content_id
from evolve.integrity import InfrastructureFailure
from evolve_support import build, log_text, main, seed_candidate
from test_evolve import ready, world  # noqa: F401  (world is a fixture)
from test_evolve_repair import Harness, broken_patch, fixed_patch

SRC = candidates.SRC_PATCH
# The world fixture replaces run._build_candidate; this is the real one, read at import, before any fixture runs.
_REAL_BUILD = run._build_candidate


def failed_child(world) -> str:
    return content_id({**seed_candidate(), SRC: broken_patch(world)})


def prior_run(tmp_path: Path, cid: str, stage: str = "just check", error: str = "error: prior failure") -> Path:
    prior = tmp_path / "prior"
    prior.mkdir()
    (prior / "log.txt").write_text(f"{failures.header(cid, stage)}\n  {error}\n  error: Recipe `check` failed\n")
    return prior


def failed_records(harness: Harness) -> list[list[dict]]:
    return [[record for record in records if "failed_patches" in record] for _candidate, records in harness.calls]


# --- within a run ---


def test_a_patch_that_failed_a_gate_gets_no_second_check_or_repair(world) -> None:  # noqa: F811
    broken, fixed = broken_patch(world), fixed_patch(world)
    harness = Harness(world, patches=[broken, fixed, broken])
    harness.dispatch()
    checked, called = len(harness.checked), len(harness.calls)

    proposal = harness.dispatch()

    assert proposal == {SRC: broken}
    assert len(harness.calls) == called + 1  # the proposal itself; no repair call
    assert len(harness.checked) == checked
    assert f"repair: the child already failed a gate; no repair" in log_text(world)


def test_a_later_src_patch_proposal_sees_the_earlier_failed_patch(world) -> None:  # noqa: F811
    harness = Harness(world, patches=[broken_patch(world), fixed_patch(world), fixed_patch(world)])
    harness.dispatch()
    harness.dispatch()

    first, _repair, later = failed_records(harness)
    assert first == [] and later == [{"failed_patches": [
        {"id": failed_child(world)[:12], "stage": "just check", "error": "error: this `map_or` can be simplified"}]}]


def test_the_cascade_fails_a_known_patch_without_a_cell_or_a_check(world) -> None:  # noqa: F811
    harness = Harness(world, patches=[])
    child = {**seed_candidate(), SRC: broken_patch(world)}
    harness.evo.failed.add(content_id(child), "just check", "error: seeded failure")
    calls, checked = len(world.calls), len(harness.checked)

    result = harness.evo.cascade(child)

    assert result.stage == "just check" and result.tail == "error: seeded failure"
    assert len(world.calls) == calls and len(harness.checked) == checked
    assert f"candidate {content_id(child)[:12]}: skipped, it already failed at just check" in log_text(world)


# --- across runs ---


def test_a_prior_run_dir_seeds_the_ledger_and_the_proposal_records(world, tmp_path) -> None:  # noqa: F811
    prior = prior_run(tmp_path, failed_child(world))
    harness = Harness(world, "--avoid-failures-from", str(prior), patches=[broken_patch(world), fixed_patch(world)])

    proposal = harness.dispatch()

    assert proposal == {SRC: broken_patch(world)}
    assert len(harness.calls) == 1 and harness.checked == []
    [record] = [record for record in harness.calls[0][1] if "failed_patches" in record]
    assert record["failed_patches"] == [{"id": failed_child(world)[:12], "stage": "just check",
                                         "error": "error: prior failure"}]


def test_the_flag_repeats_and_each_dir_seeds(world, tmp_path) -> None:  # noqa: F811
    first = prior_run(tmp_path, "a" * 12)
    second = tmp_path / "second"
    second.mkdir()
    (second / "log.txt").write_text(failures.header("b" * 12, "build") + "\n  error[E0425]: no x\n")

    evo = build(world, "--avoid-failures-from", str(first), "--avoid-failures-from", str(second))

    assert evo.failed.get("a" * 12) == ("just check", "error: prior failure")
    assert evo.failed.get("b" * 12) == ("build", "error[E0425]: no x")


def test_a_resumed_run_knows_the_failures_in_its_own_log(world) -> None:  # noqa: F811
    harness = Harness(world, patches=[broken_patch(world), fixed_patch(world)])
    harness.dispatch()

    resumed = build(world)

    assert resumed.failed.get(failed_child(world)) == ("just check", "error: this `map_or` can be simplified")


def test_a_prior_dir_without_a_log_is_refused(world, tmp_path, capsys) -> None:  # noqa: F811
    missing = tmp_path / "nothing"
    missing.mkdir()

    assert main(world, "--avoid-failures-from", str(missing)) == 2
    assert "has no log.txt" in capsys.readouterr().err


# --- the log reader ---


def test_a_cheap_tier_loss_is_not_a_known_failure() -> None:
    log = failures.header("c" * 12, "cheap tier") + "\n  p=0\n"

    assert failures.parse_log(log) == []


def test_a_build_infra_fault_is_not_a_known_failure() -> None:
    log = failures.header("d" * 12, "build infra") + "\n  OSError: no space left on device\n"

    assert failures.parse_log(log) == []
    ledger = failures.FailedPatches()
    ledger.add("d" * 12, "build infra", "OSError: no space left on device")
    assert ledger.get("d" * 12) is None


def test_the_failure_line_format_is_pinned() -> None:
    # The loop writes this line and parse_log reads it from prior logs: change both together.
    line = failures.header("0123456789abcdef", "just check")

    assert line == "candidate 0123456789ab: failed at just check"
    assert failures.parse_log(line + "\n  error: x\n") == [("0123456789ab", "just check", "error: x")]


# Lines 50-51, 53 and 80-82 of a real log, gepa-v3-r3/log.txt, from before `just check infra` existed: a `just check`
# timeout was then logged at `just check`, with the timeout message as its whole tail.
PRE_INFRA_LOG = (
    "candidate 0ab56f8b2201: failed at just check\n"
    "  python3 scripts/verify.py\n"
    "  error: `format!(..)` appended to existing `String`\n"
    "candidate 417af30cba9a: failed at just check\n"
    "  just check timed out after 1800 s\n"
    'hypothesis: ba58db318a04 -> 417af30cba9a src_patch unmeasurable {"child": null, "parent": 2.8000000000000003}\n'
)


def test_a_just_check_timeout_in_an_older_log_is_not_a_known_failure(world, tmp_path) -> None:  # noqa: F811
    prior = tmp_path / "gepa-v3-r3"
    prior.mkdir()
    (prior / "log.txt").write_text(PRE_INFRA_LOG)

    evo = build(world, "--avoid-failures-from", str(prior))

    assert evo.failed.get("417af30cba9a") is None
    assert evo.failed.get("0ab56f8b2201") == ("just check", "error: `format!(..)` appended to existing `String`")


# --- build faults ---


def test_an_infra_build_error_is_not_remembered_and_the_patch_is_built_again(world, monkeypatch) -> None:  # noqa: F811
    harness = Harness(world, patches=[])
    builds = []

    def build_candidate(sha, repo=None):
        builds.append(sha)
        raise OSError("No space left on device")

    monkeypatch.setattr(run, "build_candidate", build_candidate)
    child = {**seed_candidate(), SRC: fixed_patch(world)}
    for _ in range(2):
        result = harness.evo.new_cascade(child)
        assert harness.evo._static_checks(result) is None
        failed = harness.evo._hook_gate(result)
        assert failed is not None and failed.stage == "build infra"

    assert harness.evo.failed.get(content_id(child)) is None
    assert not harness.evo._failed_before(child)
    assert len(builds) == 2
    assert failures.header(content_id(child), "build infra") in log_text(world)


def compile_fails(world, monkeypatch) -> list[list[str]]:
    """Build the seed with the world's fake and any other sha with the real ``_build_candidate``; its cargo exits 101."""
    compiled: list[list[str]] = []

    def build_sha(sha, repo=None):
        return world.fake_build(sha, repo) if sha == world.seed_sha else _REAL_BUILD(sha, repo)

    def cargo(argv, *, cwd, env):
        compiled.append(list(argv))
        return subprocess.CompletedProcess(argv, 101, "", "error[E0308]: mismatched types\n")

    monkeypatch.setattr(run, "_build_candidate", build_sha)
    monkeypatch.setattr(run, "_run_cargo", cargo)
    return compiled


def test_a_compile_failure_is_remembered_and_skipped_next_time(world, monkeypatch) -> None:  # noqa: F811
    # The real _build_candidate raises a plain RuntimeError and run_plan wraps it; no CandidateBuildFailed by hand.
    harness = Harness(world, patches=[])
    compiled = compile_fails(world, monkeypatch)
    child = {**seed_candidate(), SRC: fixed_patch(world)}

    result = harness.evo.cascade(child)

    assert result.stage == "build" and len(compiled) == 1
    assert harness.evo.failed.get(content_id(child)) == ("build", "error[E0308]: mismatched types")
    assert build(world).failed.get(content_id(child)) is not None  # a resume skips it


def test_a_compile_failure_at_the_hook_gate_is_a_build_failure(world, monkeypatch) -> None:  # noqa: F811
    harness = Harness(world, patches=[])
    compile_fails(world, monkeypatch)
    child = {**seed_candidate(), SRC: fixed_patch(world)}
    result = harness.evo.new_cascade(child)
    assert harness.evo._static_checks(result) is None

    failed = harness.evo._hook_gate(result)

    assert failed is not None and failed.stage == "build"
    assert harness.evo.failed.get(content_id(child)) == ("build", "error[E0308]: mismatched types")


@pytest.mark.parametrize("names_seed", [False, True])
def test_a_run_plan_build_fault_that_is_not_the_candidate_compiling_is_build_infra(world, monkeypatch,  # noqa: F811
                                                                                     names_seed) -> None:
    harness = Harness(world, patches=[])

    def build_sha(sha, repo=None):
        if sha == world.seed_sha:
            return world.fake_build(sha, repo)
        if names_seed:  # a compile failure of another commit, such as the seed's strict hook build in run_plan
            raise RuntimeError(f"cargo build --release --locked failed at {world.seed_sha}:\nerror: x")
        raise OSError("No space left on device")

    monkeypatch.setattr(run, "_build_candidate", build_sha)
    child = {**seed_candidate(), SRC: fixed_patch(world)}

    result = harness.evo.cascade(child)

    assert result.stage == "build infra"
    assert harness.evo.failed.get(content_id(child)) is None


def test_a_just_check_timeout_is_not_remembered_and_the_patch_is_checked_again(world) -> None:  # noqa: F811
    harness = Harness(world, patches=[])
    checks = []

    def timed_out(worktree):
        checks.append(worktree)
        raise InfrastructureFailure("just check timed out after 1800 s")

    harness.evo.just_check = timed_out
    child = {**seed_candidate(), SRC: fixed_patch(world)}
    for _ in range(2):
        failed = harness.evo._static_checks(harness.evo.new_cascade(child))
        assert failed is not None and failed.stage == "just check infra"

    assert len(checks) == 2
    assert harness.evo.failed.get(content_id(child)) is None
    assert failures.header(content_id(child), "just check infra") in log_text(world)
    assert build(world).failed.get(content_id(child)) is None


@pytest.mark.parametrize("strict", [True, False])  # strict builds at the hook gate; without it, run_plan builds
def test_a_build_infra_fault_keeps_nothing_so_the_next_evaluation_builds_again(world, monkeypatch,  # noqa: F811
                                                                                strict) -> None:
    harness = Harness(world, *([] if strict else ["--no-strict-file-tools"]), patches=[])
    builds = []

    def build_sha(sha, repo=None):
        if sha == world.seed_sha:
            return world.fake_build(sha, repo)
        builds.append(sha)
        raise OSError("No space left on device")

    monkeypatch.setattr(run, "_build_candidate", build_sha)
    child = {**seed_candidate(), SRC: fixed_patch(world)}

    stages = [harness.evo.cascade(child).stage for _ in range(2)]

    assert stages == ["build infra", "build infra"]
    assert len(builds) == 2
    assert content_id(child) not in harness.evo.results


def test_the_proposal_lists_only_the_latest_failures() -> None:
    ledger = failures.FailedPatches()
    for index in range(failures.MAX_LISTED + 3):
        ledger.add(f"{index:012x}", "build", f"error: failure {index}")

    listed = ledger.summary()

    assert [entry["error"] for entry in listed] == [f"error: failure {index}"
                                                    for index in range(3, failures.MAX_LISTED + 3)]


def test_the_first_error_prefers_the_failed_test_then_a_compiler_error() -> None:
    tail = ("error: could not compile `tilth`\nfailures:\n---- edit::pin ----\nassertion failed\n"
            "error: Recipe `check` failed\n")

    assert failures.first_error(tail) == "---- edit::pin ----"
    assert failures.first_error("   Compiling x\nerror[E0425]: no x\n") == "error[E0425]: no x"
    assert failures.first_error("") == ""
