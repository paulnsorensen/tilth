"""Evolve with ``--preamble``: the candidate arm runs on its own commit's preamble, and the preamble evolves."""

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import run
from evolve import attribution, engine
from evolve import candidate as candidates
from evolve.calls import PaidCalls
from evolve.materialize import ApplyRejected
from evolve.proposer import Proposer
from evolve_support import build, git, make_world, scripted_engine, seed_candidate, seed_files

PREAMBLE = "prompts/preamble.md"
SEED_PREAMBLE = "# tilth agent preamble\n\n## Tools\n\nSEED-PREAMBLE\n\n## Safety\n\n## Verification\n"
CHILD_PREAMBLE = SEED_PREAMBLE.replace("SEED-PREAMBLE", "CHILD-PREAMBLE batch every read")


def preamble_files() -> dict[str, str]:
    return {**seed_files(), PREAMBLE: SEED_PREAMBLE}


def preamble_seed() -> dict[str, str]:
    return {**seed_candidate(preamble_files()), PREAMBLE: SEED_PREAMBLE}


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    return make_world(monkeypatch, tmp_path, preamble_files())


def record_prompts(world, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str | None, str]]:
    """Wrap the stub runner: each cell also records (mode, served sha, its real Claude --system-prompt)."""
    seen: list[tuple[str, str | None, str]] = []

    def recording(task_name, mode_name, model_name, repetition, **kwargs):
        mode = run.MODES[mode_name]
        argv, _config = run._runner_command(
            run.TASKS[task_name], mode, mode_name, model_name, world.tmp, bare=kwargs["bare"],
            reasoning_effort=kwargs.get("reasoning_effort"), max_budget_usd=kwargs["max_budget_usd"],
            strict_file_tools=kwargs.get("strict_file_tools", False), skill_paths=[])
        seen.append((mode_name, mode.git_sha, argv[argv.index("--system-prompt") + 1]))
        return world.fake_run_single(task_name, mode_name, model_name, repetition, **kwargs)

    monkeypatch.setattr(run, "run_single", recording)
    return seen


def test_a_preamble_run_serves_each_candidate_its_own_preamble(world, monkeypatch: pytest.MonkeyPatch) -> None:
    child = {**preamble_seed(), PREAMBLE: CHILD_PREAMBLE}
    fake, _seen = scripted_engine([child])
    monkeypatch.setattr(engine, "optimize_anything", fake)
    seen = record_prompts(world, monkeypatch)
    hints: list[dict] = []
    real = attribution.summarize
    monkeypatch.setattr(attribution, "summarize", lambda *args, **kwargs: hints.append(kwargs) or real(*args, **kwargs))
    evo = build(world, "--preamble")

    evo.run()

    child_sha = evo.materializer.materialize(child)
    assert git("show", f"{child_sha}:{PREAMBLE}", cwd=world.repo) == CHILD_PREAMBLE
    assert git("show", f"{world.seed_sha}:{PREAMBLE}", cwd=world.repo) == SEED_PREAMBLE
    prompts = {(mode, sha): prompt.split("\nYour current working directory")[0] for mode, sha, prompt in seen}
    assert prompts[("tilth_forced", world.seed_sha)] == SEED_PREAMBLE
    assert prompts[("tilth_forced", child_sha)] == CHILD_PREAMBLE
    assert {prompt for (mode, _sha), prompt in prompts.items() if mode == "baseline"} == {run.SYSTEM_PROMPT}
    assert hints and all(kwargs == {"hints": attribution.PREAMBLE_HINTS} for kwargs in hints)
    stored = [row for row in world.stored() if row["mode"] == "tilth_forced"]
    assert {row["preamble_sha256"] for row in stored} == {
        hashlib.sha256(text.encode()).hexdigest() for text in (SEED_PREAMBLE, CHILD_PREAMBLE)}


def test_the_seed_carries_the_preamble_only_in_a_preamble_run(world) -> None:
    assert candidates.PREAMBLE == PREAMBLE
    assert PREAMBLE in build(world, "--preamble").seed
    assert PREAMBLE not in build(world).seed


def test_a_preamble_run_refuses_a_seed_without_a_preamble(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = make_world(monkeypatch, tmp_path, seed_files())

    assert build(world, "--preamble") == 2


def test_a_preamble_edit_outside_a_preamble_run_is_refused(world) -> None:
    evo = build(world)

    with pytest.raises(ApplyRejected, match=r"extra \['prompts/preamble.md'\]"):
        evo.materializer.materialize({**evo.seed, PREAMBLE: CHILD_PREAMBLE})


def test_a_preamble_candidate_must_keep_every_seed_component(world) -> None:
    evo = build(world, "--preamble")
    without = {name: text for name, text in evo.seed.items() if name != PREAMBLE}

    with pytest.raises(ApplyRejected, match=r"missing \['prompts/preamble.md'\]"):
        evo.materializer.materialize({**without, "prompts/tools/read.md": "changed\n"})


def test_the_preamble_cap_is_4096_bytes(world) -> None:
    evo = build(world, "--preamble")
    fits = SEED_PREAMBLE + "x" * (4096 - len(SEED_PREAMBLE))

    assert evo.materializer.materialize({**evo.seed, PREAMBLE: fits})
    with pytest.raises(ApplyRejected, match=f"{PREAMBLE} is 4097 bytes; .* at most 4096 bytes"):
        evo.materializer.materialize({**evo.seed, PREAMBLE: fits + "x"})


def test_the_proposer_export_holds_the_candidate_preamble(world, tmp_path: Path) -> None:
    proposer = Proposer(world.repo, world.seed_sha, PaidCalls.__new__(PaidCalls), "sonnet", forbidden=[],
                        log=lambda _line: None)

    with proposer.export({**preamble_seed(), PREAMBLE: CHILD_PREAMBLE}) as root:
        assert (root / PREAMBLE).read_text() == CHILD_PREAMBLE
    with proposer.export(seed_candidate(preamble_files())) as root:
        assert (root / PREAMBLE).read_text() == SEED_PREAMBLE


def test_reflection_on_the_preamble_states_its_role_and_cap() -> None:
    preamble = engine.reflection_prompt(SEED_PREAMBLE, [], max_bytes=4096, name=PREAMBLE)
    mcp = engine.reflection_prompt("text", [], max_bytes=2048, name="prompts/mcp.md")

    assert engine.PREAMBLE_NOTE in preamble and "at most 4096 UTF-8 bytes" in preamble
    assert engine.PREAMBLE_NOTE not in mcp and "at most 2048 UTF-8 bytes" in mcp
    for heading in ("## Tools", "## Safety", "## Verification"):
        assert heading in engine.PREAMBLE_NOTE


def test_read_breadth_names_the_preamble_in_a_preamble_run() -> None:
    cand = {("files",): 5.0, ("chars", "tilth_read"): 1000.0, ("calls", "tilth_read"): 2.0}
    base = {("files",): 1.0}

    assert [d["component_hint"] for d in attribution._dominant(cand, base, attribution.HINTS)] == ["prompts/mcp.md"]
    assert [d["component_hint"] for d in attribution._dominant(cand, base, attribution.PREAMBLE_HINTS)] == [PREAMBLE]
