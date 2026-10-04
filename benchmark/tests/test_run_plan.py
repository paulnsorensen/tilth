"""``run.run_plan``: the evolve loop's cell path registers the panel, stamps rows, and serves candidate binaries."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import baselines
import run
from evolve_support import CHEAP, DEV, MODEL, TEST, git, make_repo, make_world
from panel_support import COMPLETE, FB_ALGORITHMS, admit_all, load, read, restratify, stub_loaders, write
from spend import SpendLedger


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    return make_world(monkeypatch, tmp_path)


def cells(*tasks: str, mode: str = "baseline", reps: tuple[int, ...] = (1,)) -> list[run.CellSpec]:
    return [run.CellSpec(task, mode, MODEL, rep) for task in tasks for rep in reps]


def plan(world, specs, *, ledger=None, sha=None, refreeze=False, **kwargs) -> list[dict]:
    return run.run_plan(specs, panel=world.panel, ledger=ledger or SpendLedger(100.0), candidate_sha=sha,
                        refreeze_baselines=refreeze, **kwargs)


# --- c5 AC-8: rows stored through run_plan carry the panel stamp ---


def test_run_plan_rows_carry_panel_stamp(world, monkeypatch: pytest.MonkeyPatch) -> None:
    stored_without_stamp: list[dict] = []
    real_store = baselines.store

    def checking_store(row, *, path):
        if "panel_name" not in row:
            stored_without_stamp.append(row)
        assert path == run.RESULTS_DIR / baselines.STORE_FILENAME
        real_store(row, path=path)

    monkeypatch.setattr(baselines, "store", checking_store)
    plan(world, cells(*CHEAP, *DEV, *TEST) + cells(*DEV, mode="tilth"), sha=world.seed_sha)

    rows = world.stored()
    assert stored_without_stamp == []
    assert len(rows) == 6
    digest = hashlib.sha256(json.dumps({"cheap": sorted(CHEAP), "dev": sorted(DEV), "test": sorted(TEST)},
                                       sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    split = {**{task: "cheap" for task in CHEAP}, **{task: "dev" for task in DEV}, **{task: "test" for task in TEST}}
    for row in rows:
        assert row["panel_name"] == world.panel.name
        assert row["panel_split"] == split[row["task"]]
        assert row["panel_split_digest"] == digest


def test_run_plan_registers_before_planning(world, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    order: list[str] = []
    real_identity = run.planned_identity

    class RecordingPanel:
        def __init__(self, inner):
            self.inner = inner

        def register(self, tasks):
            assert tasks is run.TASKS
            order.append("register")

        def __getattr__(self, name):
            return getattr(self.inner, name)

    def recording_identity(cell):
        order.append(f"plan {cell.task}")
        return real_identity(cell)

    monkeypatch.setattr(run, "planned_identity", recording_identity)
    run.run_plan(cells(*DEV), panel=RecordingPanel(world.panel), ledger=SpendLedger(10.0), candidate_sha=None,
                 refreeze_baselines=False)

    assert order[0] == "register"
    assert [entry for entry in order if entry.startswith("plan")] == [f"plan {task}" for task in DEV]


def test_run_plan_plans_external_member_only_after_registration(bench, monkeypatch: pytest.MonkeyPatch,
                                                                 tmp_path: Path) -> None:
    monkeypatch.setattr(run, "TASKS", dict(run.TASKS))
    monkeypatch.setattr(run.external.preflight, "admit", admit_all)
    stub_loaders(monkeypatch)
    bench.runner(lambda _stream: 0.1)
    panel = load(write(tmp_path, restratify(read(COMPLETE))), bench.store_path)
    assert FB_ALGORITHMS not in run.TASKS
    planned: list[tuple[str, bool]] = []
    real_identity = run.planned_identity

    def recording_identity(cell):
        planned.append((cell.task, cell.task in run.TASKS))
        return real_identity(cell)

    monkeypatch.setattr(run, "planned_identity", recording_identity)
    run.run_plan([run.CellSpec(FB_ALGORITHMS, "plain", "sonnet5", 1)], panel=panel, ledger=SpendLedger(10.0),
                 candidate_sha=None, refreeze_baselines=False)

    assert planned == [(FB_ALGORITHMS, True)]
    assert bench.stored_rows()[0]["panel_name"] == panel.name


# --- c5 AC-8: baselines are bought once, then answered from the store ---


def test_run_plan_reuses_stored_cells(world, tmp_path: Path) -> None:
    output = tmp_path / "out.jsonl"
    plan(world, cells(*DEV), output=output)
    plan(world, cells(*DEV), output=output)

    assert len(world.runner_calls("baseline")) == len(DEV)
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert [row["reused"] for row in rows] == [False, False, True, True]
    assert all(row["panel_name"] == world.panel.name for row in rows)


def test_reused_row_is_restamped_for_this_panel(world, tmp_path: Path) -> None:
    plan(world, cells(*DEV))
    restored = []
    for row in world.stored():
        if row["task"] == "dev_a":
            row = {key: value for key, value in row.items() if not key.startswith("panel_")}
        else:
            row.update(panel_name="another-panel", panel_split="test", panel_split_digest="another-digest")
        restored.append(json.dumps(row))
    world.store_path.write_text("\n".join(restored) + "\n")
    output = tmp_path / "out.jsonl"

    rows = plan(world, cells(*DEV), output=output)

    assert len(world.runner_calls()) == len(DEV)
    assert [row["reused"] for row in rows] == [True, True]
    for row in [*rows, *(json.loads(line) for line in output.read_text().splitlines())]:
        assert {key: row[key] for key in world.panel.stamp(row["task"])} == world.panel.stamp(row["task"])


def test_run_plan_refuses_drift_without_refreeze(world) -> None:
    plan(world, cells(*DEV))
    world.env = "env-fingerprint-b"

    with pytest.raises(run.BaselineDrift, match="env_fingerprint"):
        plan(world, cells(*DEV))
    assert len(world.runner_calls()) == len(DEV)

    plan(world, cells(*DEV), refreeze=True)
    plan(world, cells(*DEV), refreeze=True)
    assert len(world.runner_calls()) == 2 * len(DEV)


def test_run_plan_store_only_never_runs(world) -> None:
    assert plan(world, cells(*DEV), store_only=True) == []
    assert world.runner_calls() == []


def test_run_plan_stops_before_crossing_ceiling(world) -> None:
    ledger = SpendLedger(0.15)
    with pytest.raises(run.PlanStopped) as stopped:
        plan(world, cells(*DEV), ledger=ledger, cell_estimate_usd=0.1)
    assert stopped.value.reason == "ceiling"
    assert len(world.runner_calls()) == 1
    assert ledger.spent == pytest.approx(0.1)


def test_run_plan_stops_on_quota(world, monkeypatch: pytest.MonkeyPatch) -> None:
    def quota(*args, **kwargs):
        world.calls.append(args)
        raise run.QuotaExhaustedError("rejected five_hour rate limit")

    monkeypatch.setattr(run, "run_single", quota)
    with pytest.raises(run.PlanStopped) as stopped:
        plan(world, cells(*DEV))
    assert stopped.value.reason == "quota"
    assert len(world.calls) == 1
    assert world.stored()[0]["infra"] == "quota"


def test_run_plan_refuses_api_key(world, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    with pytest.raises(run.ClaudeAuthError):
        plan(world, cells(*DEV))
    assert world.runner_calls() == []


# --- c5 AC-7: candidate cells are served from the candidate commit's binary ---


@pytest.fixture
def candidate_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A repo whose HEAD is the seed, with one candidate commit the checkout is not on, and a cargo recorder."""
    repo = tmp_path / "tilth"
    seed = make_repo(repo)
    (repo / "src" / "lib.rs").write_text("pub fn changed() {}\n")
    git("commit", "-qam", "candidate", cwd=repo)
    candidate = git("rev-parse", "HEAD", cwd=repo).strip()
    git("checkout", "-q", seed, cwd=repo)
    monkeypatch.setattr(run, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(run, "_CANDIDATE_BUILDS", {})
    builds: list[tuple[str, list[str]]] = []

    def fake_cargo(argv, *, cwd, env, **kwargs):
        builds.append((git("rev-parse", "HEAD", cwd=Path(cwd)).strip(), list(argv)))
        binary = Path(env["CARGO_TARGET_DIR"]) / "release" / "tilth"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b"binary for " + candidate.encode())
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(run, "_run_cargo", fake_cargo)
    return repo, candidate, builds


def test_candidate_build_runs_at_candidate_commit(candidate_repo, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, candidate, builds = candidate_repo
    monkeypatch.setattr(run, "REPO_ROOT", repo)
    first = run.build_candidate(candidate)
    second = run.build_candidate(candidate)

    assert builds == [(candidate, ["cargo", "build", "--release", "--locked"])]
    assert first == second
    assert first.git_sha == candidate
    assert first.binary_sha256 == hashlib.sha256(b"binary for " + candidate.encode()).hexdigest()
    assert Path(first.binary_path).read_bytes() == b"binary for " + candidate.encode()


def test_candidate_build_is_cached_on_disk_across_processes(candidate_repo, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, candidate, builds = candidate_repo
    first = run.build_candidate(candidate, repo=repo)
    monkeypatch.setattr(run, "_CANDIDATE_BUILDS", {})

    assert run.build_candidate(candidate, repo=repo) == first
    assert len(builds) == 1
    assert "candidates" not in git("worktree", "list", cwd=repo)


def test_candidate_cells_record_candidate_identity(world) -> None:
    rows = plan(world, cells(*DEV, mode="tilth"), sha=world.seed_sha)

    assert world.builds == [world.seed_sha]
    assert [call[3] for call in world.runner_calls("tilth")] == [world.seed_sha] * len(DEV)
    for row in rows:
        assert row["git_sha"] == row["variant"]["git_sha"] == world.seed_sha
        assert row["binary_sha256"] == row["variant"]["binary_sha256"] == f"sha256-of-{world.seed_sha}"
    assert run.MODES["tilth"].git_sha is None


def test_run_py_candidate_sha_flag(bench, monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[str] = []

    def fake_build(sha):
        built.append(sha)
        return run.CandidateBuild(git_sha=sha, binary_path="/bin/true", binary_sha256="sha256-candidate")

    monkeypatch.setattr(run, "build_candidate", fake_build)
    monkeypatch.setitem(run.MODES, "tilth", run.replace(run.MODES["tilth"], binary_path="/bin/true"))
    bench.runner(lambda _stream: 0.1)

    assert bench.main("--tasks", "cell_a", "--modes", "tilth", "--models", "sonnet5", "--reps", "1",
                      "--max-usd", "5", "--candidate-sha", "abc123") == 0

    assert built == ["abc123"]
    row = bench.output_rows()[0]
    assert row["git_sha"] == "abc123" and row["binary_sha256"] == "sha256-candidate"


def test_retried_cell_keeps_each_attempts_sidecar(world) -> None:
    world.baseline["dev_a"] = "E"
    plan(world, cells("dev_a"))
    plan(world, cells("dev_a"))

    failed = [row for row in world.stored() if row["task"] == "dev_a"]
    assert len(failed) == 2 and all(row.get("error") for row in failed)
    paths = [row["trajectory_path"] for row in failed]
    assert len(set(paths)) == 2 and all(Path(path).is_file() for path in paths)


def test_failed_candidate_build_reports_cargo_error(candidate_repo, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, candidate, _builds = candidate_repo

    def failing_cargo(argv, *, cwd, env, **kwargs):
        subprocess.run(["git", "worktree", "remove", "--force", str(cwd)], cwd=repo, check=True)
        return subprocess.CompletedProcess(argv, 101, "", "error[E0425]: cannot find value `x`")

    monkeypatch.setattr(run, "_run_cargo", failing_cargo)
    with pytest.raises(RuntimeError, match=r"E0425"):
        run.build_candidate(candidate, repo=repo)


def test_run_plan_reports_any_build_failure_as_candidate_build_failed(world, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(sha, repo):
        raise subprocess.CalledProcessError(128, ["git", "worktree", "add"], stderr="fatal: invalid reference")

    monkeypatch.setattr(run, "_build_candidate", failing)
    with pytest.raises(run.CandidateBuildFailed, match="fatal: invalid reference"):
        plan(world, cells(*DEV, mode="tilth"), sha=world.seed_sha)
    assert world.runner_calls() == []
    assert run.MODES["tilth"].git_sha is None
