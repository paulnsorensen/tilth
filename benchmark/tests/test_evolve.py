"""The evolution loop: GEPA over tilth commits, scored by grader correctness against frozen baselines.

Every paid call is a stub (cells, judge, reflection, proposer, PR client); the
real pinned ``gepa.optimize_anything`` runs only over those stubs.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import baselines
import paired
import panels
import run
from evolve import candidate as candidates
from evolve import engine
from evolve.finish import GitHubPRClient
from evolve.loop import Cascade
from evolve.materialize import ApplyRejected, Materializer
from evolve_support import (
    CHEAP, DEV, SEED_MCP, TEST, build, cargo_available, child, copy_tilth, git, log_text,
    main, make_world, mcp, quota_stream, result_stream, scripted_engine, seed_candidate, seed_files,
)
from judge import core as judge_core
from judge import store as judge_store
from judge.store import Agreement

TEXTS = ("prompts/mcp.md", "prompts/tools/read.md", "prompts/tools/search.md", "prompts/tools/write.md")


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    return make_world(monkeypatch, tmp_path)


def use_engine(monkeypatch: pytest.MonkeyPatch, candidates_: list[dict] = (), **kwargs):
    fake, seen = scripted_engine(list(candidates_), **kwargs)
    monkeypatch.setattr(engine, "optimize_anything", fake)
    return seen


def ready(world, *extra: str, **kwargs):
    """An evolution after its preflight and baseline purchase, before the search."""
    evo = build(world, *extra, **kwargs)
    assert evo.preflight() is None
    assert evo.buy_baselines() is None
    return evo


def tilth_calls(world, sha: str | None = None) -> list[tuple[str, int]]:
    return [(task, rep) for task, mode, rep, called in world.calls if mode == "tilth" and (sha is None or called == sha)]


def patch_for(world, changes: dict[str, str]) -> str:
    """A unified diff of ``changes`` against the seed, made in a scratch checkout."""
    scratch = world.tmp / f"scratch-{len(list(world.tmp.glob('scratch-*')))}"
    git("worktree", "add", "-q", "--detach", str(scratch), world.seed_sha, cwd=world.repo)
    for name, text in changes.items():
        (scratch / name).parent.mkdir(parents=True, exist_ok=True)
        (scratch / name).write_text(text)
    git("add", "-A", cwd=scratch)
    diff = git("diff", "--cached", world.seed_sha, cwd=scratch)
    git("worktree", "remove", "--force", str(scratch), cwd=world.repo)
    return diff


def tool_stream(calls: list[tuple[str, dict, str]], *, cost: float = 0.4, text: str = "done") -> str:
    events = []
    for index, (name, tool_input, output) in enumerate(calls):
        events.append({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": f"toolu_{index}", "name": name, "input": tool_input}]}})
        events.append({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": f"toolu_{index}", "content": output}]}})
    return result_stream(text, cost=cost, events=events)


def is_proposer(argv: list[str]) -> bool:
    return "Read,Edit,Write,Glob,Grep" in argv


def accept(evo, candidate: dict, means: dict[str, float], *, just_check: bool = True) -> Cascade:
    """Place a materialized candidate on evolve's accepted frontier with the given per-task means."""
    result = evo.results.get(candidates.content_id(candidate)) or evo.new_cascade(candidate)
    result.sha = evo.materializer.materialize(candidate)
    result.just_check_ok = just_check
    result.reached_paid = True
    result.scores = dict(means)
    result.means = dict(means)
    result.accepted = True
    evo.frontier.append(result)
    return result


def refs(world) -> list[str]:
    return git("for-each-ref", "--format=%(refname)", "refs/evolve", cwd=world.repo).split()


# --- AC-1: records are c4 stripped records ---


def test_records_are_judge_stripped_records(world) -> None:
    world.judge.calibrated = True
    evo = ready(world)
    _score, info = evo.evaluate(evo.seed, "dev_a")

    rows = [row for row in world.stored() if row["mode"] == "tilth" and row["task"] == "dev_a"]
    assert info["records"]
    for record, row in zip(info["records"], rows, strict=True):
        sidecar = Path(row["trajectory_path"]).read_text()
        expected = judge_core.stripped_record(row, sidecar)
        assert {key: value for key, value in record.items() if key not in {"label", "critique"}} == expected
        assert set(record) - set(expected) == {"label", "critique"}
    critiqued = [row for row, _trajectory in world.judge.critiques if row["task"] == "dev_a"]
    assert critiqued and all(set(row) >= {"run_key", "correctness_reason", "trajectory_path"} for row in critiqued)
    assert all(trajectory == Path(row["trajectory_path"]).read_text() for row, trajectory in world.judge.critiques)


# --- AC-2: test-split rollouts never feed reflection ---


def test_test_split_never_reflected(world, monkeypatch: pytest.MonkeyPatch) -> None:
    world.judge.calibrated = True
    prompts: list[str] = []

    def spawn(argv, **kwargs):
        prompts.append(kwargs["input"])
        return subprocess.CompletedProcess(argv, 0, result_stream("no change"), "")

    seen = use_engine(monkeypatch, propose=[["prompts/mcp.md"], ["src_patch"]])
    assert main(world, spawn=spawn) == 0

    assert len(prompts) == 2
    assert any(task == "test_a" for task, _rep in tilth_calls(world))
    texts = [json.dumps(seen["side_infos"]), *prompts,
             *(json.dumps(row, default=str) for row, _ in world.judge.critiques)]
    for text in texts:
        assert "[test_a:" not in text
        assert '"task": "test_a"' not in text


# --- AC-3: the score is the paid-tier dev mean ---


def weak_world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A world whose seed fails every dev task, so any child is undominated."""
    return make_world(monkeypatch, tmp_path / "weak", seed_files(mcp(dev_a="0", dev_b="0", cheap_a="1", test_a="0")))


def test_paid_score_is_dev_mean(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = weak_world(monkeypatch, tmp_path)
    evo = ready(world, "--reruns", "2")
    scored = child(evo.seed, dev_a="110", dev_b="000", cheap_a="1", test_a="1")
    scores = {task: evo.evaluate(scored, task)[0] for task in DEV}
    assert scores["dev_a"] == pytest.approx(2 / 3)
    assert scores["dev_b"] == 0


def test_missing_contamination_flag_stops(world, monkeypatch: pytest.MonkeyPatch) -> None:
    evo = ready(world)
    real_run_plan = run.run_plan

    def unflagged(*args, **kwargs):
        rows = real_run_plan(*args, **kwargs)
        for row in rows:
            row.pop("contaminated", None)
        return rows

    monkeypatch.setattr(run, "run_plan", unflagged)
    with pytest.raises(Exception, match=r"contaminated.*cheap_a|cheap_a.*contaminated"):
        evo.evaluate(evo.seed, "dev_a")


# --- AC-4: judge gating ---


def test_unlabeled_task_refuses_run(world, capsys: pytest.CaptureFixture[str]) -> None:
    world.judge.label_errors["dev_b"] = judge_core.JudgeAnswerInvalid("dev_b: judge answered 'maybe'")

    assert main(world) != 0

    assert world.runner_calls() == []
    assert "dev_b" in capsys.readouterr().err


@pytest.mark.parametrize("error", [
    judge_core.CritiqueRejected("no verdict"), judge_core.CritiqueWithheld("no-trajectory"),
    judge_core.JudgeCallFailed("claude exited 1"),
], ids=["rejected", "withheld", "failed"])
def test_critique_error_recorded_not_raised(world, error: Exception) -> None:
    world.judge.calibrated = True
    world.judge.critique_error = error
    evo = ready(world)

    score, info = evo.evaluate(evo.seed, "dev_a")

    assert score == 1
    assert info["records"]
    for record in info["records"]:
        assert "critique" not in record
        assert record["critique_error"] == str(error)
        assert record["label"] == "strong"
    assert evo.stop.reason is None


# --- AC-5: the proposer works in an isolated export of the candidate's own tree ---


def test_proposer_command_disables_tools(world, tmp_path: Path) -> None:
    seen: list[dict] = []
    user_config = Path.home() / ".claude"

    def spawn(argv, **kwargs):
        config_dir = Path(kwargs["env"]["CLAUDE_CONFIG_DIR"])
        seen.append({"argv": argv, "env": dict(kwargs["env"]), "config_files": list(config_dir.iterdir()),
                     "cwd": kwargs["cwd"], "input": kwargs["input"]})
        return subprocess.CompletedProcess(argv, 0, tool_stream([]), "")

    evo = ready(world, spawn=spawn)
    evo.proposer.propose_src_patch(evo.seed, [{"prompt": "p", "row": {}, "trajectory": ""}])

    [call] = seen
    argv = call["argv"]
    assert argv[argv.index("--tools") + 1] == "Read,Edit,Write,Glob,Grep"
    disallowed = argv[argv.index("--disallowedTools") + 1].split(",")
    assert {"Bash", "WebFetch", "WebSearch", "Agent", "Task"} <= set(disallowed)
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--mcp-config" not in argv and "--bare" not in argv
    assert call["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-fixture-token"
    assert "ANTHROPIC_API_KEY" not in call["env"]
    assert call["env"]["CLAUDE_CONFIG_DIR"] and Path(call["env"]["CLAUDE_CONFIG_DIR"]) != user_config
    assert call["config_files"] == []
    assert call["input"] not in argv


def test_proposer_returns_cumulative_diff(world) -> None:
    lib = "pub mod mcp;\npub mod edit;\n\npub fn lib_marker() -> u8 {\n    2\n}\n"
    parent = {**seed_candidate(), "prompts/mcp.md": mcp(dev_a="1"), "src_patch": patch_for(world, {"src/lib.rs": lib})}
    exported: dict[str, object] = {}

    def spawn(argv, **kwargs):
        export = Path(kwargs["cwd"])
        exported.update(lib=(export / "src" / "lib.rs").read_text(), mcp=(export / "prompts" / "mcp.md").read_text(),
                        names=sorted(path.name for path in export.iterdir()))
        (export / "src" / "main.rs").write_text("fn main() {\n    println!(\"tilth evolved\");\n}\n")
        (export / "prompts" / "mcp.md").write_text("proposer rewrote the instructions")
        return subprocess.CompletedProcess(argv, 0, tool_stream([
            ("Edit", {"file_path": str(export / "src" / "main.rs")}, "ok"),
            ("Write", {"file_path": "prompts/mcp.md"}, "ok"),
        ]), "")

    evo = ready(world, spawn=spawn)
    diff = evo.proposer.propose_src_patch(parent, [])

    assert exported["lib"] == lib
    assert exported["mcp"] == parent["prompts/mcp.md"]
    assert exported["names"] == ["Cargo.lock", "Cargo.toml", "prompts", "src"]
    assert "+++ b/src/lib.rs" in diff and "+    2" in diff
    assert "+++ b/src/main.rs" in diff and "tilth evolved" in diff
    assert "prompts/mcp.md" not in diff
    assert "dropped" in log_text(world) and "prompts/mcp.md" in log_text(world)


def test_rejected_proposal_returns_parent_patch(world) -> None:
    parent_patch = patch_for(world, {"src/lib.rs": "pub fn evolved() {}\n"})
    parent = {**seed_candidate(), "src_patch": parent_patch}
    panel_file = str(world.tmp / "panel.json")

    def spawn(argv, **kwargs):
        export = Path(kwargs["cwd"])
        (export / "src" / "main.rs").write_text("fn main() {}\n// leaked\n")
        return subprocess.CompletedProcess(argv, 0, tool_stream([("Read", {"file_path": panel_file}, "{}")]), "")

    evo = ready(world, spawn=spawn)
    before = refs(world)
    proposal = evo.dispatcher(parent, {"src_patch": [{"records": []}]}, ["src_patch"])

    assert proposal == {"src_patch": parent_patch}
    assert refs(world) == before
    assert "rejected" in log_text(world)


# --- AC-6: the applier ---


@pytest.mark.parametrize("path", ["benchmark/run.py", "Cargo.toml"])
def test_rejects_out_of_allowlist_diff(world, path: str) -> None:
    patch = patch_for(world, {path: "# changed by a candidate\n"})
    materializer = Materializer(world.repo, world.seed_sha, "run1", world.tmp / "work")

    with pytest.raises(Exception, match=path):
        materializer.materialize({**seed_candidate(), "src_patch": patch})
    assert refs(world) == []


LIB = "pub mod mcp;\npub mod edit;\n\npub fn lib_marker() -> u8 {\n    1\n}\n"


@pytest.mark.parametrize("line", [
    'pub const P: &str = include_str!("../Cargo.toml");',
    'pub const P: &[u8] = include_bytes!(\n    "../../outside.bin"\n);',
    'pub const P: &str = include_str!("/etc/hostname");',
    'include!(concat!(env!("CARGO_MANIFEST_DIR"), "/src/main.rs"));',
    'pub const P: &str = include_str!(env!("HOME"));',
    "pub const P: &str = include_str!(PATH);",
    'pub const P: &str = include_str!["../Cargo.toml"];',
], ids=["parent", "multiline-bytes", "absolute", "concat-env", "env", "not-a-literal", "brackets"])
def test_rejects_include_outside_src_and_prompts(world, line: str) -> None:
    materializer = Materializer(world.repo, world.seed_sha, "run1", world.tmp / "work")
    patch = patch_for(world, {"src/lib.rs": LIB + line + "\n"})

    with pytest.raises(ApplyRejected, match="include"):
        materializer.materialize({**seed_candidate(), "src_patch": patch})
    assert refs(world) == []


def test_includes_inside_src_and_prompts_are_kept(world) -> None:
    materializer = Materializer(world.repo, world.seed_sha, "run1", world.tmp / "work")
    lines = 'pub const A: &str = include_str!("main.rs");\npub const B: &str = include_str!("../prompts/mcp.md");\n'
    sha = materializer.materialize({**seed_candidate(), "src_patch": patch_for(world, {"src/lib.rs": LIB + lines})})
    assert "include_str!(\"main.rs\")" in git("show", f"{sha}:src/lib.rs", cwd=world.repo)


@pytest.mark.parametrize("term", ["benchmark", "Benchmark", ".cheese", "tilth_bench", "TILTH_BENCH_DATA",
                                  "panel-path", "panel-name", "data-dir"])
def test_rejects_harness_terms_in_src(world, monkeypatch: pytest.MonkeyPatch, term: str) -> None:
    data = world.tmp / "harness-data"
    monkeypatch.setenv("TILTH_BENCH_DATA", str(data))
    evo = build(world)
    text = {"panel-path": str(world.tmp / "panel.json"), "panel-name": "panel.json", "data-dir": str(data)}.get(term, term)
    patch = patch_for(world, {"src/lib.rs": LIB + f"// reads {text} at run time\n"})

    with pytest.raises(ApplyRejected, match="names"):
        evo.materializer.materialize({**seed_candidate(), "src_patch": patch})
    assert refs(world) == []


@pytest.fixture(scope="module")
def tilth_clone(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    return copy_tilth(tmp_path_factory.mktemp("tilth"))


def _edit_line(text: str, needle: str, replacement: str) -> str:
    assert needle in text
    return text.replace(needle, replacement, 1)


def _clone_patch(repo: Path, seed: str, changes: dict[str, str]) -> str:
    scratch = repo.parent / f"scratch-{hashlib.sha256(json.dumps(changes).encode()).hexdigest()[:8]}"
    git("worktree", "add", "-q", "--detach", str(scratch), seed, cwd=repo)
    for name, text in changes.items():
        (scratch / name).write_text(text)
    diff = git("diff", seed, cwd=scratch)
    git("worktree", "remove", "--force", str(scratch), cwd=repo)
    return diff


def test_rejects_cfg_test_edit(tilth_clone, tmp_path: Path) -> None:
    repo, seed = tilth_clone
    mod = git("show", f"{seed}:src/mcp/mod.rs", cwd=repo)
    integration = git("show", f"{seed}:src/edit/integration_tests.rs", cwd=repo)
    materializer = Materializer(repo, seed, "cfgtest", tmp_path / "work")
    base = candidates.read_seed(repo, seed)
    assertion = _edit_line(mod, '"SERVER_INSTRUCTIONS must not introduce triple newlines"',
                           '"SERVER_INSTRUCTIONS may not introduce triple newlines"')
    integration_edit = integration.replace("\n", "\n// candidate edit\n", 1)

    for name, text in (("src/mcp/mod.rs", assertion), ("src/edit/integration_tests.rs", integration_edit)):
        with pytest.raises(Exception, match=r"cfg\(test\)"):
            materializer.materialize({**base, "src_patch": _clone_patch(repo, seed, {name: text})})
    assert git("for-each-ref", "refs/evolve/cfgtest", cwd=repo).strip() == ""


def _byte_lock(text: str) -> tuple[int, str, str]:
    import re
    body = text[text.index("fn server_instructions_byte_lock"):]
    count = int(re.search(r"SERVER_INSTRUCTIONS\.len\(\),\s*(\d+)", body)[1])
    lead = re.search(r'starts_with\(\s*"((?:[^"\\]|\\.)*)"', body)[1]
    last = re.search(r'ends_with\("((?:[^"\\]|\\.)*)"', body)[1]
    unescape = lambda literal: literal.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
    return count, unescape(lead), unescape(last)


def test_materialize_commits_text_and_patch(tilth_clone, tmp_path: Path) -> None:
    repo, seed = tilth_clone
    base = candidates.read_seed(repo, seed)
    new_mcp = "evolved first line\nevolved second line\n" + base["prompts/mcp.md"] + "\nEvolved last line."
    lib = git("show", f"{seed}:src/lib.rs", cwd=repo) + "\n// evolve marker\n"
    candidate = {**base, "prompts/mcp.md": new_mcp, "src_patch": _clone_patch(repo, seed, {"src/lib.rs": lib})}
    materializer = Materializer(repo, seed, "commit", tmp_path / "work")

    sha = materializer.materialize(candidate)

    assert git("rev-parse", f"{sha}^", cwd=repo).strip() == seed
    cid = candidates.content_id(candidate)
    assert git("rev-parse", f"evolve/commit/{cid[:12]}", cwd=repo).strip() == sha
    expected_agents = ("<!-- generated from prompts/mcp.md by scripts/regen-agents-md.sh — do not edit directly -->\n\n"
                       + new_mcp + "\n")
    assert git("show", f"{sha}:AGENTS.md", cwd=repo) == expected_agents
    assert git("show", f"{sha}:src/lib.rs", cwd=repo) == lib
    count, lead, last = _byte_lock(git("show", f"{sha}:src/mcp/mod.rs", cwd=repo))
    assert count == len(new_mcp.encode())
    assert lead == "evolved first line\nevolved second line"
    assert last == "Evolved last line."
    assert git("show", f"{sha}:prompts/mcp.md", cwd=repo) == new_mcp


@pytest.mark.skipif(not cargo_available(), reason="cargo is not installed")
def test_text_only_child_of_patched_parent_materializes(tilth_clone, tmp_path: Path) -> None:
    repo, seed = tilth_clone
    base = candidates.read_seed(repo, seed)
    lib = git("show", f"{seed}:src/lib.rs", cwd=repo) + "\n// evolve marker\n"
    parent = {**base, "prompts/mcp.md": base["prompts/mcp.md"] + "\nParent line.",
              "src_patch": _clone_patch(repo, seed, {"src/lib.rs": lib})}
    text_child = {**parent, "prompts/mcp.md": base["prompts/mcp.md"] + "\nChild line, longer than the parent's."}
    tool_child = {**parent, "prompts/tools/search.md": base["prompts/tools/search.md"] + "\nOne more hint.\n"}
    materializer = Materializer(repo, seed, "child", tmp_path / "work")

    parent_sha = materializer.materialize(parent)
    child_sha = materializer.materialize(text_child)
    tool_sha = materializer.materialize(tool_child)

    for sha in (child_sha, tool_sha):
        assert git("rev-parse", f"{sha}^", cwd=repo).strip() == seed
    changed = git("diff", "-U0", parent_sha, child_sha, "--", "src", cwd=repo)
    removed = [line for line in changed.splitlines() if line.startswith("-") and not line.startswith("---")]
    added = [line for line in changed.splitlines() if line.startswith("+") and not line.startswith("+++")]
    assert git("diff", "--name-only", parent_sha, child_sha, "--", "src", cwd=repo).split() == ["src/mcp/mod.rs"]
    assert len(removed) == len(added) == 2
    assert any("Child line" in line for line in added)
    assert any(str(len(text_child["prompts/mcp.md"].encode())) in line for line in added)
    assert git("diff", parent_sha, tool_sha, "--", "src", cwd=repo) == ""
    check = subprocess.run(["cargo", "check", "--locked", "--quiet"], cwd=materializer.worktree(child_sha),
                           env={**os.environ, "CARGO_TARGET_DIR": str(run.REPO_ROOT / "target")},
                           capture_output=True, text=True)
    assert check.returncode == 0, check.stderr[-2000:]


def test_candidate_id_is_content_hash(world) -> None:
    candidate = {**seed_candidate(), "prompts/mcp.md": mcp(dev_a="1", dev_b="0")}
    reordered = dict(reversed(list(candidate.items())))
    canonical = json.dumps(candidate, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()

    assert candidates.content_id(candidate) == hashlib.sha256(canonical).hexdigest()
    assert candidates.content_id(reordered) == candidates.content_id(candidate)
    materializer = Materializer(world.repo, world.seed_sha, "ids", world.tmp / "work")
    sha = materializer.materialize(candidate)
    commits = git("rev-list", "--all", "--count", cwd=world.repo)
    assert materializer.materialize(reordered) == sha
    assert git("rev-list", "--all", "--count", cwd=world.repo) == commits
    assert refs(world) == [f"refs/evolve/ids/{candidates.content_id(candidate)[:12]}"]
    assert materializer.materialize(seed_candidate()) == world.seed_sha


# --- AC-7: the cascade ---


def _broken_check(worktree: Path) -> tuple[bool, str]:
    _broken_check.calls.append(worktree)
    if "BROKEN" in (worktree / "prompts" / "mcp.md").read_text():
        return False, "Compiling tilth\nerror[E0425]: cannot find value `x` in this scope"
    return True, "ok"


_broken_check.calls = []


@pytest.mark.parametrize("stage", ["apply", "just check", "cheap tier"])
def test_cascade_stops_at_first_failure(world, stage: str) -> None:
    evo = ready(world, just_check=_broken_check)
    failing = {
        "apply": {**child(evo.seed, dev_a="1", cheap_a="1"), "src_patch": "not a patch\n"},
        "just check": child(evo.seed, dev_a="1", cheap_a="1", BROKEN="1"),
        "cheap tier": child(evo.seed, dev_a="1", dev_b="1", cheap_a="0"),
    }[stage]
    checks_before = len(_broken_check.calls)

    score, info = evo.evaluate(failing, "dev_a")

    assert score == 0
    assert info["stage"] == stage
    result = evo.results[candidates.content_id(failing)]
    assert result.stage == stage and not result.reached_paid
    non_seed = [call for call in world.calls if call[1] == "tilth" and call[3] != world.seed_sha]
    if stage in {"apply", "just check"}:
        assert non_seed == []
        assert len(_broken_check.calls) - checks_before == (0 if stage == "apply" else 1)
    else:
        assert {task for task, *_ in non_seed} == set(CHEAP)


def test_failed_candidate_build_is_a_cascade_stage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = weak_world(monkeypatch, tmp_path)
    broken = "BUILDFAIL\n" + mcp(dev_a="1", dev_b="1", cheap_a="1", test_a="1")
    real_build = world.fake_build

    def build_candidate(sha: str, repo: Path | None = None) -> run.CandidateBuild:
        if "BUILDFAIL" in git("show", f"{sha}:prompts/mcp.md", cwd=world.repo):
            raise RuntimeError(f"cargo build --release --locked failed at {sha}:\nerror[E0308]: mismatched types")
        return real_build(sha, repo)

    def spawn(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, result_stream(f"<new_text>{broken}</new_text>", cost=0.0), "")

    monkeypatch.setattr(run, "_build_candidate", build_candidate)
    monkeypatch.setattr("gepa.optimize_anything.make_litellm_lm", lambda *a, **k: pytest.fail("litellm used"))

    evo = build(world, calls="12", spawn=spawn)
    assert evo.run() == 0

    [failed] = [result for result in evo.results.values() if "BUILDFAIL" in result.candidate["prompts/mcp.md"]]
    assert failed.stage == "build" and "error[E0308]: mismatched types" in failed.tail
    assert "finish: " in log_text(world)
    assert not any("BUILDFAIL" in git("show", f"{sha}:prompts/mcp.md", cwd=world.repo)
                   for _task, mode, _rep, sha in world.calls if mode == "tilth")


def test_build_failure_side_info_names_stage(world, monkeypatch: pytest.MonkeyPatch) -> None:
    evo = ready(world)
    seed_sha = world.seed_sha

    def build_candidate(sha: str, repo: Path | None = None) -> run.CandidateBuild:
        if sha != seed_sha:
            raise RuntimeError(f"cargo build --release --locked failed at {sha}:\nerror[E0308]: mismatched types")
        return world.fake_build(sha, repo)

    monkeypatch.setattr(run, "_build_candidate", build_candidate)
    failing = child(evo.seed, dev_a="1", dev_b="1", cheap_a="1")
    score, info = evo.evaluate(failing, "dev_a")

    assert score == 0
    assert info["stage"] == "build" and "error[E0308]: mismatched types" in info["tail"]
    result = evo.results[candidates.content_id(failing)]
    assert result.stage == "build" and not result.reached_paid
    assert evo.evaluate(failing, "dev_b")[1]["stage"] == "build"


def test_cheap_tier_threshold(world) -> None:
    evo = ready(world)
    equal = child(evo.seed, dev_a="1", dev_b="0", cheap_a="1")
    below = child(evo.seed, dev_a="1", dev_b="1", cheap_a="0")

    assert evo.evaluate(equal, "dev_a")[1]["stage"] == "paid"
    assert evo.evaluate(below, "dev_a")[1]["stage"] == "cheap tier"


def test_passing_candidate_reaches_paid_score(world) -> None:
    evo = ready(world)
    score, info = evo.evaluate(child(evo.seed, dev_a="1", dev_b="1", cheap_a="1"), "dev_b")
    assert score == 1
    assert info["stage"] == "paid"
    assert set(info["task_scores"]) == set(DEV)


def test_build_failure_side_info_has_tail(world) -> None:
    evo = ready(world, just_check=_broken_check)
    _score, info = evo.evaluate(child(evo.seed, BROKEN="1"), "dev_a")
    assert info["stage"] == "just check"
    assert "error[E0425]: cannot find value `x` in this scope" in info["tail"]


def test_cheap_failure_side_info_strips_grader_text(world, monkeypatch: pytest.MonkeyPatch,
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    real = world.fake_run_single

    def printing(*args, **kwargs):
        row = real(*args, **kwargs)
        print(f"  -> {row['correctness_reason']}")
        return row

    monkeypatch.setattr(run, "run_single", printing)
    evo = ready(world)
    failing = child(evo.seed, dev_a="1", cheap_a="0")
    _score, info = evo.evaluate(failing, "dev_a")

    assert "SECRET_GT" in capsys.readouterr().out
    assert info["stage"] == "cheap tier"
    assert set(info) == {"stage", "task_scores", "records"}
    assert "SECRET_GT" not in json.dumps(info)
    sha = evo.results[candidates.content_id(failing)].sha
    expected = [judge_core.stripped_record(row, Path(row["trajectory_path"]).read_text())
                for row in world.stored() if row["task"] == "cheap_a" and row.get("git_sha") == sha]
    assert info["records"] == expected


def test_same_candidate_materializes_once(world) -> None:
    evo = ready(world)
    candidate = child(evo.seed, dev_a="1", dev_b="1", cheap_a="1")
    evo.evaluate(candidate, "dev_a")
    evo.evaluate(candidate, "dev_b")
    evo.evaluate(dict(reversed(list(candidate.items()))), "dev_a")

    sha = evo.results[candidates.content_id(candidate)].sha
    assert len([ref for ref in refs(world) if ref.endswith(candidates.content_id(candidate)[:12])]) == 1
    cid = candidates.content_id(candidate)
    assert sum(1 for path in world.checks if path.name == f"candidate-{cid[:12]}") == 1
    assert world.builds.count(sha) == 1
    rows = [row for row in world.stored() if row.get("git_sha") == sha]
    assert rows and {row["binary_sha256"] for row in rows} == {f"sha256-of-{sha}"}


def test_cascade_releases_the_worktree_after_just_check(world) -> None:
    evo = ready(world)
    candidate = child(evo.seed, dev_a="1", dev_b="1", cheap_a="1")
    evo.evaluate(candidate, "dev_a")

    [checked] = [path for path in world.checks if path.name.startswith("candidate-")]
    assert not checked.exists()
    assert str(checked) not in git("worktree", "list", cwd=world.repo)
    assert evo.results[candidates.content_id(candidate)].sha not in evo.materializer._worktrees


def test_materializer_release_removes_one_worktree(world) -> None:
    materializer = Materializer(world.repo, world.seed_sha, "run1", world.tmp / "work")
    sha = materializer.materialize(child(materializer.seed, dev_a="1"))
    other = materializer.worktree(world.seed_sha)
    kept = materializer.worktree(sha)

    materializer.release(sha)

    assert not kept.exists() and other.exists()
    assert str(kept) not in git("worktree", "list", cwd=world.repo)
    materializer.release(sha)
    materializer.cleanup()
    assert not other.exists()

def test_candidate_cells_use_candidate_binary(world) -> None:
    evo = ready(world)
    candidate = child(evo.seed, dev_a="1", dev_b="1", cheap_a="1")
    evo.evaluate(candidate, "dev_a")
    sha = evo.results[candidates.content_id(candidate)].sha

    assert sha in world.builds
    assert git("rev-parse", "HEAD", cwd=evo.materializer.worktree(sha)).strip() == sha
    served = [call for call in world.calls if call[1] == "tilth" and call[3] == sha]
    assert served
    rows = [row for row in world.stored() if row["mode"] == "tilth" and row.get("git_sha") == sha]
    assert len(rows) == len(served)
    for row in rows:
        assert row["variant"]["git_sha"] == sha and row["binary_sha256"] == f"sha256-of-{sha}"


# --- AC-8: frozen baselines and panel stamps ---


def test_refreeze_buys_once_then_reuses(world, monkeypatch: pytest.MonkeyPatch) -> None:
    use_engine(monkeypatch)
    assert main(world) == 0
    first = len(world.runner_calls("baseline"))
    assert first == len(set(world.runner_calls("baseline")))
    world.env = "env-fingerprint-b"

    assert main(world, "--run-id", "run2") != 0
    assert len(world.runner_calls("baseline")) == first

    assert main(world, "--run-id", "run3", "--refreeze-baselines") == 0
    assert len(world.runner_calls("baseline")) == 2 * first
    assert main(world, "--run-id", "run4") == 0
    assert len(world.runner_calls("baseline")) == 2 * first


def test_evolve_rows_carry_panel_stamp(world, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def load_panel(path, **kwargs):
        calls.append(kwargs)
        return world.panel

    monkeypatch.setattr(panels, "load_panel", load_panel)
    use_engine(monkeypatch, [child(seed_candidate(), dev_a="1", dev_b="1", cheap_a="1", test_a="1")])
    assert main(world) == 0

    assert [call["store_path"] for call in calls] == [run.RESULTS_DIR / baselines.STORE_FILENAME]
    rows = world.stored()
    assert {row["mode"] for row in rows} == {"baseline", "tilth"}
    assert {row["repetition"] for row in rows} == {1, 2}
    for row in rows:
        stamp = world.panel.stamp(row["task"])
        assert {key: row[key] for key in stamp} == stamp


# --- AC-9: accepted frontier, re-runs, deltas, plateau ---


def test_reruns_use_fresh_repetitions(world) -> None:
    evo = ready(world, "--reruns", "2")
    candidate = child(evo.seed, dev_a="1", dev_b="1", cheap_a="1")
    evo.evaluate(candidate, "dev_a")
    sha = evo.results[candidates.content_id(candidate)].sha

    dev_calls = sorted(call for call in tilth_calls(world, sha) if call[0] in DEV)
    assert dev_calls == sorted((task, rep) for task in DEV for rep in (1, 2, 3))
    assert evo.results[candidates.content_id(candidate)].accepted


def test_dominated_candidate_not_rerun(world) -> None:
    evo = ready(world)
    evo.evaluate(evo.seed, "dev_a")
    dominated = child(evo.seed, dev_a="0", dev_b="1", cheap_a="1")
    score, _info = evo.evaluate(dominated, "dev_a")
    result = evo.results[candidates.content_id(dominated)]

    assert score == 0
    assert sorted(call for call in tilth_calls(world, result.sha) if call[0] in DEV) == [("dev_a", 1), ("dev_b", 1)]
    assert not result.accepted
    assert result not in evo.frontier


def test_rerun_dominated_candidate_not_accepted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = weak_world(monkeypatch, tmp_path)
    evo = ready(world)
    member = accept(evo, child(evo.seed, tag="member"), {"dev_a": 1.0, "dev_b": 0.5})
    # One rollout scores (1, 1), which the member does not dominate; the re-run brings the means to (0.5, 0.5).
    candidate = child(evo.seed, dev_a="10", dev_b="10", cheap_a="1")
    evo.evaluate(candidate, "dev_a")
    result = evo.results[candidates.content_id(candidate)]

    assert sorted(call for call in tilth_calls(world, result.sha) if call[0] in DEV) == [
        ("dev_a", 1), ("dev_a", 2), ("dev_b", 1), ("dev_b", 2)]
    assert result.means == {} and not result.accepted
    assert evo.frontier == [member]
    assert "dominated after 2 rollouts" in log_text(world)


def test_dev_delta_never_buys_baseline_cells(world) -> None:
    evo = build(world)
    assert evo.preflight() is None
    evo.evaluate(child(evo.seed, dev_a="1", dev_b="1", cheap_a="1"), "dev_a")

    assert evo.deltas and world.runner_calls("baseline") == []


def test_single_rollout_never_finalist_on_tie(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    dev3 = ("dev_a", "dev_b", "dev_c")
    files = seed_files(mcp(dev_a="0", dev_b="0", dev_c="1", cheap_a="1", test_a="1"))
    world = make_world(monkeypatch, tmp_path, files)
    members = tuple(panels.Member(task, "local", "rust", split)
                    for split, tasks in (("cheap", CHEAP), ("dev", dev3), ("test", TEST)) for task in tasks)
    world.panel = panels.Panel(name="tie", split_seed=7, cheap=CHEAP, dev=dev3, test=TEST,
                               split_digest=panels.split_digest(members), members=members)
    seed_mcp = files["prompts/mcp.md"]
    better = mcp(dev_a="1", dev_b="1", dev_c="1000", cheap_a="1", test_a="1")
    tie = mcp(dev_a="1", dev_b="1", dev_c="0", cheap_a="1", test_a="1")
    proposals: list[str] = []

    def spawn(argv, **kwargs):
        text = "no change"
        if "<current>\n" + seed_mcp + "\n</current>" in kwargs["input"] and tie not in proposals:
            proposal = tie if better in proposals else better
            proposals.append(proposal)
            text = f"<new_text>{proposal}</new_text>"
        return subprocess.CompletedProcess(argv, 0, result_stream(text, cost=0.0), "")

    evo = build(world, "--reruns", "3", calls="400", spawn=spawn)
    monkeypatch.setattr("gepa.optimize_anything.make_litellm_lm", lambda *a, **k: pytest.fail("litellm used"))
    assert evo.preflight() is None and evo.buy_baselines() is None
    result = evo.search()

    tie_id = candidates.content_id({**seed_candidate(files), "prompts/mcp.md": tie})
    assert proposals == [better, tie]
    gepa_front = {index for front in result.per_val_instance_best_candidates.values() for index in front}
    assert any(candidates.content_id(result.candidates[index]) == tie_id for index in gepa_front)
    assert tie_id in evo.results and not evo.results[tie_id].accepted
    assert all(member.cid != tie_id for member in evo.frontier)
    evo.finish()
    assert evo.results[tie_id].sha not in {call[3] for call in world.calls if call[0] == "test_a"}


def test_delta_report_matches_paired(world) -> None:
    world.baseline.update(dev_a="10", dev_b="01")
    evo = ready(world)
    candidate = child(evo.seed, dev_a="11", dev_b="10", cheap_a="1")
    baseline_calls = len(world.runner_calls("baseline"))
    evo.evaluate(candidate, "dev_a")

    assert len(world.runner_calls("baseline")) == baseline_calls
    sha = evo.results[candidates.content_id(candidate)].sha
    rows = [row for row in world.stored()
            if row["task"] in DEV and (row["mode"] == "baseline" or row.get("git_sha") == sha)]
    report = next(delta for delta in evo.deltas if delta["candidate"] == candidates.content_id(candidate)[:12])
    accuracy = paired.paired_accuracy_delta(rows, "tilth", "baseline")
    cpc = paired.paired_cpc_delta(rows, "tilth", "baseline")
    assert report["accuracy_delta"] == pytest.approx(accuracy[0])
    assert report["accuracy_ci"] == pytest.approx(list(accuracy[1:3]))
    assert report["cpc_delta"] == pytest.approx(cpc[0])
    assert f"delta {candidates.content_id(candidate)[:12]}" in log_text(world)


def test_plateau_stops_after_p_rounds(world) -> None:
    evo = ready(world, "--plateau", "5")
    evo.evaluate(evo.seed, "dev_a")
    flat = [child(evo.seed, dev_a="1", dev_b="1", cheap_a="1", round=str(index)) for index in range(3)]
    for candidate in flat:
        evo.evaluate(candidate, "dev_a")
    assert evo.stop.reason is None

    # The seed scores 1.0 on both dev tasks, so nothing improves; two more unimproved rounds make five.
    evo.evaluate(child(evo.seed, dev_a="1", dev_b="1", cheap_a="1", round="3"), "dev_a")
    assert evo.stop.reason is None
    evo.evaluate(child(evo.seed, dev_a="1", dev_b="1", cheap_a="1", round="4"), "dev_a")
    assert evo.stop.reason == "plateau"

    sixth = child(evo.seed, dev_a="1", dev_b="1", cheap_a="1", round="5")
    calls = len(world.calls)
    assert evo.evaluate(sixth, "dev_a") == (0.0, {"stopped": "plateau"})
    assert len(world.calls) == calls


def test_plateau_resets_on_improvement(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = make_world(monkeypatch, tmp_path, seed_files(mcp(dev_a="0", dev_b="0", cheap_a="1", test_a="1")))
    evo = ready(world, "--plateau", "5")
    evo.evaluate(evo.seed, "dev_a")
    rounds = [("0", "0")] * 3 + [("1", "0")] + [("0", "0")] * 4
    for index, (dev_a, dev_b) in enumerate(rounds):
        evo.evaluate(child(evo.seed, dev_a=dev_a, dev_b=dev_b, cheap_a="1", round=str(index)), "dev_a")
    assert evo.stop.reason is None
    evo.evaluate(child(evo.seed, dev_a="0", dev_b="0", cheap_a="1", round="last"), "dev_a")
    assert evo.stop.reason == "plateau"


# --- AC-10: finish ---


def _finalist_runs(world, evo, *results) -> dict[str, list[tuple[str, int]]]:
    return {result.cid[:12]: sorted(call for call in tilth_calls(world, result.sha) if call[0] in TEST)
            for result in results}


def test_finalists_are_top_two_nonseed_plus_seed(world) -> None:
    evo = ready(world)
    seed_result = accept(evo, evo.seed, {"dev_a": 0.0, "dev_b": 0.0})
    members = [accept(evo, child(evo.seed, test_a="1", tag=str(mean)), {"dev_a": mean, "dev_b": mean})
               for mean in (0.5, 0.7, 0.6, 0.4)]
    evo.finish()

    reps = [("test_a", rep) for rep in (1, 2)]
    runs = _finalist_runs(world, evo, seed_result, *members)
    assert runs[members[1].cid[:12]] == reps and runs[members[2].cid[:12]] == reps
    assert runs[seed_result.cid[:12]] == reps
    assert runs[members[0].cid[:12]] == [] and runs[members[3].cid[:12]] == []


def test_winner_is_best_test_mean(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = make_world(monkeypatch, tmp_path,
                       seed_files(mcp(dev_a="1", dev_b="1", cheap_a="1", test_a="1" * 10 + "0" * 10)))
    evo = ready(world, "--reruns", "19")
    accept(evo, evo.seed, {"dev_a": 0.0, "dev_b": 0.0})
    best_dev = accept(evo, child(evo.seed, test_a="1" * 11 + "0" * 9, tag="a"), {"dev_a": 0.9, "dev_b": 0.9})
    second_dev = accept(evo, child(evo.seed, test_a="1" * 12 + "0" * 8, tag="b"), {"dev_a": 0.8, "dev_b": 0.8})

    assert evo.finish() is not None
    assert world.pr.pushes == [(second_dev.sha, "evolve/run1-winner")]
    assert best_dev.sha != second_dev.sha


def test_finalists_come_from_accepted_frontier(world) -> None:
    evo = ready(world)
    accept(evo, evo.seed, {"dev_a": 0.5, "dev_b": 0.5})
    outsider = child(evo.seed, test_a="1", tag="outsider")
    evo.new_cascade(outsider).sha = evo.materializer.materialize(outsider)
    evo.finish()
    assert tilth_calls(world, evo.results[candidates.content_id(outsider)].sha) == []


@pytest.mark.parametrize("scenario", ["dev mean", "first seen"])
def test_winner_tie_breaks(world, scenario: str) -> None:
    evo = ready(world)
    accept(evo, evo.seed, {"dev_a": 0.0, "dev_b": 0.0})
    second_mean = 0.8 if scenario == "dev mean" else 0.6
    first = accept(evo, child(evo.seed, test_a="1", tag="first"), {"dev_a": 0.6, "dev_b": 0.6})
    second = accept(evo, child(evo.seed, test_a="1", tag="second"), {"dev_a": second_mean, "dev_b": second_mean})
    evo.finish()
    assert world.pr.pushes == [((second if scenario == "dev mean" else first).sha, "evolve/run1-winner")]


def _remote(world) -> Path:
    remote = world.tmp / "remote.git"
    git("init", "-q", "--bare", str(remote), cwd=world.tmp)
    git("remote", "add", "origin", str(remote), cwd=world.repo)
    git("push", "-q", "origin", "main", cwd=world.repo)
    return remote


def test_finish_opens_one_draft_for_nonseed_winner(world) -> None:
    remote = _remote(world)
    gh: list[list[str]] = []

    def run_gh(argv, **kwargs):
        gh.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "https://example.invalid/pr/7\n", "")

    evo = ready(world, pr_client=GitHubPRClient(world.repo, remote="origin", run=run_gh))
    accept(evo, evo.seed, {"dev_a": 0.0, "dev_b": 0.0})
    loser = accept(evo, child(evo.seed, test_a="0", tag="loser"), {"dev_a": 0.9, "dev_b": 0.9})
    winner = accept(evo, child(evo.seed, test_a="1", tag="winner"), {"dev_a": 0.5, "dev_b": 0.5})
    evo.finish()

    heads = dict(line.split("\t")[::-1] for line in git("ls-remote", "--heads", str(remote), cwd=world.tmp).splitlines())
    assert set(heads) == {"refs/heads/main", "refs/heads/evolve/run1-winner"}
    assert heads["refs/heads/evolve/run1-winner"] == winner.sha
    assert loser.sha not in heads.values()
    [create] = gh
    assert create[:3] == ["gh", "pr", "create"] and "--draft" in create
    assert create[create.index("--base") + 1] == "main"
    assert create[create.index("--head") + 1] == "evolve/run1-winner"
    body = create[create.index("--body") + 1]
    assert "dev delta" in body and "test delta" in body
    assert not any("merge" in part for part in create)


def test_finish_refuses_winner_without_just_check_pass(world) -> None:
    evo = ready(world)
    accept(evo, evo.seed, {"dev_a": 0.0, "dev_b": 0.0})
    accept(evo, child(evo.seed, test_a="1"), {"dev_a": 0.9, "dev_b": 0.9}, just_check=False)
    assert evo.finish() is None
    assert world.pr.calls == []
    assert "just check" in log_text(world)


def test_finish_no_pr_when_seed_wins(world) -> None:
    evo = ready(world)
    accept(evo, evo.seed, {"dev_a": 0.0, "dev_b": 0.0})
    accept(evo, child(evo.seed, test_a="0"), {"dev_a": 0.9, "dev_b": 0.9})
    assert evo.finish() is None
    assert world.pr.calls == []
    assert "no improvement" in log_text(world)


def test_test_split_scored_once_at_finish(world, monkeypatch: pytest.MonkeyPatch) -> None:
    use_engine(monkeypatch, [child(seed_candidate(), dev_a="1", dev_b="1", cheap_a="1", test_a="1", tag="c")])
    evo = build(world)
    real_finish = evo.finish
    before_finish: list[int] = []

    def finish():
        before_finish.append(len(world.calls))
        return real_finish()

    evo.finish = finish
    assert evo.run() == 0
    assert all(call[0] != "test_a" for call in world.calls[:before_finish[0]])
    test_cells = [call for call in world.calls if call[0] == "test_a"]
    assert test_cells and len(test_cells) == len(set(test_cells))


def test_quota_stop_finish_buys_nothing(world) -> None:
    evo = ready(world)
    accept(evo, evo.seed, {"dev_a": 0.0, "dev_b": 0.0})
    accept(evo, child(evo.seed, test_a="1"), {"dev_a": 0.9, "dev_b": 0.9})
    evo.stop.set("quota")
    calls = len(world.calls)

    assert evo.finish() is None
    assert len(world.calls) == calls
    assert world.pr.calls == []
    assert "finish: incomplete (quota)" in log_text(world)


def test_mcp_unavailable_stop_in_search_makes_finish_buy_nothing(world, monkeypatch: pytest.MonkeyPatch) -> None:
    evo = ready(world)
    accept(evo, evo.seed, {"dev_a": 0.0, "dev_b": 0.0})
    accept(evo, child(evo.seed, test_a="1"), {"dev_a": 0.9, "dev_b": 0.9})

    def unavailable(*args, **kwargs):
        raise run.McpUnavailableError("tilth MCP did not start")

    monkeypatch.setattr(run, "run_single", unavailable)
    evo.evaluate(child(evo.seed, dev_a="1", cheap_a="1"), "dev_a")
    assert evo.stop.reason == "mcp-unavailable"
    monkeypatch.setattr(run, "run_single", world.fake_run_single)
    calls = len(world.calls)

    assert evo.finish() is None
    assert len(world.calls) == calls
    assert world.pr.calls == []
    assert "finish: incomplete (mcp-unavailable)" in log_text(world)


def test_quota_stop_finish_scores_stored_rows(world) -> None:
    evo = ready(world)
    accept(evo, evo.seed, {"dev_a": 0.0, "dev_b": 0.0})
    winner = accept(evo, child(evo.seed, test_a="1"), {"dev_a": 0.9, "dev_b": 0.9})
    evo.finish()
    world.pr.calls.clear()
    world.pr.pushes.clear()
    calls = len(world.calls)

    evo.stop.set("quota")
    assert evo.finish() is not None
    assert len(world.calls) == calls
    assert world.pr.pushes == [(winner.sha, "evolve/run1-winner")]


def test_finish_spends_reserve_after_ceiling(world, monkeypatch: pytest.MonkeyPatch) -> None:
    evo = ready(world, max_usd="2.05")
    accept(evo, evo.seed, {"dev_a": 0.0, "dev_b": 0.0})
    winner = accept(evo, child(evo.seed, test_a="1"), {"dev_a": 0.9, "dev_b": 0.9})
    evo.ledger.reserve = 0.65
    evo.ledger.charge(2.05 - 0.65 - evo.ledger.spent, source="search")
    evo.stop.set("ceiling")
    finish_reserve: list[float] = []
    real_plan = run.run_plan

    def recording_plan(*args, **kwargs):
        finish_reserve.append(kwargs["ledger"].reserve)
        return real_plan(*args, **kwargs)

    monkeypatch.setattr(run, "run_plan", recording_plan)
    assert evo.finish() is not None

    assert finish_reserve and set(finish_reserve) == {0.0}
    assert sorted(tilth_calls(world, winner.sha)) == [("test_a", 1), ("test_a", 2)]
    assert sorted(tilth_calls(world, world.seed_sha)) == [("test_a", 1), ("test_a", 2)]
    assert evo.ledger.spent <= 2.05 + 1e-9


def test_finish_stops_before_crossing_full_ceiling(world) -> None:
    evo = ready(world, max_usd="2.0")
    accept(evo, evo.seed, {"dev_a": 0.0, "dev_b": 0.0})
    accept(evo, child(evo.seed, test_a="1"), {"dev_a": 0.9, "dev_b": 0.9})
    evo.ledger.charge(1.95 - evo.ledger.spent, source="search")
    evo.stop.set("ceiling")

    assert evo.finish() is None
    assert world.pr.calls == []
    assert "finish: incomplete (ceiling)" in log_text(world)
    assert evo.ledger.spent <= 2.0 + 1e-9


def test_test_split_baseline_drift_refuses_before_paid_calls(world, monkeypatch: pytest.MonkeyPatch,
                                                             capsys: pytest.CaptureFixture[str]) -> None:
    use_engine(monkeypatch)
    assert main(world) == 0
    monkeypatch.setattr(run, "env_fingerprint", lambda task: "env-test-b" if "test_a" in str(task) else world.env)
    labelled, calls, spawned = len(world.judge.labelled), len(world.calls), len(world.spawned)

    assert main(world, "--run-id", "run2") == 2
    assert (len(world.judge.labelled), len(world.calls), len(world.spawned)) == (labelled, calls, spawned)
    assert "test_a" in capsys.readouterr().err

    assert main(world, "--run-id", "run3", "--refreeze-baselines") == 0
    assert [call for call in world.calls[calls:] if call[:2] == ("test_a", "baseline")]


def test_finish_baseline_drift_is_incomplete_not_traceback(world, monkeypatch: pytest.MonkeyPatch) -> None:
    use_engine(monkeypatch)
    assert main(world) == 0
    world.pr.calls.clear()

    def drifting(seed_candidate, **kwargs):
        world.env = "env-fingerprint-b"

    monkeypatch.setattr(engine, "optimize_anything", drifting)
    assert main(world, "--run-id", "run2") == 1
    assert world.pr.calls == []
    assert "finish: incomplete (baseline drift" in log_text(world, "run2")


def test_finish_ceiling_after_plateau_logs_ceiling(world) -> None:
    evo = ready(world, max_usd="2.0")
    accept(evo, evo.seed, {"dev_a": 0.0, "dev_b": 0.0})
    accept(evo, child(evo.seed, test_a="1"), {"dev_a": 0.9, "dev_b": 0.9})
    evo.ledger.charge(1.95 - evo.ledger.spent, source="search")
    evo.stop.set("plateau")

    assert evo.finish() is None
    assert world.pr.calls == []
    assert "finish: incomplete (ceiling)" in log_text(world)


def test_existing_winner_branch_is_a_logged_refusal(world) -> None:
    remote = _remote(world)
    gh: list[list[str]] = []
    run_gh = lambda argv, **kwargs: gh.append(list(argv)) or subprocess.CompletedProcess(argv, 0, "url\n", "")
    evo = ready(world, pr_client=GitHubPRClient(world.repo, remote="origin", run=run_gh))
    accept(evo, evo.seed, {"dev_a": 0.0, "dev_b": 0.0})
    accept(evo, child(evo.seed, test_a="1", tag="winner"), {"dev_a": 0.9, "dev_b": 0.9})
    git("push", "-q", "origin", f"{world.seed_sha}:refs/heads/evolve/run1-winner", cwd=world.repo)
    evo.search = lambda: None

    assert evo.run() == 1
    assert gh == []
    assert "finish: refused" in log_text(world) and "evolve/run1-winner" in log_text(world)
    heads = git("ls-remote", "--heads", str(remote), "evolve/run1-winner", cwd=world.tmp)
    assert heads.split()[0] == world.seed_sha


# --- AC-11: one ceiling, one ledger, one stop state ---


def test_evolve_requires_max_usd(world, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(world, max_usd=None) != 0
    assert world.calls == [] and world.judge.labelled == [] and world.spawned == []
    assert "--max-usd" in capsys.readouterr().err


def test_one_spend_ledger_instance(world, monkeypatch: pytest.MonkeyPatch) -> None:
    ledgers: list[object] = []
    real_plan = run.run_plan

    def recording_plan(*args, **kwargs):
        ledgers.append(kwargs["ledger"])
        return real_plan(*args, **kwargs)

    def judge_factory(ledger, floor):
        ledgers.append(ledger)
        return world.judge

    monkeypatch.setattr(run, "run_plan", recording_plan)
    use_engine(monkeypatch, propose=[["prompts/mcp.md"], ["src_patch"]])
    evo = build(world, judge_factory=judge_factory)
    assert evo.run() == 0
    ledgers.append(evo.paid.ledger)
    ledgers.append(evo.proposer.paid.ledger)
    assert len({id(ledger) for ledger in ledgers}) == 1
    assert ledgers[0] is evo.ledger


def test_ledger_counts_every_kind(world, monkeypatch: pytest.MonkeyPatch) -> None:
    class Client:
        def __call__(self, prompt):
            return judge_core.JudgeReply(text="strong", cost=0.2, cost_source="native")

    def spawn(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, result_stream("no change", cost=0.4 if is_proposer(argv) else 0.3), "")

    use_engine(monkeypatch, propose=[["prompts/mcp.md"], ["src_patch"]])
    evo = build(world, spawn=spawn,
                judge_factory=lambda ledger, floor: judge_core.Judge(ledger, Client(), cell_estimate_usd=floor))
    assert evo.run() == 0

    cells = 0.1 * len(world.calls)
    judge = 0.2 * len({*CHEAP, *DEV, *TEST})
    assert evo.ledger.spent == pytest.approx(cells + judge + 0.3 + 0.4)
    text = log_text(world)
    assert f"cells=${cells:.4f}" in text and f"judge=${judge:.4f}" in text
    assert "reflection=$0.3000" in text and "proposer=$0.4000" in text


def test_reflection_and_proposer_estimate_tiers(world) -> None:
    costs = iter([0.3, 0.5, 0.7])

    def spawn(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, result_stream("no change", cost=next(costs)), "")

    evo = ready(world, spawn=spawn)
    assert evo.paid.estimate("reflection") == pytest.approx(0.1)
    assert evo.paid.estimate("proposer") == pytest.approx(0.1)
    evo.reflect("prompts/mcp.md", "text", [])
    evo.reflect("prompts/mcp.md", "text", [])
    assert evo.paid.estimate("reflection") == pytest.approx(0.4)
    assert evo.paid.estimate("proposer") == pytest.approx(0.1)
    evo.proposer.propose_src_patch(evo.seed, [])
    assert evo.paid.estimate("proposer") == pytest.approx(0.7)


def test_judge_gets_ledger_not_estimate(world, monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[tuple[object, float]] = []

    def judge_factory(ledger, floor):
        built.append((ledger, floor))
        return world.judge

    evo = build(world, "--cell-estimate-usd", "0.25", judge_factory=judge_factory)
    assert built == [(evo.ledger, 0.25)]
    assert not hasattr(evo, "judge_estimate") and "judge" not in evo.paid.costs


def test_search_critique_respects_reserve(world, monkeypatch: pytest.MonkeyPatch) -> None:
    client_calls: list[str] = []

    class Client:
        def __call__(self, prompt):
            client_calls.append(prompt)
            return judge_core.JudgeReply(text="strong" if "Ground truth" in prompt else "verdict: apt\nok",
                                         cost=0.0, cost_source="native")

    factory = lambda ledger, floor: judge_core.Judge(ledger, Client(), cell_estimate_usd=floor)
    world.costs.update(dev_a=0.0, dev_b=0.0, cheap_a=0.0, test_a=0.1)
    warm = build(world, "--reruns", "0", "--cell-estimate-usd", "0.2", judge_factory=factory)
    assert warm.preflight() is None and warm.buy_baselines() is None
    warm.evaluate(warm.seed, "dev_a")
    client_calls.clear()

    evo = build(world, "--reruns", "0", "--cell-estimate-usd", "0.2", "--run-id", "run2", max_usd="1.0",
                judge_factory=factory)
    judge_store.write_agreement(Agreement(calibrated=True, **judge_store.current_stamp()))
    monkeypatch.setattr(evo.judge, "calibrate",
                        lambda labels: Agreement(calibrated=True, label_kappa=0.9, verdict_kappa=0.9))
    assert evo.preflight() is None and evo.buy_baselines() is None
    evo.ledger.reserve = 0.4
    evo.ledger.charge(0.5, source="search")

    _score, info = evo.evaluate(evo.seed, "dev_a")

    assert client_calls == []
    assert evo.stop.reason == "ceiling"
    assert all("critique_error" not in record for record in info.get("records", []))
    reserve_at_finish: list[float] = []
    real_plan = run.run_plan

    def recording_plan(*args, **kwargs):
        reserve_at_finish.append(kwargs["ledger"].reserve)
        return real_plan(*args, **kwargs)

    monkeypatch.setattr(run, "run_plan", recording_plan)
    evo.finish()
    assert set(reserve_at_finish) == {0.0}
    assert [call for call in world.calls if call[0] == "test_a"]
    assert evo.ledger.spent <= 1.0 + 1e-9


def test_insufficient_reserve_refuses_at_start(world, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(world, max_usd="0.15") != 0
    assert world.calls == [] and world.judge.labelled == [] and world.spawned == []
    assert "$0.25" in capsys.readouterr().err


def test_stop_state_neutralizes_calls(world) -> None:
    evo = ready(world)
    evo.stop.set("ceiling")
    calls, spawned = len(world.calls), len(world.spawned)

    proposal = evo.dispatcher(evo.seed, {name: [{"records": []}] for name in evo.seed}, list(evo.seed))
    assert proposal == {}
    assert evo.evaluate(child(evo.seed, dev_a="1"), "dev_a") == (0.0, {"stopped": "ceiling"})
    assert (len(world.calls), len(world.spawned)) == (calls, spawned)
    assert refs(world) == []


def test_cli_change_mid_search_stops_the_run(world, monkeypatch: pytest.MonkeyPatch) -> None:
    evo = ready(world)
    calls = len(world.calls)
    monkeypatch.setattr(run, "cli_version", lambda runner, *, fresh=False: "9.9.9" if fresh else "2.1.0")

    assert evo.evaluate(child(evo.seed, dev_a="1", cheap_a="1"), "dev_a") == (0.0, {"stopped": "cli-version"})
    assert evo.stop.reason == "cli-version" and len(world.calls) == calls
    assert evo.finish() is None and world.pr.calls == []
    assert "finish: incomplete (cli-version)" in log_text(world)


def test_run_log_has_no_gate_verdict(world, monkeypatch: pytest.MonkeyPatch) -> None:
    use_engine(monkeypatch, [child(seed_candidate(), dev_a="1", dev_b="1", cheap_a="1", test_a="1", tag="c")])
    evo = build(world)
    assert evo.run() == 0
    assert evo.deltas
    for text in (log_text(world), json.dumps(evo.deltas)):
        assert "grow TASK pool" not in text and "task-growth" not in text


def _ceiling_world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawn):
    """max 1.2, a 0.3 finish reserve, free search cells, and 0.1 test cells; a metric budget the stop must cut short."""
    world = weak_world(monkeypatch, tmp_path)
    world.costs.update(dev_a=0.0, dev_b=0.0, cheap_a=0.0, test_a=0.1)
    evo = build(world, "--reruns", "0", "--cell-estimate-usd", "0.05", max_usd="1.2", calls="2000", spawn=spawn)
    monkeypatch.setattr("gepa.optimize_anything.make_litellm_lm", lambda *a, **k: pytest.fail("litellm used"))
    assert evo.preflight() is None
    assert evo.ledger.reserve == pytest.approx(0.3)
    assert evo.buy_baselines() is None
    return world, evo


def count_evaluations_after_stop(evo) -> list[str]:
    """Record each evaluator call gepa makes once the stop state is set; gepa reads ``evo.evaluate`` at search."""
    after_stop: list[str] = []
    evaluate = evo.evaluate

    def counting(candidate, example):
        if evo.stop.is_set:
            after_stop.append(example)
        return evaluate(candidate, example)

    evo.evaluate = counting
    return after_stop


def test_real_gepa_ceiling_stop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    paid: list[str] = []

    def spawn(argv, **kwargs):
        paid.append("proposer" if is_proposer(argv) else "reflection")
        return subprocess.CompletedProcess(argv, 0, result_stream("no change"), "")

    world, evo = _ceiling_world(monkeypatch, tmp_path, spawn)
    better = child(evo.seed, dev_a="1", dev_b="1", cheap_a="1", test_a="1", tag="better")
    for task in DEV:
        evo.evaluate(better, task)
    evo.paid.costs.setdefault("reflection", []).append(0.2)
    evo.paid.costs.setdefault("proposer", []).append(0.2)
    evo.ledger.charge(0.8 - evo.ledger.spent, source="search")
    after_stop = count_evaluations_after_stop(evo)

    evo.search()
    assert paid == []
    assert evo.stop.reason == "ceiling"
    assert after_stop == []
    search_calls = len(world.calls)
    pr = evo.finish()

    assert pr is not None and world.pr.pushes == [(evo.results[candidates.content_id(better)].sha,
                                                   "evolve/run1-winner")]
    finish_calls = world.calls[search_calls:]
    assert finish_calls and all(call[0] == "test_a" for call in finish_calls)
    assert evo.ledger.spent <= 1.2 + 1e-9
    assert paid == []


def test_real_gepa_quota_in_proposer_stops(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    paid: list[str] = []

    def spawn(argv, **kwargs):
        kind = "proposer" if is_proposer(argv) else "reflection"
        paid.append(kind)
        return subprocess.CompletedProcess(argv, 0, quota_stream() if kind == "proposer" else result_stream("no change"), "")

    world, evo = _ceiling_world(monkeypatch, tmp_path, spawn)
    evo.ledger.max_usd = 100.0
    after_stop = count_evaluations_after_stop(evo)
    evo.search()
    assert paid == ["reflection"] * 4 + ["proposer"]
    assert evo.stop.reason == "quota"
    assert after_stop == []
    calls = len(world.calls)

    assert evo.finish() is None
    assert len(world.calls) == calls
    assert paid == ["reflection"] * 4 + ["proposer"]
    assert world.pr.calls == []
    text = log_text(world)
    assert "infra:quota" in text and "finish: incomplete (quota)" in text


# --- AC-12: seed and engine wiring ---


def test_seed_requires_prompt_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                    capsys: pytest.CaptureFixture[str]) -> None:
    files = seed_files()
    del files["prompts/tools/search.md"]
    world = make_world(monkeypatch, tmp_path, files)

    assert main(world) != 0
    assert world.calls == [] and world.judge.labelled == [] and world.spawned == []
    assert "prompts/tools/search.md" in capsys.readouterr().err


def test_engine_receives_seed_candidate(world, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = use_engine(monkeypatch)
    evo = build(world)
    assert evo.run() == 0

    files = seed_files()
    assert set(seen["seed"]) == {*TEXTS, "src_patch"}
    assert all(seen["seed"][name] == files[name] for name in TEXTS)
    assert seen["seed"]["src_patch"] == ""
    assert seen["evaluator"] == evo.evaluate
    assert seen["dataset"] == list(DEV)
    config = seen["config"]
    assert config.engine.max_metric_calls == 6
    assert config.engine.parallel is False
    assert any(isinstance(stop, engine.Stopper) for stop in config.stop_callbacks)
    assert config.reflection.custom_candidate_proposer is evo.dispatcher
    assert config.reflection.reflection_lm is None
    assert config.reflection.module_selector == "round_robin"


def test_dispatcher_routes_components_without_litellm(world) -> None:
    routed: list[tuple[str, str]] = []
    stop = engine.StopState()
    dispatcher = engine.Dispatcher(
        stop,
        reflect=lambda name, text, records: routed.append(("reflect", name)) or text + "!",
        propose=lambda candidate, records: routed.append(("propose", "src_patch")) or "patch",
    )
    seed = seed_candidate()
    proposal = dispatcher(seed, {name: [{"records": [{"r": name}]}] for name in seed}, list(seed))

    assert routed == [("reflect", name) for name in TEXTS] + [("propose", "src_patch")]
    assert proposal == {**{name: seed[name] + "!" for name in TEXTS}, "src_patch": "patch"}
    assert "litellm" not in sys.modules


def test_dispatcher_passes_failure_tail_to_every_component() -> None:
    seen: dict[str, list[dict]] = {}
    dispatcher = engine.Dispatcher(
        engine.StopState(),
        reflect=lambda name, text, records: seen.setdefault(name, records) and text,
        propose=lambda candidate, records: seen.setdefault("src_patch", records) and "patch",
    )
    failed = {"stage": "just check", "tail": "error[E0425]: cannot find value `x` in this scope"}
    paid = {"stage": "paid", "task_scores": {"dev_a": 1.0}, "records": [{"prompt": "p", "row": {}, "trajectory": ""}]}
    cheap = {"stage": "cheap tier", "task_scores": {"cheap_a": 0.0}, "records": []}
    dataset = {name: [failed, failed, paid, cheap] for name in ("prompts/mcp.md", "src_patch")}

    dispatcher(seed_candidate(), dataset, ["prompts/mcp.md", "src_patch"])

    for name in ("prompts/mcp.md", "src_patch"):
        assert seen[name] == [failed, *paid["records"]]


@pytest.mark.parametrize("glob", [{"pattern": "**/../../*"}, {"pattern": "src/*/../../../secret/*"},
                                  {"pattern": "*/../../../*", "path": "src"}])
def test_proposer_glob_climbing_after_a_wildcard_is_rejected(world, glob: dict) -> None:
    evo = ready(world)
    export = world.tmp / "export" / "tilth"
    (export / "src").mkdir(parents=True)
    inside = [{"name": "Glob", "input": {"pattern": "src/**/*.rs"}}, {"name": "Glob", "input": {"pattern": "src/*/../lib.rs"}}]

    assert evo.proposer.scan(inside, export) is None
    assert "outside the export" in evo.proposer.scan([{"name": "Glob", "input": glob, "output": ""}], export)


def test_real_gepa_drives_evaluate(world, monkeypatch: pytest.MonkeyPatch) -> None:
    examples: list[object] = []
    evo = build(world, calls="6")
    monkeypatch.setattr("gepa.optimize_anything.make_litellm_lm", lambda *a, **k: pytest.fail("litellm used"))

    def evaluate(candidate, example):
        examples.append(example)
        return 0.5, {"stage": "paid", "records": []}

    evo.evaluate = evaluate
    evo.search()
    assert set(examples) == set(DEV)
    assert len(examples) <= 6 + len(DEV)
    assert "litellm" not in sys.modules


# --- AC-13: the reflection call is isolated ---


def test_reflection_call_isolated(world, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"Stop": [{"command": "HOME_HOOK_RAN"}]}}))
    (home / ".mcp.json").write_text(json.dumps({"mcpServers": {"leak": {"command": "leak"}}}))
    monkeypatch.setenv("HOME", str(home))
    stream = (Path(__file__).parent / "fixtures" / "streams" / "claude_native_cost.jsonl").read_text()
    seen: list[dict] = []

    def spawn(argv, **kwargs):
        cwd = Path(kwargs["cwd"])
        config_dir = Path(kwargs["env"]["CLAUDE_CONFIG_DIR"])
        seen.append({"argv": list(argv), "input": kwargs["input"], "cwd": cwd, "cwd_files": list(cwd.iterdir()),
                     "config": config_dir, "config_files": list(config_dir.iterdir()), "env": dict(kwargs["env"])})
        return subprocess.CompletedProcess(argv, 0, stream, "")

    evo = ready(world, spawn=spawn)
    records = [{"prompt": "Name the dispatcher.", "row": {"task": "dev_a"}, "trajectory": "{}"}]
    assert evo.reflect("prompts/mcp.md", SEED_MCP, records) == SEED_MCP

    [call] = seen
    argv = call["argv"]
    assert argv[argv.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in argv and argv[argv.index("--setting-sources") + 1] == ""
    assert "--mcp-config" not in argv and "--bare" not in argv
    assert call["input"] not in argv and SEED_MCP not in " ".join(argv)
    assert call["cwd_files"] == [] and call["config_files"] == []
    assert world.repo not in call["cwd"].parents and call["cwd"] != world.repo
    assert call["config"] != home / ".claude"
    assert call["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-fixture-token"
    prompt = call["input"]
    assert "HOME_HOOK_RAN" not in prompt and ".mcp.json" not in prompt and str(home) not in prompt
    assert prompt == engine.reflection_prompt(SEED_MCP, records)
    remainder = prompt.replace(SEED_MCP, "").replace(json.dumps(records, indent=2, sort_keys=True), "")
    assert remainder == engine.reflection_prompt("", []).replace("[]", "")


def test_reflection_refuses_api_key(world, monkeypatch: pytest.MonkeyPatch) -> None:
    evo = ready(world)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    with pytest.raises(run.ClaudeAuthError):
        evo.reflect("prompts/mcp.md", "text", [])
    with pytest.raises(run.ClaudeAuthError):
        evo.proposer.propose_src_patch(evo.seed, [])
    assert world.spawned == []


_SECRET_ENV = {"CLAUDE_CODE_OAUTH_TOKEN": "oauth", "GH_TOKEN": "gh", "SSH_AUTH_SOCK": "/tmp/agent.sock",
               "AWS_SECRET_ACCESS_KEY": "aws", "ANTHROPIC_BASE_URL": "https://example.invalid",
               "MISE_GITHUB_TOKEN": "mise"}


def test_default_just_check_gets_an_allowlisted_env_and_a_shared_target(monkeypatch: pytest.MonkeyPatch,
                                                                      tmp_path: Path) -> None:
    from evolve import cli
    for key, value in _SECRET_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("MISE_DATA_DIR", "/tmp/mise")
    monkeypatch.setattr(run, "RESULTS_DIR", tmp_path / "results")
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    worktree = tmp_path / "wt"
    worktree.mkdir()

    assert cli.default_just_check(worktree) == (True, "ok")

    assert seen["argv"] == ["just", "check"]
    assert not set(_SECRET_ENV) & set(seen["env"])
    assert seen["env"]["PATH"] == os.environ["PATH"] and seen["env"]["HOME"] == os.environ["HOME"]
    assert seen["env"]["MISE_DATA_DIR"] == "/tmp/mise" and "CARGO_TARGET_DIR" not in seen["env"]
    link = worktree / "target"
    assert link.is_symlink() and link.resolve() == run.candidate_target_dir().resolve()
