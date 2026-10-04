"""Cross-cutting benchmark invariants: a panel run reaches every member, refuses incomplete panels, and never starts a container."""

import io
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import external.preflight
import run
from conftest import STREAMS
from panel_support import (
    CHEAP, COMPLETE, FB_IDS, GO_SLOT, RENDER, RUST_SLOT, SYNTHETIC_FB, admit_all, hermetic_repos, panel_run, read,
    restratify, seed_synthetic_data, synthetic_panel, without, write,
)

CONTAINER_TOOLS = {"docker", "podman", "nerdctl"}
RUNNER_TOOLS = {"claude", "codex"}


# --- c3 AC-1: every panel member reaches the runner ---


def test_complete_panel_reaches_runner(bench, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    prun = panel_run(bench, monkeypatch, tmp_path)

    assert prun.main(prun.panel(read(COMPLETE))) == 0

    members = {entry["id"] for entry in read(COMPLETE)["members"]}
    assert sorted(prun.called) == sorted(members)


# --- c3 AC-2: a paid panel requires every task family ---


@pytest.mark.parametrize(("missing", "named"), [
    (FB_IDS, "featurebench"),
    ((RENDER,), RENDER),
    *(((name,), name) for name in CHEAP),
    ((GO_SLOT,), GO_SLOT),
    ((RUST_SLOT,), RUST_SLOT),
], ids=["featurebench", RENDER, *CHEAP, "go-slot", "rust-slot"])
def test_paid_panel_requires_every_task_family(bench, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                               capsys: pytest.CaptureFixture[str], missing: tuple[str, ...],
                                               named: str) -> None:
    prun = panel_run(bench, monkeypatch, tmp_path)
    incomplete = restratify(without(read(COMPLETE), *missing))

    assert prun.main(prun.panel(incomplete)) != 0

    assert prun.called == []
    assert named in capsys.readouterr().err


# --- c3 AC-1 / F-1: no container on an external panel cell ---


class _Replay:
    """A Popen stand-in that replays a canned claude stream."""

    def __init__(self, stream: str) -> None:
        self.stdout = io.StringIO(stream)
        self.stderr = io.StringIO("")
        self.returncode = 0

    def wait(self) -> int:
        return 0

    def kill(self) -> None:
        self.returncode = -9


def _program_names(argv: list[str]) -> set[str]:
    words = [word for part in argv for word in str(part).replace(";", " ").replace("|", " ").replace("(", " ").split()]
    return {Path(word.strip("'\"")).name for word in words}


def test_external_cell_never_invokes_container(bench, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    hermetic_repos(monkeypatch, tmp_path)
    seed_synthetic_data(monkeypatch, tmp_path)
    monkeypatch.setattr(run, "TASKS", dict(run.TASKS))
    monkeypatch.setattr(external.preflight, "admit", admit_all)
    stream = (STREAMS / "claude_native_cost.jsonl").read_text()
    recorded: list[tuple[str | None, list[str]]] = []
    current: list[str | None] = [None]
    real_popen = subprocess.Popen

    def recording_popen(argv, *args, **kwargs):
        argv = [str(part) for part in argv] if not isinstance(argv, str) else [argv]
        recorded.append((current[0], argv))
        if Path(argv[0]).name in RUNNER_TOOLS:
            return _Replay(stream)
        if argv[:2] == ["uv", "venv"]:
            # A stdlib venv that sees the host pytest; uv itself would fetch an interpreter.
            argv = [sys.executable, "-m", "venv", "--without-pip", "--system-site-packages", argv[-1]]
        elif argv[:2] == ["uv", "pip"]:
            argv = ["true"]
        return real_popen(argv, *args, **kwargs)

    real_run_single = run.run_single

    def tagged_run_single(task_name, *args, **kwargs):
        current[0] = task_name
        try:
            return real_run_single(task_name, *args, **kwargs)
        finally:
            current[0] = None

    monkeypatch.setattr(subprocess, "Popen", recording_popen)
    monkeypatch.setattr(run, "run_single", tagged_run_single)
    data = synthetic_panel()

    assert bench.main("--panel", str(write(tmp_path, data)), "--models", "sonnet5", "--modes", "plain",
                      "--reps", "1", "--max-usd", "50") == 0

    containers = [argv for _cell, argv in recorded if _program_names(argv) & CONTAINER_TOOLS]
    assert containers == []
    members = [entry["id"] for entry in data["members"]]
    for name in members:
        cell = [argv for tag, argv in recorded if tag == name]
        assert any(Path(argv[0]).name == "claude" for argv in cell), name
    for name in (SYNTHETIC_FB, GO_SLOT, RUST_SLOT):
        native = [argv for tag, argv in recorded if tag == name and Path(argv[0]).name not in RUNNER_TOOLS]
        assert native, f"{name} recorded no prepare or grading command"
    graded = {row["task"]: row for row in bench.stored_rows()}
    for name in (SYNTHETIC_FB, GO_SLOT, RUST_SLOT):
        assert not graded[name].get("error"), graded[name].get("error")
        assert graded[name]["f2p_total"] == 1 and graded[name]["p2p_total"] == 1


# --- c5: the evolution loop never shows the grader to reflection, scores only by correctness ---

import json  # noqa: E402

import evolve_support as es  # noqa: E402
from evolve import candidate as evolve_candidate  # noqa: E402
from evolve import engine as evolve_engine  # noqa: E402
from evolve.finish import GitHubPRClient  # noqa: E402
from judge import core as judge_core  # noqa: E402
from judge import store as judge_store  # noqa: E402


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    return es.make_world(monkeypatch, tmp_path)


def _ready(world, *extra: str, **kwargs):
    evo = es.build(world, *extra, **kwargs)
    assert evo.preflight() is None and evo.buy_baselines() is None
    return evo


def _engine(monkeypatch: pytest.MonkeyPatch, candidates=(), **kwargs) -> dict:
    fake, seen = es.scripted_engine(list(candidates), **kwargs)
    monkeypatch.setattr(evolve_engine, "optimize_anything", fake)
    return seen


def _records(info: dict) -> list[dict]:
    return info.get("records", [])


def test_reflective_record_has_untruncated_tool_io(world) -> None:
    evo = _ready(world)
    _score, info = evo.evaluate(evo.seed, "dev_a")

    records = _records(info)
    assert records
    for record in records:
        calls = [json.loads(line) for line in record["trajectory"].splitlines()]
        assert any(call["output"].startswith(es.LONG_OUTPUT) and len(call["output"]) > 80 for call in calls)


def test_reflective_record_strips_grader_text(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = es.make_world(monkeypatch, tmp_path, es.seed_files(es.mcp(dev_a="0", dev_b="0", cheap_a="0", test_a="0")))
    world.judge.calibrated = True
    prompts: list[str] = []

    def spawn(argv, **kwargs):
        prompts.append(kwargs["input"])
        return subprocess.CompletedProcess(argv, 0, es.result_stream("no change"), "")

    seen = _engine(monkeypatch, propose=[["prompts/mcp.md"], ["src_patch"]])
    assert es.main(world, spawn=spawn) == 0

    stored = [row for row in world.stored() if row["mode"] == "tilth"]
    assert any(row["correctness_reason"] == "Missing: SECRET_GT" for row in stored)
    assert "SECRET_GT" in run.TASKS["dev_a"].ground_truth.required_strings
    critiques = [es.StubJudge().critique(row, trajectory) for row, trajectory in world.judge.critiques]
    assert critiques and len(prompts) == 2
    for text in (json.dumps(seen["side_infos"]), *critiques, *prompts):
        assert "SECRET_GT" not in text


def test_agent_output_kept_verbatim(world) -> None:
    evo = _ready(world)
    _score, info = evo.evaluate(evo.seed, "dev_a")

    for record in _records(info):
        assert "AGENT_GT" in record["row"]["result_text"]
        assert "AGENT_GT" in record["trajectory"]
        assert "dispatch AGENT_GT" in record["trajectory"]


def test_contaminated_rollout_unreflected(world) -> None:
    world.judge.calibrated = True
    evo = _ready(world)
    candidate = es.child(evo.seed, dev_a="CC", dev_b="11", cheap_a="1")
    infos = {task: evo.evaluate(candidate, task)[1] for task in es.DEV}

    sha = evo.results[evolve_candidate.content_id(candidate)].sha
    assert _records(infos["dev_a"]) == []
    assert len(_records(infos["dev_b"])) == len([row for row in world.stored()
                                                 if row.get("git_sha") == sha and row["task"] == "dev_b"])
    critiqued = [(row["task"], row["contaminated"]) for row, _ in world.judge.critiques if row.get("git_sha") == sha]
    assert ("dev_a", True) not in critiqued
    assert ("dev_b", False) in critiqued


def test_score_ignores_labels_and_critiques(world) -> None:
    candidate = es.child(es.seed_candidate(), dev_a="10", dev_b="11", cheap_a="1")
    scores = []
    for index, judge in enumerate([es.StubJudge(labels={"dev_a": "strong"}, calibrated=True),
                                   es.StubJudge(labels={"dev_a": "none"}, calibrated=False)]):
        evo = _ready(world, "--run-id", f"score{index}", judge_factory=lambda _ledger, _floor, judge=judge: judge)
        scores.append({task: evo.evaluate(candidate, task)[0] for task in es.DEV})
    assert scores[0] == scores[1]


def test_contaminated_rollout_scores_incorrect(world) -> None:
    evo = _ready(world, "--reruns", "0")
    candidate = es.child(evo.seed, dev_a="C", dev_b="1", cheap_a="1")
    assert evo.evaluate(candidate, "dev_a")[0] == 0
    assert evo.evaluate(candidate, "dev_b")[0] == 1


def test_uncalibrated_judge_withholds_labels(world, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _engine(monkeypatch, [es.child(es.seed_candidate(), dev_a="1", dev_b="1", cheap_a="1", tag="c")])
    evo = es.build(world)
    assert evo.run() == 0

    records = [record for info in seen["side_infos"] for record in _records(info)]
    assert records and all("label" not in record and "critique" not in record for record in records)
    assert world.judge.critiques == []
    assert "labels: uncalibrated" in es.log_text(world)
    assert evo.deltas and all(delta["labels"] == "uncalibrated" for delta in evo.deltas)


def test_calibrated_judge_labels_flow(world, monkeypatch: pytest.MonkeyPatch) -> None:
    world.judge.calibrated = True
    world.judge.labels = {"dev_a": "strong", "dev_b": "weak"}
    seen = _engine(monkeypatch)
    evo = es.build(world)
    assert evo.run() == 0

    assert set(world.judge.labelled) >= {*es.CHEAP, *es.DEV, *es.TEST}
    assert "label_kappa=0.90" in es.log_text(world)
    records = [record for info in seen["side_infos"] for record in _records(info)]
    assert records and all(record["label"] in {"strong", "weak"} and record["critique"].startswith("verdict:")
                           for record in records)
    assert all(set(delta["labels"]) == {"strong", "weak"} for delta in evo.deltas)


def test_label_is_categorical(world, capsys: pytest.CaptureFixture[str]) -> None:
    class FreeText:
        def __call__(self, prompt):
            return judge_core.JudgeReply(text="it is quite strong, I think", cost=0.0, cost_source="native")

    factory = lambda ledger, floor: judge_core.Judge(ledger, FreeText(), cell_estimate_usd=floor)
    assert es.main(world, judge_factory=factory) != 0

    assert world.calls == []
    assert all(judge_store.cached_label(run._cell_task_digest(run.TASKS[task])) is None
               for task in (*es.CHEAP, *es.DEV, *es.TEST))
    assert "dev_a" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["panel", "outside", "data", "benchmark"])
def test_proposer_cannot_see_grader_inputs(world, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    data_dir = world.tmp / "bench-data"
    monkeypatch.setenv("TILTH_BENCH_DATA", str(data_dir))
    exports: list[Path] = []
    prompts: list[str] = []

    def spawn(argv, **kwargs):
        export = Path(kwargs["cwd"])
        exports.append(export)
        prompts.append(kwargs["input"])
        assert not (export / "benchmark").exists() and not (export / ".git").exists()
        (export / "src" / "main.rs").write_text("fn main() {}\n// proposed\n")
        tool = {
            "panel": ("Read", {"file_path": str(world.tmp / "panel.json")}, "{}"),
            "outside": ("Read", {"file_path": "../../etc/passwd"}, "root"),
            "data": ("Grep", {"pattern": "x", "path": str(data_dir)}, ""),
            "benchmark": ("Grep", {"pattern": "grader", "path": "src"}, "see benchmark/run.py for the grader"),
        }[kind]
        events = [{"type": "assistant", "message": {"content": [
                      {"type": "tool_use", "id": "t1", "name": tool[0], "input": tool[1]}]}},
                  {"type": "user", "message": {"content": [
                      {"type": "tool_result", "tool_use_id": "t1", "content": tool[2]}]}}]
        return subprocess.CompletedProcess(argv, 0, es.result_stream("done", events=events), "")

    seen = _engine(monkeypatch, propose=[["src_patch"]])
    evo = es.build(world, spawn=spawn)
    assert evo.run() == 0

    assert seen["proposals"] == [{"src_patch": ""}]
    assert exports
    assert [ref for ref in es.git("for-each-ref", "--format=%(refname)", "refs/evolve", cwd=world.repo).split()
            if not ref.endswith(evolve_candidate.content_id(evo.seed)[:12])] == []
    assert "[test_a:" not in prompts[0]


def test_baselines_bought_once_then_reused(world, monkeypatch: pytest.MonkeyPatch) -> None:
    _engine(monkeypatch)
    assert es.main(world) == 0
    planned = {(task, rep) for task in (*es.CHEAP, *es.DEV, *es.TEST) for rep in (1, 2)}
    assert sorted((task, rep) for task, _mode, rep, _sha in world.runner_calls("baseline")) == sorted(planned)

    assert es.main(world, "--run-id", "run2") == 0
    assert len(world.runner_calls("baseline")) == len(planned)
    rows = [json.loads(line) for line in (run.RESULTS_DIR / "evolve" / "run2" / "rows.jsonl").read_text().splitlines()]
    baseline_rows = [row for row in rows if row["mode"] == "baseline"]
    assert {(row["task"], row["repetition"]) for row in baseline_rows} == planned
    assert all(row["reused"] is True for row in baseline_rows)


def test_env_drift_refuses_without_refreeze(world, monkeypatch: pytest.MonkeyPatch,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    _engine(monkeypatch)
    assert es.main(world) == 0
    calls = len(world.calls)
    world.env = "env-fingerprint-b"

    assert es.main(world, "--run-id", "run2") != 0
    assert len(world.calls) == calls
    assert "--refreeze-baselines" in capsys.readouterr().err


def test_frontier_needs_reruns_and_frozen_delta(world) -> None:
    evo = _ready(world)
    baseline_calls = len(world.runner_calls("baseline"))
    flaky = es.child(evo.seed, dev_a="1E", dev_b="11", cheap_a="1", tag="flaky")
    steady = es.child(evo.seed, dev_a="11", dev_b="11", cheap_a="1", tag="steady")
    evo.evaluate(flaky, "dev_a")
    evo.evaluate(steady, "dev_a")

    assert not evo.results[evolve_candidate.content_id(flaky)].accepted
    accepted = evo.results[evolve_candidate.content_id(steady)]
    assert accepted.accepted and accepted in evo.frontier
    assert sorted(rep for task, rep in [(call[0], call[2]) for call in world.calls
                                        if call[3] == accepted.sha and call[0] == "dev_a"]) == [1, 2]
    assert len(world.runner_calls("baseline")) == baseline_calls


def test_winner_is_unmerged_allowlisted_draft(world) -> None:
    gh: list[list[str]] = []

    def run_gh(argv, **kwargs):
        gh.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "https://example.invalid/pr/1\n", "")

    remote = world.tmp / "remote.git"
    es.git("init", "-q", "--bare", str(remote), cwd=world.tmp)
    es.git("remote", "add", "origin", str(remote), cwd=world.repo)
    client = GitHubPRClient(world.repo, remote="origin", run=run_gh)
    evo = _ready(world, pr_client=client)
    seed = evo.new_cascade(evo.seed)
    seed.sha, seed.means, seed.accepted, seed.just_check_ok = world.seed_sha, {"dev_a": 0.0, "dev_b": 0.0}, True, True
    evo.frontier.append(seed)
    candidate = es.child(evo.seed, test_a="1")
    winner = evo.new_cascade(candidate)
    winner.sha = evo.materializer.materialize(candidate)
    winner.means, winner.accepted, winner.just_check_ok = {"dev_a": 1.0, "dev_b": 1.0}, True, True
    evo.frontier.append(winner)
    evo.finish()

    assert not any(hasattr(client, name) for name in ("merge", "enable_auto_merge", "auto_merge"))
    [create] = gh
    assert "--draft" in create and not any("merge" in part for part in create)
    changed = es.git("diff", "--name-only", world.seed_sha, winner.sha, cwd=world.repo).split()
    assert changed and all(path.startswith(("src/", "prompts/")) or path == "AGENTS.md" for path in changed)

    # A winner commit that reaches outside the allowlist is never pushed.
    (world.repo / "Cargo.toml").write_text("[package]\nname = \"tilth\"\nversion = \"9.9.9\"\n")
    es.git("commit", "-qam", "bump", cwd=world.repo)
    winner.sha = es.git("rev-parse", "HEAD", cwd=world.repo).strip()
    gh.clear()
    assert evo.finish() is None
    assert gh == []
    assert "evolve/run1-winner" in es.git("ls-remote", "--heads", str(remote), cwd=world.tmp)


def test_one_ceiling_spans_all_paid_calls(world, monkeypatch: pytest.MonkeyPatch) -> None:
    class Client:
        calls = 0

        def __call__(self, prompt):
            Client.calls += 1
            return judge_core.JudgeReply(text="strong" if "Ground truth" in prompt else "verdict: apt\nok",
                                         cost=0.2, cost_source="native")

    evo = _ready(world, max_usd="10", judge_factory=lambda ledger, floor: judge_core.Judge(
        ledger, Client(), cell_estimate_usd=floor))
    Client.calls = 0
    evo.ledger.charge(evo.ledger.max_usd - evo.ledger.reserve - evo.ledger.spent - 0.05, source="search")
    spawned = len(world.spawned)

    assert evo.dispatcher(evo.seed, {name: [{"records": []}] for name in evo.seed}, list(evo.seed)) == evo.seed
    assert len(world.spawned) == spawned
    assert evo.stop.reason == "ceiling"
    evo.stop.reason = None
    judge_store.write_agreement(judge_store.Agreement(calibrated=True, **judge_store.current_stamp()))
    row = next(row for row in world.stored() if row["mode"] == "baseline")
    with pytest.raises(judge_core.JudgeSpendCeiling):
        evo.judge.critique(row, Path(row["trajectory_path"]).read_text())
    assert Client.calls == 0
    assert evo.ledger.spent <= evo.ledger.max_usd

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert es.main(world, "--run-id", "keyed") != 0
    assert len(world.spawned) == spawned
