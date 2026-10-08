"""Shared scaffolding for tests that drive ``run.main`` against a result store."""

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import baselines
import run
from config import DEFAULT_MAX_BUDGET_USD, ModeConfig
from tasks.base import GroundTruth, TaskSource

STREAMS = Path(__file__).parent / "fixtures" / "streams"

# A runner behavior receives the cell's tee path and returns the cell cost, or raises.
Behavior = Callable[[Path], float]


@dataclass
class StoreTask:
    repo: str = "synthetic"
    prompt: str = "Name the dispatcher."
    capability: str = "trace"
    hide_git: bool = False
    ground_truth: GroundTruth = field(default_factory=lambda: GroundTruth(required_strings=["handler_3"]))
    source: TaskSource = TaskSource(origin="fixture", license="MIT", commit_or_tag="test-pin", transformation="test-only")

    def check_correctness(self, result_text: str, repo_path: str) -> tuple[bool, str]:
        return True, "expected answer"


@dataclass
class Bench:
    """A hermetic ``run.main`` harness with a stubbed runner and identity probes."""

    monkeypatch: pytest.MonkeyPatch
    results_dir: Path
    cli: str = "2.1.0"
    env: str = "env-fingerprint-a"
    calls: list[tuple[str, str, int]] = field(default_factory=list)

    @property
    def store_path(self) -> Path:
        return self.results_dir / baselines.STORE_FILENAME

    def identity(self, task: str, mode: str, repetition: int, model: str = "sonnet5") -> dict:
        return run.cell_identity(
            task, mode, model, repetition,
            bare=False, reasoning_effort=None,
            max_budget_usd=DEFAULT_MAX_BUDGET_USD, strict_file_tools=False,
        )

    def row(self, task: str, mode: str, repetition: int, *, cost: float = 0.1, **fields: object) -> dict:
        return {
            "task": task, "mode": mode, "model": run.MODELS["sonnet5"], "model_alias": "sonnet5",
            "repetition": repetition, "correct": True, "correctness_reason": "stored",
            "num_turns": 1, "context_tokens": 1, "output_tokens": 1, "duration_ms": 1,
            "total_cost_usd": cost, "cost_source": "native", "reused": False,
            **self.identity(task, mode, repetition), **fields,
        }

    def seed(self, row: dict) -> None:
        baselines.store(row, path=self.store_path)

    def runner(self, behavior: Behavior) -> None:
        def fake_run_single(task_name, mode_name, model_name, repetition, **kwargs):
            self.calls.append((task_name, mode_name, repetition))
            cost = behavior(kwargs["stream_log_path"])
            return {
                "task": task_name, "mode": mode_name, "model": run.MODELS[model_name],
                "model_alias": model_name, "repetition": repetition, "correct": True,
                "correctness_reason": "fresh", "num_turns": 1, "context_tokens": 1,
                "output_tokens": 1, "duration_ms": 1, "total_cost_usd": cost,
                "cost_source": "native",
            }

        self.monkeypatch.setattr(run, "run_single", fake_run_single)

    def main(self, *argv: str) -> int:
        self.monkeypatch.setattr(sys, "argv", ["run.py", *argv])
        try:
            run.main()
        except SystemExit as error:
            return error.code if isinstance(error.code, int) else 1
        return 0

    def output_rows(self) -> list[dict]:
        newest = max(self.results_dir.glob("benchmark_*.jsonl"), key=lambda path: path.stat().st_mtime_ns)
        return [json.loads(line) for line in newest.read_text().splitlines()]

    def stored_rows(self) -> list[dict]:
        return baselines.load_rows(self.store_path)


@pytest.fixture(autouse=True)
def hermetic_version_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep host toolchain and agent CLI probes out of every test's subprocess stubs."""
    monkeypatch.setattr(run, "_probe_version", lambda argv: f"{argv[0]} test-version")
    monkeypatch.setattr(run, "_run_probe", lambda argv: f"{argv[0]} test-version")


@pytest.fixture
def bench(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Bench:
    source = tmp_path / "synthetic"
    source.mkdir()
    harness = Bench(monkeypatch=monkeypatch, results_dir=tmp_path / "results")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(run, "SYNTHETIC_REPO", source)
    monkeypatch.setattr(run, "RESULTS_DIR", harness.results_dir)
    monkeypatch.setattr(run, "reset_repo", lambda: None)
    monkeypatch.setattr(run, "cli_version", lambda _runner, **_kwargs: harness.cli)
    monkeypatch.setattr(run, "env_fingerprint", lambda _repo: harness.env)
    for name in ("cell_a", "cell_b", "cell_c"):
        monkeypatch.setitem(run.TASKS, name, StoreTask())
    monkeypatch.setitem(run.MODES, "plain", ModeConfig(name="plain", tools=["Read"], mcp_config_path=None, description="test arm"))
    return harness
