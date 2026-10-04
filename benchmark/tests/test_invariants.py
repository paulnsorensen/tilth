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
