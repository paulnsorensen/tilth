"""Strict file tools by default in evolve: the seed-hook preflight, the candidate hook gate, and strict cells.

Hook binaries are fake scripts that really run as subprocesses, so the frozen corpus is replayed for real.
No test reaches a model, a network, or cargo, except the demo, which reuses a built ``target/*/tilth``.
"""

import json
import re
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import run
from config import REPO_ROOT
from evolve import cli, engine, hookgate
from evolve.materialize import ApplyRejected, Materializer
from evolve_support import (
    CHEAP, DEV, TEST, build, child, evolve_args, git, log_text, make_world, mcp, scripted_engine, seed_candidate,
    seed_files,
)

CORPUS = json.loads(hookgate.CORPUS_PATH.read_text())
WEAK_SEED = mcp(dev_a="0", dev_b="0", cheap_a="1", test_a="0")
# Rows the harness's own two-command probe does not use, so a flipped verdict reaches the gate, not the probe.
DENY_ROW = next(row for row in CORPUS["must_deny"] if row["command"] not in {run._PROBE_DENY, run._PROBE_ALLOW})
ALLOW_ROW = next(row for row in CORPUS["must_allow"] if row["command"] not in {run._PROBE_DENY, run._PROBE_ALLOW})

_FAKE_HOOK = """#!{python}
import json, sys
cfg = json.load(open({config!r}))
hint = sys.argv[sys.argv.index("--hint") + 1]
command = json.load(sys.stdin)["tool_input"]["command"]
deny = command in cfg["deny"]
if command in cfg["flip"].get(hint, []):
    deny = not deny
if deny:
    if not cfg["silent"]:
        sys.stderr.write("Use tilth_read" if hint == "tilth" else "Use the Read tool")
    sys.exit(2)
sys.exit(0)
"""


def fake_hook(tmp: Path, name: str, *, flip: dict[str, list[str]] | None = None, silent: bool = False) -> Path:
    """A hook that is faithful to the corpus, except it flips the verdict of ``flip[hint]`` commands."""
    config = tmp / f"{name}.json"
    config.write_text(json.dumps({"deny": [row["command"] for row in CORPUS["must_deny"]], "flip": flip or {},
                                  "silent": silent}))
    path = tmp / name
    path.write_text(_FAKE_HOOK.format(python=sys.executable, config=str(config)))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def strict_world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, hooks: dict[str, Path] | None = None,
                 seed_hook: Path | None = None, seed_mcp: str | None = None):
    """A world whose builds return fake hook binaries: the seed's ``seed_hook``, a candidate's by its ``tag=`` mark."""
    files = seed_files(seed_mcp) if seed_mcp else seed_files()
    world = make_world(monkeypatch, tmp_path, {**files, "src/hook/rules.rs": "pub fn deny() {}\n"})
    seed_hook = seed_hook or fake_hook(tmp_path, "seed-hook")
    hooks = hooks or {}

    def build_candidate(sha: str, repo: Path | None = None) -> run.CandidateBuild:
        if sha == world.seed_sha:
            path = seed_hook
        else:
            text = git("show", f"{sha}:prompts/mcp.md", cwd=world.repo)
            path = hooks[re.search(r"tag=(\w+)", text).group(1)]
        return run.CandidateBuild(git_sha=sha, binary_path=str(path), binary_sha256=f"sha-{path.name}")

    monkeypatch.setattr(run, "build_candidate", build_candidate)
    monkeypatch.setattr(run, "_hook_source_key", lambda _sha, _repo=None: "tree:lock")
    world.seed_hook = seed_hook
    return world


def winner(tag: str) -> dict[str, str]:
    return child(seed_candidate(), dev_a="1", dev_b="1", cheap_a="1", test_a="1", tag=tag)


def ready(world, *extra: str):
    evo = build(world, *extra, strict=True)
    assert evo.preflight() is None
    assert evo.buy_baselines() is None
    return evo


def patch_for(world, changes: dict[str, str]) -> str:
    scratch = world.tmp / "scratch"
    git("worktree", "add", "-q", "--detach", str(scratch), world.seed_sha, cwd=world.repo)
    for name, text in changes.items():
        (scratch / name).parent.mkdir(parents=True, exist_ok=True)
        (scratch / name).write_text(text)
    git("add", "-A", cwd=scratch)
    diff = git("diff", "--cached", world.seed_sha, cwd=scratch)
    git("worktree", "remove", "--force", str(scratch), cwd=world.repo)
    return diff


# --- AC-12: strict is the default ---


@pytest.mark.parametrize(("flag", "expected"), [(None, True), ("--no-strict-file-tools", False)])
def test_cli_strict_flag_reaches_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, flag, expected) -> None:
    world = strict_world(monkeypatch, tmp_path)
    args = evolve_args(world, strict=True) + ([flag] if flag else [])

    evo = cli.build(args, **{"judge_factory": lambda _l, _f: world.judge, "pr_client": world.pr,
                             "just_check": world.just_check, "spawn": world.spawn})

    assert evo.settings.strict_file_tools is expected


@pytest.mark.parametrize("strict", [True, False])
def test_strict_settings_reach_every_run_call(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, strict: bool) -> None:
    world = strict_world(monkeypatch, tmp_path)
    plans, identities, drifts = [], [], []
    originals = (run.run_plan, run.planned_identity, run.check_baseline_drift)

    def record(into, original):
        def wrapper(*args, **kwargs):
            into.append((args, kwargs))
            return original(*args, **kwargs)
        return wrapper

    monkeypatch.setattr(run, "run_plan", record(plans, originals[0]))
    monkeypatch.setattr(run, "planned_identity", record(identities, originals[1]))
    monkeypatch.setattr(run, "check_baseline_drift", record(drifts, originals[2]))
    evo = build(world, strict=strict)
    assert evo.preflight() is None
    assert evo.buy_baselines() is None
    evo.finalist_test_cost()

    assert plans and identities and len(drifts) == 1
    drift_cells = {cell[0] for cell in drifts[0][0][0]}
    assert drift_cells == {*CHEAP, *DEV, *TEST}
    for _args, kwargs in [*plans, *identities, *drifts]:
        assert kwargs.get("strict_file_tools", False) is strict
        assert kwargs.get("hook_sha") == (world.seed_sha if strict else None)
    for _args, kwargs in drifts:
        assert kwargs.get("hook_repo") == (world.repo if strict else None)


# --- AC-13: the seed hook preflight ---


@pytest.mark.parametrize("fault", [
    pytest.param({"native": [DENY_ROW["command"]]}, id="allows-a-must-deny-row-natively"),
    pytest.param({"tilth": [DENY_ROW["command"]]}, id="allows-a-must-deny-row-with-tilth-hint"),
    pytest.param({"native": [ALLOW_ROW["command"]]}, id="denies-a-must-allow-row-natively"),
    pytest.param({"tilth": [ALLOW_ROW["command"]]}, id="denies-a-must-allow-row-with-tilth-hint"),
])
def test_unfaithful_seed_hook_refuses_before_any_paid_call(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys,
                                                          fault) -> None:
    seed_hook = fake_hook(tmp_path, "bad-seed-hook", flip=fault)
    world = strict_world(monkeypatch, tmp_path, seed_hook=seed_hook)
    evo = build(world, strict=True)

    assert evo.preflight() == 2

    [command] = next(iter(fault.values()))
    assert command in capsys.readouterr().err and command in log_text(world)
    assert world.calls == [] and world.spawned == [] and world.judge.labelled == []
    assert not world.stored()


def test_unfaithful_seed_hook_refuses_the_whole_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seed_hook = fake_hook(tmp_path, "bad-seed-hook", flip={"native": [DENY_ROW["command"]]})
    world = strict_world(monkeypatch, tmp_path, seed_hook=seed_hook)

    assert cli.main(evolve_args(world, strict=True), judge_factory=lambda _l, _f: world.judge, pr_client=world.pr,
                    just_check=world.just_check, spawn=world.spawn) == 2

    assert world.calls == [] and world.spawned == [] and world.judge.labelled == []


def test_faithful_seed_hook_passes_the_preflight(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = strict_world(monkeypatch, tmp_path)
    evo = build(world, strict=True)

    assert evo.preflight() is None
    assert "seed hook passes the frozen corpus" in log_text(world)


def test_non_strict_run_skips_the_seed_hook_preflight(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seed_hook = fake_hook(tmp_path, "bad-seed-hook", flip={"native": [DENY_ROW["command"]]})
    world = strict_world(monkeypatch, tmp_path, seed_hook=seed_hook)

    assert build(world).preflight() is None


# --- AC-14: a candidate may change hook source, and must still pass the hook gate ---


def test_hook_source_patch_materializes_but_the_corpus_patch_is_refused(monkeypatch: pytest.MonkeyPatch,
                                                                        tmp_path: Path) -> None:
    world = strict_world(monkeypatch, tmp_path)
    materializer = Materializer(world.repo, world.seed_sha, "run1", world.tmp / "work")

    sha = materializer.materialize({**seed_candidate(), "src_patch": patch_for(world, {
        "src/hook/rules.rs": "pub fn deny() {\n    // reworded\n}\n"})})
    assert git("show", f"{sha}:src/hook/rules.rs", cwd=world.repo).startswith("pub fn deny() {\n    // reworded")
    with pytest.raises(ApplyRejected, match="hook_corpus.json"):
        materializer.materialize({**seed_candidate(), "src_patch": patch_for(world, {
            "benchmark/hook_corpus.json": "{}\n"})})


@pytest.mark.parametrize("fault", [
    pytest.param({"native": [DENY_ROW["command"]]}, id="native-allows-must-deny"),
    pytest.param({"tilth": [DENY_ROW["command"]]}, id="tilth-allows-must-deny"),
    pytest.param({"native": [ALLOW_ROW["command"]]}, id="native-denies-must-allow"),
    pytest.param({"tilth": [ALLOW_ROW["command"]]}, id="tilth-denies-must-allow"),
])
def test_unfaithful_candidate_hook_fails_the_gate_with_no_cells(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                               fault) -> None:
    bad = fake_hook(tmp_path, "bad-candidate-hook", flip=fault)
    world = strict_world(monkeypatch, tmp_path, hooks={"bad": bad})
    evo = ready(world)
    plans = []
    original = run.run_plan
    monkeypatch.setattr(run, "run_plan", lambda *a, **k: plans.append(k.get("candidate_sha")) or original(*a, **k))
    candidate = winner("bad")

    score, info = evo.evaluate(candidate, "dev_a")

    [command] = next(iter(fault.values()))
    result = next(r for r in evo.results.values() if r.candidate == candidate)
    assert result.stage == "hook gate" and score == 0
    assert info["stage"] == "hook gate" and command in info["tail"]
    assert f"--hint {next(iter(fault))}" in info["tail"]
    assert result.sha not in plans and all(call[3] != result.sha for call in world.calls)
    assert f"candidate {result.cid[:12]}: failed at hook gate" in log_text(world)


def test_hook_probe_failure_is_a_hook_gate_stage_not_a_build_failure(monkeypatch: pytest.MonkeyPatch,
                                                                     tmp_path: Path) -> None:
    mute = fake_hook(tmp_path, "mute-hook", silent=True)
    world = strict_world(monkeypatch, tmp_path, hooks={"mute": mute})
    evo = ready(world)

    score, info = evo.evaluate(winner("mute"), "dev_a")

    assert score == 0 and info["stage"] == "hook gate" and "hint text" in info["tail"]
    assert all(call[3] == world.seed_sha for call in world.calls if call[1] == "tilth_forced")


# --- AC-15: a passing candidate runs strict cells ---


def test_passing_candidate_runs_its_own_hook_and_the_baseline_runs_the_seed_hook(monkeypatch: pytest.MonkeyPatch,
                                                                                tmp_path: Path) -> None:
    good = fake_hook(tmp_path, "good-candidate-hook")
    world = strict_world(monkeypatch, tmp_path, hooks={"good": good})
    seen = []
    original = run.run_single

    def recording(task, mode, model, rep, **kwargs):
        binary, hint = run._strict_hook_spec(run.MODES[mode], run.build_candidate(kwargs["hook_sha"]).binary_path)
        seen.append((mode, run.MODES[mode].git_sha, kwargs["strict_file_tools"], kwargs["hook_sha"], binary, hint))
        return original(task, mode, model, rep, **kwargs)

    evo = ready(world)
    monkeypatch.setattr(run, "run_single", recording)
    candidate = winner("good")

    score, info = evo.evaluate(candidate, "dev_a")

    result = next(r for r in evo.results.values() if r.candidate == candidate)
    assert result.stage is None and score == 1.0
    candidate_cells = [cell for cell in seen if cell[1] == result.sha]
    assert candidate_cells and {cell[0] for cell in candidate_cells} == {"tilth_forced"}
    for _mode, _sha, strict, hook_sha, binary, hint in candidate_cells:
        assert (strict, hook_sha, binary, hint) == (True, world.seed_sha, str(good), "tilth")
    baseline_cells = [cell for cell in seen if cell[0] == "baseline"]
    for _mode, _sha, strict, hook_sha, binary, hint in baseline_cells:
        assert (strict, hook_sha, binary, hint) == (True, world.seed_sha, str(world.seed_hook), "native")


# --- offline demo: a real built hook, one dropped rule, one reworded hint ---


def _built_tilth() -> Path | None:
    return next((path for path in (REPO_ROOT / "target" / "debug" / "tilth", REPO_ROOT / "target" / "release" / "tilth")
                 if path.is_file()), None)


_DROP_CAT = """#!{python}
import re, subprocess, sys
event = sys.stdin.read()
if re.search(r'"command": *"\\s*cat\\b', event):
    sys.exit(0)
sys.exit(subprocess.run([{real!r}, *sys.argv[1:]], input=event, text=True).returncode)
"""


def test_demo_dropped_cat_rule_fails_the_gate_and_a_reworded_hint_runs_strict_cells(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    real = _built_tilth()
    if real is None:
        pytest.skip("no built tilth in target/")
    dropped = tmp_path / "tilth-without-cat-rule"
    dropped.write_text(_DROP_CAT.format(python=sys.executable, real=str(real)))
    dropped.chmod(dropped.stat().st_mode | stat.S_IXUSR)
    assert hookgate.corpus_failures(str(real)) == [] and hookgate.corpus_failures(str(dropped))
    world = strict_world(monkeypatch, tmp_path, hooks={"dropcat": dropped, "reword": real}, seed_hook=real)
    fake, _seen = scripted_engine([winner("dropcat"), winner("reword")])
    monkeypatch.setattr(engine, "optimize_anything", fake)
    strict_cells = []
    original = run.run_single
    monkeypatch.setattr(run, "run_single", lambda *a, **k: strict_cells.append(
        (a[1], run.MODES[a[1]].git_sha, k["strict_file_tools"], k["hook_sha"])) or original(*a, **k))
    evo = build(world, strict=True)

    assert evo.run() == 0

    by_tag = {re.search(r"tag=(\w+)", r.candidate["prompts/mcp.md"]).group(1): r
              for r in evo.results.values() if "tag=" in r.candidate["prompts/mcp.md"]}
    assert by_tag["dropcat"].stage == "hook gate" and "cat" in by_tag["dropcat"].tail
    assert by_tag["reword"].stage is None and by_tag["reword"].scores
    assert not any(cell[1] == by_tag["dropcat"].sha for cell in strict_cells)
    reworded = [cell for cell in strict_cells if cell[1] == by_tag["reword"].sha]
    assert reworded and all(cell[2:] == (True, world.seed_sha) for cell in strict_cells)


# --- press round 1: loosened cells, preflight faults, bounded hooks, strict slots ---


def loosen_candidate_cells(monkeypatch: pytest.MonkeyPatch, world, tasks: set[str]) -> list[tuple[str, str]]:
    """Make every candidate-arm cell of ``tasks`` fail as hook_loosened, as the real harness does on a denied Bash call."""
    hit: list[tuple[str, str]] = []
    original = run.run_single

    def loosened(task, mode, model, rep, **kwargs):
        sha = run.MODES[mode].git_sha
        if mode == "tilth_forced" and sha != world.seed_sha and task in tasks:
            hit.append((task, sha))
            raise run.HookLoosenedError(f"hook_loosened: strict cell ran a Bash command the hook denies: cat {task}")
        return original(task, mode, model, rep, **kwargs)

    monkeypatch.setattr(run, "run_single", loosened)
    return hit


def test_a_loosened_dev_cell_scores_zero_and_the_candidate_keeps_running(monkeypatch: pytest.MonkeyPatch,
                                                                       tmp_path: Path) -> None:
    good = fake_hook(tmp_path, "good-candidate-hook")
    world = strict_world(monkeypatch, tmp_path, hooks={"good": good})
    evo = ready(world)
    hit = loosen_candidate_cells(monkeypatch, world, {"dev_a"})
    candidate = winner("good")

    loosened_score, _ = evo.evaluate(candidate, "dev_a")
    clean_score, _ = evo.evaluate(candidate, "dev_b")

    assert hit and loosened_score == 0.0
    assert clean_score == 1.0


def test_a_loosened_cheap_cell_does_not_score_as_correct(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    good = fake_hook(tmp_path, "good-candidate-hook")
    world = strict_world(monkeypatch, tmp_path, hooks={"good": good})
    evo = ready(world)
    loosen_candidate_cells(monkeypatch, world, {"cheap_a", "dev_a", "dev_b"})
    candidate = winner("good")

    score, _ = evo.evaluate(candidate, "dev_a")

    result = next(r for r in evo.results.values() if r.candidate == candidate)
    assert score == 0.0 and not result.accepted


def test_control_a_clean_strict_winner_opens_the_draft_pr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    good = fake_hook(tmp_path, "good-candidate-hook")
    world = strict_world(monkeypatch, tmp_path, hooks={"good": good}, seed_mcp=WEAK_SEED)
    fake, _seen = scripted_engine([winner("good")])
    monkeypatch.setattr(engine, "optimize_anything", fake)

    build(world, strict=True).run()

    assert len(world.pr.creates) == 1


def test_finish_with_a_loosened_winner_test_cell_opens_no_pr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    good = fake_hook(tmp_path, "good-candidate-hook")
    world = strict_world(monkeypatch, tmp_path, hooks={"good": good}, seed_mcp=WEAK_SEED)
    fake, _seen = scripted_engine([winner("good")])
    monkeypatch.setattr(engine, "optimize_anything", fake)
    hit = loosen_candidate_cells(monkeypatch, world, {"test_a"})
    evo = build(world, strict=True)

    evo.run()

    assert hit
    assert world.pr.pushes == [] and world.pr.creates == []
    assert "finish: incomplete" in log_text(world)


def test_finish_with_a_loosened_seed_test_cell_opens_no_pr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    good = fake_hook(tmp_path, "good-candidate-hook")
    world = strict_world(monkeypatch, tmp_path, hooks={"good": good}, seed_mcp=WEAK_SEED)
    fake, _seen = scripted_engine([winner("good")])
    monkeypatch.setattr(engine, "optimize_anything", fake)
    original = run.run_single

    def loosen_seed_test(task, mode, model, rep, **kwargs):
        if mode == "tilth_forced" and run.MODES[mode].git_sha == world.seed_sha and task == "test_a":
            raise run.HookLoosenedError("hook_loosened: strict cell ran a Bash command the hook denies: cat x")
        return original(task, mode, model, rep, **kwargs)

    monkeypatch.setattr(run, "run_single", loosen_seed_test)
    evo = build(world, strict=True)

    evo.run()

    assert world.pr.pushes == [] and world.pr.creates == []


# --- seed preflight faults refuse, never crash ---


def _refuses(world, capsys) -> None:
    evo = build(world, strict=True)
    assert evo.preflight() == 2
    assert capsys.readouterr().err.startswith("error:")
    assert world.calls == [] and world.spawned == [] and world.judge.labelled == []
    assert not world.stored()


def test_seed_build_failure_refuses_before_any_paid_call(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                         capsys) -> None:
    world = strict_world(monkeypatch, tmp_path)

    def broken(sha, repo=None):
        raise run.CandidateBuildFailed("cargo build failed", "error[E0432]", [])

    monkeypatch.setattr(run, "build_candidate", broken)

    _refuses(world, capsys)


@pytest.mark.parametrize("corpus_text", [None, "{not json", "{}", '{"must_deny": [{"rule": "r"}], "must_allow": []}'])
def test_unusable_hook_corpus_refuses_the_seed_preflight(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys,
                                                         corpus_text) -> None:
    world = strict_world(monkeypatch, tmp_path)
    corpus = tmp_path / "corpus.json"
    if corpus_text is not None:
        corpus.write_text(corpus_text)
    monkeypatch.setattr(hookgate, "CORPUS_PATH", corpus)

    _refuses(world, capsys)


def test_an_empty_hook_corpus_does_not_pass_the_gate_vacuously(tmp_path: Path) -> None:
    hook = fake_hook(tmp_path, "any-hook")

    assert hookgate.corpus_failures(str(hook), {"must_deny": [], "must_allow": []})


# --- hook gate bounds and missing subcommand ---


def test_a_hanging_hook_is_bounded_and_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    hang = tmp_path / "hang-hook"
    hang.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    hang.chmod(hang.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(hookgate, "HOOK_TIMEOUT_S", 0.2)
    corpus = {"must_deny": CORPUS["must_deny"][:2], "must_allow": CORPUS["must_allow"][:2]}

    failures = hookgate.corpus_failures(str(hang), corpus)

    assert len(failures) == 2 * (2 + 2) and all("could not run" in line for line in failures)


@pytest.mark.parametrize("code", [0, 2, 1])
def test_a_binary_without_the_hook_subcommand_fails_the_corpus(tmp_path: Path, code: int) -> None:
    """A binary that exits the same code for every input (clap usage error is 2) cannot pass both corpus halves."""
    stub = tmp_path / "no-hook"
    stub.write_text(f"#!{sys.executable}\nimport sys\nsys.exit({code})\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)

    assert hookgate.corpus_failures(str(stub))


def test_a_candidate_without_the_hook_subcommand_fails_the_hook_gate_stage(monkeypatch: pytest.MonkeyPatch,
                                                                         tmp_path: Path) -> None:
    stub = tmp_path / "no-hook"
    stub.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(2)\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    world = strict_world(monkeypatch, tmp_path, hooks={"nohook": stub})
    evo = ready(world)

    score, info = evo.evaluate(winner("nohook"), "dev_a")

    assert score == 0 and info["stage"] == "hook gate"
    assert all(call[3] == world.seed_sha for call in world.calls if call[1] == "tilth_forced")


def test_a_candidate_build_failure_in_the_gate_is_a_build_stage(monkeypatch: pytest.MonkeyPatch,
                                                               tmp_path: Path) -> None:
    world = strict_world(monkeypatch, tmp_path, hooks={"bad": fake_hook(tmp_path, "unused")})
    evo = ready(world)
    original = run.build_candidate

    def failing(sha, repo=None):
        if sha != world.seed_sha:
            raise run.CandidateBuildFailed("cargo build failed", "error[E0432]", [])
        return original(sha, repo)

    monkeypatch.setattr(run, "build_candidate", failing)

    score, info = evo.evaluate(winner("bad"), "dev_a")

    assert score == 0 and info["stage"] == "build"


def test_the_hook_gate_runs_once_per_candidate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    good = fake_hook(tmp_path, "good-candidate-hook")
    world = strict_world(monkeypatch, tmp_path, hooks={"good": good})
    evo = ready(world)
    runs = []
    original = hookgate.corpus_failures
    monkeypatch.setattr(hookgate, "corpus_failures", lambda *a, **k: runs.append(a[0]) or original(*a, **k))
    candidate = winner("good")

    evo.evaluate(candidate, "dev_a")
    evo.evaluate(candidate, "dev_b")

    assert runs.count(str(good)) == 1


# --- strict is its own baseline slot ---


def test_strict_run_buys_its_own_baselines_over_stored_non_strict_ones(monkeypatch: pytest.MonkeyPatch,
                                                                       tmp_path: Path) -> None:
    world = strict_world(monkeypatch, tmp_path)
    loose = build(world)
    assert loose.preflight() is None and loose.buy_baselines() is None
    loose_calls = len(world.runner_calls("baseline"))
    assert loose_calls and world.stored()

    strict = build(world, strict=True)
    assert strict.preflight() is None
    assert strict.buy_baselines() is None

    assert len(world.runner_calls("baseline")) == 2 * loose_calls


def test_a_second_strict_run_reuses_its_stored_strict_baselines(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = strict_world(monkeypatch, tmp_path)
    first = build(world, strict=True)
    assert first.preflight() is None and first.buy_baselines() is None
    bought = len(world.runner_calls("baseline"))

    again = build(world, strict=True)
    assert again.preflight() is None and again.buy_baselines() is None

    assert bought and len(world.runner_calls("baseline")) == bought


def test_a_non_strict_run_does_not_reuse_stored_strict_baselines(monkeypatch: pytest.MonkeyPatch,
                                                                 tmp_path: Path) -> None:
    world = strict_world(monkeypatch, tmp_path)
    strict = build(world, strict=True)
    assert strict.preflight() is None and strict.buy_baselines() is None
    bought = len(world.runner_calls("baseline"))

    loose = build(world)
    assert loose.preflight() is None and loose.buy_baselines() is None

    assert len(world.runner_calls("baseline")) == 2 * bought


def test_no_strict_flag_with_refreeze_survives_drift_without_a_hook_preflight(monkeypatch: pytest.MonkeyPatch,
                                                                             tmp_path: Path) -> None:
    seed_hook = fake_hook(tmp_path, "bad-seed-hook", flip={"native": [DENY_ROW["command"]]})
    world = strict_world(monkeypatch, tmp_path, seed_hook=seed_hook)
    first = build(world)
    assert first.preflight() is None and first.buy_baselines() is None
    world.env = "env-fingerprint-b"

    assert build(world).preflight() == 2
    again = build(world, "--refreeze-baselines")
    assert again.settings.strict_file_tools is False and again.settings.refreeze_baselines
    assert again.preflight() is None


def test_strict_refreeze_runs_the_seed_preflight_first(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seed_hook = fake_hook(tmp_path, "bad-seed-hook", flip={"native": [DENY_ROW["command"]]})
    world = strict_world(monkeypatch, tmp_path, seed_hook=seed_hook)
    evo = build(world, "--refreeze-baselines", strict=True)

    assert evo.preflight() == 2
    assert world.calls == []
