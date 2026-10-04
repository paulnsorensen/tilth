"""run.py task hooks for external tasks: prepare, timeouts, identity, grading rows, guard, contamination."""

import io
import json
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
import external
import external.featurebench
import external.patches
import external.preflight
import external.swebench_ml
import run
from conftest import STREAMS, StoreTask
from external_support import FB_LV1, SWE_GO, SWE_PY, SWE_RS, git, rows

_ARGS = ("--models", "sonnet5", "--modes", "plain", "--reps", "1", "--max-usd", "5")


class _Replay:
    """A Popen stand-in that replays a canned claude stream."""

    def __init__(self, stream: str) -> None:
        self.stdout = io.StringIO(stream)
        self.stderr = io.StringIO("")
        self.returncode = 0

    def wait(self) -> None:
        return None

    def kill(self) -> None:
        self.returncode = -9


def claude_stream(tool_name: str = "Bash", tool_input: dict | None = None) -> str:
    """The canned native-cost stream with its first tool call replaced."""
    lines = []
    for line in (STREAMS / "claude_native_cost.jsonl").read_text().splitlines():
        event = json.loads(line)
        for block in event.get("message", {}).get("content", []) if isinstance(event.get("message"), dict) else []:
            if block.get("type") == "tool_use" and block.get("id") == "toolu_fixture_1" and tool_input is not None:
                block["name"], block["input"] = tool_name, tool_input
        lines.append(json.dumps(event))
    return "\n".join(lines) + "\n"


def run_cell(monkeypatch: pytest.MonkeyPatch, task_name: str, workdir: Path, tmp_path: Path,
             stream: str | None = None) -> dict:
    real_popen = subprocess.Popen

    def popen(cmd, *args, **kwargs):
        # Only the agent CLI is replayed; grading subprocesses run for real.
        return _Replay(stream or claude_stream()) if cmd[0] == "claude" else real_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(run.subprocess, "Popen", popen)
    return run._run_single_in_repo(task_name, "plain", "sonnet5", 0, workdir,
                                   stream_log_path=tmp_path / "streams" / "cell.jsonl")


def load(instance_id: str) -> external.ExternalTask:
    if instance_id in rows("featurebench"):
        return external.featurebench.load(instance_id, external.FEATUREBENCH_REVISION)
    return external.swebench_ml.load(instance_id, external.SWEBENCH_ML_REVISION)


def _verdict(instance_id: str, admitted: bool, reason: str) -> external.preflight.PreflightVerdict:
    return external.preflight.PreflightVerdict(
        instance_id=instance_id, dataset="swebench_ml", data_rev=external.SWEBENCH_ML_REVISION,
        env_fingerprint="stub", admitted=admitted, reason=reason,
    )


@dataclass
class PreparedTask(StoreTask):
    repo: str = "not_a_fixture_repo"
    prepared_into: Path | None = None

    def prepare(self, workdir: Path) -> None:
        workdir.mkdir(parents=True)
        (workdir / "PREPARED").write_text("yes\n")
        self.prepared_into = workdir


@dataclass
class SlowTask(StoreTask):
    timeout_s: int = 1800


def _capture_cells(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    seen: list[Path] = []

    def fake_cell(task_name, mode_name, model_name, repetition, repo_path, **kwargs):
        seen.append(Path(repo_path))
        assert (Path(repo_path)).is_dir()
        return {"task": task_name, "mode": mode_name, "model": run.MODELS[model_name], "repetition": repetition,
                "correct": True, "correctness_reason": "stub", "num_turns": 1, "context_tokens": 1,
                "output_tokens": 1, "duration_ms": 1, "total_cost_usd": 0.1, "cost_source": "native",
                "trajectory_path": None}

    monkeypatch.setattr(run, "_run_single_in_repo", fake_cell)
    return seen


# --- AC-1: prepare hook and task timeouts ---


def test_prepare_hook_replaces_repo_copy(bench, monkeypatch: pytest.MonkeyPatch) -> None:
    task = PreparedTask()
    monkeypatch.setitem(run.TASKS, "prepared_task", task)
    seen = _capture_cells(monkeypatch)

    @contextmanager
    def forbidden_agent_repo(*args, **kwargs):
        raise AssertionError("prepared task copied a REPOS fixture")
        yield

    def forbidden_clean(*args, **kwargs):
        raise AssertionError("prepared task cleaned a REPOS fixture")

    monkeypatch.setattr(run, "_agent_repo", forbidden_agent_repo)
    monkeypatch.setattr(run, "ensure_repo_clean", forbidden_clean)

    assert bench.main("--tasks", "prepared_task", *_ARGS) == 0
    assert seen == [task.prepared_into]
    assert not seen[0].exists()


def test_task_without_prepare_uses_agent_repo(bench, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen = _capture_cells(monkeypatch)
    copies: list[Path] = []

    @contextmanager
    def recording_agent_repo(repo_path: Path, hide_git: bool):
        copies.append(repo_path)
        workspace = tmp_path / "copy"
        workspace.mkdir()
        yield workspace

    monkeypatch.setattr(run, "_agent_repo", recording_agent_repo)

    assert bench.main("--tasks", "cell_a", *_ARGS) == 0
    assert copies == [run.SYNTHETIC_REPO]
    assert seen == [tmp_path / "copy"]


class _Timer:
    intervals: list[float] = []

    def __init__(self, interval: float, function) -> None:
        _Timer.intervals.append(interval)
        self.function = function

    def start(self) -> None:
        self.function()

    def cancel(self) -> None:
        return None


@pytest.mark.parametrize(("task", "expected"), [(SlowTask(), 1800), (StoreTask(), 600)])
def test_task_timeout_reaches_runner(bench, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                     task: StoreTask, expected: int) -> None:
    monkeypatch.setitem(run.TASKS, "timed_task", task)
    _Timer.intervals = []
    monkeypatch.setattr(run.threading, "Timer", _Timer)

    with pytest.raises(subprocess.TimeoutExpired) as streamed:
        run_cell(monkeypatch, "timed_task", tmp_path, tmp_path)
    assert _Timer.intervals == [expected]
    assert streamed.value.timeout == expected

    timeouts: list[float] = []

    def fake_run(cmd, **kwargs):
        timeouts.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(run.subprocess, "run", fake_run)
    with pytest.raises(subprocess.TimeoutExpired):
        run._run_single_in_repo("timed_task", "plain", "sonnet5", 0, tmp_path, stream_log_path=None)
    assert timeouts == [expected]


def test_timeout_message_names_task_timeout(bench, monkeypatch: pytest.MonkeyPatch,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setitem(run.TASKS, "timed_task", SlowTask())

    def timeout(_stream: Path) -> float:
        raise subprocess.TimeoutExpired(["claude"], 1800)

    bench.runner(timeout)
    assert bench.main("--tasks", "timed_task", *_ARGS) == 0
    assert "TIMEOUT (>1800s)" in capsys.readouterr().out


def test_external_fixture_timeouts(external_bench) -> None:
    external_bench.seed(SWE_GO, SWE_RS, FB_LV1, SWE_PY)

    assert [load(instance_id).timeout_s for instance_id in (SWE_GO, SWE_RS, FB_LV1, SWE_PY)] == [1800, 1800, 1200, 1200]


def test_task_timeout_in_cell_identity(bench, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(run.TASKS, "slow_task", SlowTask())
    monkeypatch.setitem(run.TASKS, "fast_task", StoreTask())

    slow = bench.identity("slow_task", "plain", 0)
    fast = bench.identity("fast_task", "plain", 0)

    assert slow["timeout_s"] == 1800 and fast["timeout_s"] == 600
    assert slow["run_key"] != fast["run_key"]


# --- AC-2: external identity ---


def _identity(task_name: str) -> dict:
    return run.cell_identity(task_name, "baseline", "sonnet5", 0, bare=False, reasoning_effort=None,
                             max_budget_usd=1.0, strict_file_tools=False)


def test_external_cell_identity_never_indexes_repos(external_bench, monkeypatch: pytest.MonkeyPatch) -> None:
    external_bench.seed(FB_LV1, SWE_GO, SWE_RS)
    probes: list[str] = []

    def no_repo_path(name: str) -> Path:
        raise AssertionError(f"indexed REPOS for {name}")

    def probe(argv: tuple[str, ...]) -> str:
        probes.append(argv[0])
        return f"{argv[0]} test-version"

    monkeypatch.setattr(run, "get_repo_path", no_repo_path)
    monkeypatch.setattr(run, "_probe_version", probe)
    expected = {FB_LV1: ["python3", "uv"], SWE_GO: ["go"], SWE_RS: ["rustc", "cargo"]}
    for instance_id, toolchains in expected.items():
        monkeypatch.setitem(run.TASKS, instance_id, load(instance_id))
        probes.clear()
        identity = _identity(instance_id)
        assert identity["env_fingerprint"] and identity["task_digest"]
        assert [name for name in probes if name in {"python3", "uv", "go", "rustc", "cargo", "node"}] == toolchains


def _changed_rows(external_bench) -> dict[str, tuple[dict, str]]:
    row = external_bench.row(SWE_RS)
    parent = git("rev-parse", f"{row['base_commit']}^", cwd=external_bench.upstream / "fixture/calcrs.git").strip()
    return {
        "gold_patch": ({**row, "patch": row["patch"] + "\n"}, external.SWEBENCH_ML_REVISION),
        "test_patch": ({**row, "test_patch": row["test_patch"] + "\n"}, external.SWEBENCH_ML_REVISION),
        "fail_to_pass": ({**row, "FAIL_TO_PASS": row["FAIL_TO_PASS"][:1]}, external.SWEBENCH_ML_REVISION),
        "pass_to_pass": ({**row, "PASS_TO_PASS": []}, external.SWEBENCH_ML_REVISION),
        "base_commit": ({**row, "base_commit": parent}, external.SWEBENCH_ML_REVISION),
        "data_revision": (row, "0" * 40),
    }


@pytest.mark.parametrize("changed", ["gold_patch", "test_patch", "fail_to_pass", "pass_to_pass", "base_commit",
                                     "data_revision", "toolchain", "lockfile"])
def test_identity_inputs_change_run_key(external_bench, monkeypatch: pytest.MonkeyPatch, changed: str) -> None:
    external_bench.seed(SWE_RS)
    base = external.swebench_ml.SweBenchTask(external_bench.row(SWE_RS), external.SWEBENCH_ML_REVISION)
    monkeypatch.setitem(run.TASKS, SWE_RS, base)
    before = _identity(SWE_RS)["run_key"]

    if changed == "toolchain":
        monkeypatch.setattr(run, "_probe_version", lambda argv: f"{argv[0]} 9.9.9")
    elif changed == "lockfile":
        monkeypatch.setattr(base, "base_file_hashes", lambda names: {"Cargo.lock": "f" * 64})
    else:
        row, revision = _changed_rows(external_bench)[changed]
        monkeypatch.setitem(run.TASKS, SWE_RS, external.swebench_ml.SweBenchTask(row, revision))

    assert _identity(SWE_RS)["run_key"] != before


# --- AC-3: grading rows ---


def _partial_fix_workdir(external_bench, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    external_bench.seed(SWE_PY)
    task = load(SWE_PY)
    monkeypatch.setitem(run.TASKS, SWE_PY, task)
    workdir = tmp_path / "workdir"
    task.prepare(workdir)
    first_hunk = external.patches.tampered(task.gold_patch)
    subprocess.run(["git", "apply", "-"], cwd=workdir, input=first_hunk, text=True, check=True)
    return workdir


def test_row_carries_resolved_and_pass_rate(bench, external_bench, monkeypatch: pytest.MonkeyPatch,
                                            tmp_path: Path) -> None:
    workdir = _partial_fix_workdir(external_bench, tmp_path, monkeypatch)

    row = run_cell(monkeypatch, SWE_PY, workdir, tmp_path)

    assert row["correct"] is False
    assert (row["f2p_passed"], row["f2p_total"], row["p2p_passed"], row["p2p_total"]) == (1, 2, 1, 1)
    assert row["pass_rate"] == pytest.approx(2 / 3)


def test_grader_material_only_in_reason(bench, external_bench, monkeypatch: pytest.MonkeyPatch,
                                        tmp_path: Path) -> None:
    workdir = _partial_fix_workdir(external_bench, tmp_path, monkeypatch)
    secret_lines = ["return left // right\n", "assert div(7, 2) == 3", "assert mul(2, 3) == 6"]

    row = run_cell(monkeypatch, SWE_PY, workdir, tmp_path)

    assert any(line in row["correctness_reason"] for line in secret_lines[1:])
    for field, value in row.items():
        if field == "correctness_reason":
            continue
        rendered = json.dumps(value)
        assert not [line for line in secret_lines if json.dumps(line)[1:-1] in rendered], field


# --- AC-4 / AC-5: guard and --tasks resolution ---


def test_guard_uses_admit(bench, external_bench, monkeypatch: pytest.MonkeyPatch,
                          capsys: pytest.CaptureFixture[str]) -> None:
    external_bench.seed(SWE_PY)
    monkeypatch.setitem(run.TASKS, SWE_PY, load(SWE_PY))
    bench.runner(lambda _stream: 0.1)
    monkeypatch.setattr(external.preflight, "admit", lambda instance_id: _verdict(instance_id, False, "tampered_resolved"))

    assert bench.main("--tasks", SWE_PY, *_ARGS) != 0
    assert bench.calls == []
    error = capsys.readouterr().err
    assert SWE_PY in error and "tampered_resolved" in error

    monkeypatch.setattr(external.preflight, "admit", lambda instance_id: _verdict(instance_id, True, "admitted"))
    assert bench.main("--tasks", SWE_PY, *_ARGS) == 0
    assert bench.calls == [(SWE_PY, "plain", 0)]


def test_tasks_flag_registers_and_prepares_external(bench, external_bench, monkeypatch: pytest.MonkeyPatch) -> None:
    external_bench.seed(SWE_PY)
    monkeypatch.setattr(run, "TASKS", dict(run.TASKS))
    monkeypatch.setattr(external.preflight, "admit", lambda instance_id: _verdict(instance_id, True, "admitted"))
    checked: list[bool] = []

    def prepared_cell(task_name, mode_name, model_name, repetition, repo_path, **kwargs):
        checked.append((Path(repo_path) / "calc" / "ops.py").is_file() and (Path(repo_path) / ".git").is_dir())
        return {"task": task_name, "mode": mode_name, "model": run.MODELS[model_name], "repetition": repetition,
                "correct": False, "correctness_reason": "stub", "num_turns": 1, "context_tokens": 1,
                "output_tokens": 1, "duration_ms": 1, "total_cost_usd": 0.1, "cost_source": "native",
                "trajectory_path": None}

    monkeypatch.setattr(run, "_run_single_in_repo", prepared_cell)

    assert bench.main("--tasks", SWE_PY, *_ARGS) == 0
    assert isinstance(run.TASKS[SWE_PY], external.ExternalTask)
    assert checked == [True]
    assert not [row for row in bench.output_rows() if row.get("error")]
    assert SWE_PY not in run.select_tasks("all")
    assert run.select_tasks(SWE_PY) == [SWE_PY]


def test_unadmitted_task_name_reports_reason(bench, external_bench, monkeypatch: pytest.MonkeyPatch,
                                             capsys: pytest.CaptureFixture[str]) -> None:
    external_bench.seed(SWE_PY)
    monkeypatch.setattr(run, "TASKS", dict(run.TASKS))
    monkeypatch.setattr(external.preflight, "admit", lambda instance_id: _verdict(instance_id, False, "empty_resolved"))
    bench.runner(lambda _stream: 0.1)

    assert bench.main("--tasks", SWE_PY, *_ARGS) != 0
    assert "empty_resolved" in capsys.readouterr().err
    assert bench.calls == []


# --- AC-6: contaminated on every row ---


def _benchmark_read() -> dict:
    return {"file_path": str(config.BENCHMARK_DIR / "tasks" / "ripgrep_tasks.py")}


def test_contaminated_row_keeps_grader_verdict(bench, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    row = run_cell(monkeypatch, "cell_a", tmp_path, tmp_path, claude_stream("Read", _benchmark_read()))

    assert row["correct"] is True
    assert row["contaminated"] is True
    assert [hit["reason"] for hit in row["contamination_hits"]] == ["benchmark_tree"]
    assert "graded_correct" not in row


def test_every_row_has_contaminated_bool(bench, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clean = run_cell(monkeypatch, "cell_a", tmp_path, tmp_path)
    assert clean["contaminated"] is False and clean["contamination_hits"] == []
    assert clean["correct"] is True

    def failing_with_benchmark_read(stream: Path) -> float:
        stream.parent.mkdir(parents=True, exist_ok=True)
        stream.write_text(claude_stream("Read", _benchmark_read()))
        raise RuntimeError("claude -p failed")

    def failing_without_stream(_stream: Path) -> float:
        raise RuntimeError("claude -p failed before streaming")

    bench.runner(failing_with_benchmark_read)
    assert bench.main("--tasks", "cell_a", *_ARGS) == 0
    [failed] = bench.output_rows()
    assert failed["contaminated"] is True and failed["correct"] is False
    assert "benchmark_tree" in [hit["reason"] for hit in failed["contamination_hits"]]

    bench.runner(failing_without_stream)
    assert bench.main("--tasks", "cell_b", *_ARGS) == 0
    [unscanned] = bench.output_rows()
    assert unscanned["trajectory_path"] is None
    assert unscanned["contaminated"] is True
    assert [hit["reason"] for hit in unscanned["contamination_hits"]] == ["unscanned"]

    bench.runner(lambda _stream: 0.1)
    assert bench.main("--tasks", "cell_c", *_ARGS) == 0
    [no_sidecar] = bench.output_rows()
    assert no_sidecar["correct"] is True and no_sidecar["contaminated"] is True

    stored = bench.row("cell_a", "plain", 0, trajectory_path=None)
    bench.seed(stored)
    assert "contaminated" not in stored
    assert bench.main("--tasks", "cell_a", *_ARGS) == 0
    [reused] = bench.output_rows()
    assert reused["reused"] is True
    assert isinstance(reused["contaminated"], bool)
