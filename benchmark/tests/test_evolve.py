"""The evolution loop: GEPA over tilth commits, scored by cost per correct against frozen baselines.

Every paid call is a stub (cells, judge, reflection, proposer, PR client); the
real pinned ``gepa.optimize_anything`` runs only over those stubs.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import auth
import baselines
import paired
import panels
import run
from evolve import candidate as candidates
from evolve import engine
from evolve.finish import REPIN_NOTE, GitHubPRClient
from evolve.gitops import GitError
from evolve.loop import Cascade, EvolveError, dominates, task_score
from evolve.materialize import PHRASE_PIN_EXEMPT_ENV, ApplyRejected, Materializer
from evolve_support import (
    CHEAP, DEV, SEED_MCP, TEST, EvolveTask, build, cargo_available, child, copy_tilth, git, log_text,
    main, make_world, mcp, quota_stream, result_stream, scripted_engine, seed_candidate, seed_files, with_last_line,
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
    return [(task, rep) for task, mode, rep, called in world.calls
            if mode == "tilth_forced" and (sha is None or called == sha)]


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
    result.scores = dict(means)
    result.means = dict(means)
    result.accepted = True
    evo.frontier.append(result)
    return result


def refs(world) -> list[str]:
    return git("for-each-ref", "--format=%(refname)", "refs/evolve", cwd=world.repo).split()


# --- records are stripped records ---


def test_records_are_judge_stripped_records(world) -> None:
    world.judge.calibrated = True
    evo = ready(world)
    _score, info = evo.evaluate(evo.seed, "dev_a")

    rows = [row for row in world.stored() if row["mode"] == "tilth_forced" and row["task"] == "dev_a"]
    assert info["records"]
    for record, row in zip(info["records"], rows, strict=True):
        sidecar = Path(row["trajectory_path"]).read_text()
        expected = judge_core.stripped_record(row, sidecar)
        assert {key: value for key, value in record.items() if key not in {"label", "critique"}} == expected
        assert set(record) - set(expected) == {"label", "critique"}
    critiqued = [row for row, _trajectory in world.judge.critiques if row["task"] == "dev_a"]
    assert critiqued and all(set(row) >= {"run_key", "correctness_reason", "trajectory_path"} for row in critiqued)
    assert all(trajectory == Path(row["trajectory_path"]).read_text() for row, trajectory in world.judge.critiques)


# --- test-split rollouts never feed reflection ---


def test_test_split_never_reflected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = weak_world(monkeypatch, tmp_path)
    world.judge.calibrated = True
    prompts: list[str] = []

    def spawn(argv, **kwargs):
        prompts.append(kwargs["input"])
        return subprocess.CompletedProcess(argv, 0, result_stream("no change"), "")

    seen = use_engine(monkeypatch, [child(seed_candidate(), dev_a="1", dev_b="1", cheap_a="1", test_a="1", tag="c")],
                      propose=[["prompts/mcp.md"], ["src_patch"]])
    assert main(world, spawn=spawn) == 0

    assert len(prompts) == 2
    assert any(task == "test_a" for task, _rep in tilth_calls(world))
    texts = [json.dumps(seen["side_infos"]), *prompts,
             *(json.dumps(row, default=str) for row, _ in world.judge.critiques)]
    for text in texts:
        assert "[test_a:" not in text
        assert '"task": "test_a"' not in text


# --- the score is the paid-tier dev mean ---


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


# --- judge gating ---


def test_unlabeled_task_refuses_run(world, capsys: pytest.CaptureFixture[str]) -> None:
    world.judge.label_errors["dev_b"] = judge_core.JudgeAnswerInvalid("dev_b: judge answered 'maybe'")

    assert main(world) != 0

    assert world.runner_calls() == []
    assert "dev_b" in capsys.readouterr().err


def test_non_claude_model_refused_before_any_call(world, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_:
        main(world, "--model", "gpt5")

    assert exit_.value.code == 2
    assert world.runner_calls() == [] and world.spawned == []
    assert "invalid choice: 'gpt5'" in capsys.readouterr().err


def _commit_on(world, ref: str) -> str:
    """One more commit on the seed, pointed to by ``ref``; local ``main`` stays at the seed."""
    tree = git("rev-parse", f"{world.seed_sha}^{{tree}}", cwd=world.repo).strip()
    sha = git("commit-tree", tree, "-p", world.seed_sha, "-m", "next", cwd=world.repo).strip()
    git("update-ref", ref, sha, cwd=world.repo)
    return sha


@pytest.mark.parametrize("shadowed", [False, True])
def test_seed_on_the_remote_base_passes_when_the_local_base_lags(world, capsys: pytest.CaptureFixture[str],
                                                               shadowed: bool) -> None:
    sha = _commit_on(world, "refs/remotes/origin/main")
    if shadowed:
        git("update-ref", "refs/heads/origin/main", world.seed_sha, cwd=world.repo)

    evo = build(world, "--seed-sha", sha)

    assert not isinstance(evo, int), capsys.readouterr().err
    assert evo.settings.base_branch == "main"


@pytest.mark.parametrize("shadowed", [False, True])
def test_seed_only_on_the_local_base_is_refused_when_the_remote_base_exists(world, capsys, shadowed: bool) -> None:
    git("update-ref", "refs/remotes/origin/main", world.seed_sha, cwd=world.repo)
    sha = _commit_on(world, "refs/heads/main")
    if shadowed:
        git("update-ref", "refs/heads/origin/main", sha, cwd=world.repo)

    assert build(world, "--seed-sha", sha) == 2
    assert "is not an ancestor of origin/main" in capsys.readouterr().err


@pytest.mark.parametrize("shadowed", [False, True])
def test_seed_on_the_local_base_passes_without_a_remote(world, shadowed: bool) -> None:
    sha = _commit_on(world, "refs/heads/main")
    if shadowed:
        git("update-ref", "refs/tags/main", world.seed_sha, cwd=world.repo)

    assert not isinstance(build(world, "--seed-sha", sha), int)


def test_seed_only_on_a_tag_is_refused_without_a_remote_base(world, capsys) -> None:
    sha = _commit_on(world, "refs/tags/main")

    assert build(world, "--seed-sha", sha) == 2
    assert "is not an ancestor of main (--base-branch main)" in capsys.readouterr().err

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

def test_label_auth_failure_stops_the_run_with_store_only_finish(world) -> None:
    world.judge.label_errors["dev_a"] = auth.ClaudeAuthError("apiKeySource ANTHROPIC_API_KEY")
    evo = build(world)

    evo.run()

    assert evo.stop.reason == "auth"
    assert world.runner_calls() == []
    assert "finish: incomplete (auth)" in log_text(world)


def test_calibration_auth_failure_stops_the_run(world, monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(_labels):
        raise auth.ClaudeAuthError("apiKeySource ANTHROPIC_API_KEY")

    monkeypatch.setattr(world.judge, "calibrate", refused)
    evo = build(world)

    assert evo.preflight() is None

    assert evo.stop.reason == "auth"


def test_critique_auth_failure_stops_the_search(world) -> None:
    world.judge.calibrated = True
    world.judge.critique_error = auth.ClaudeAuthError("apiKeySource ANTHROPIC_API_KEY")
    evo = ready(world)

    evo.evaluate(evo.seed, "dev_a")

    assert evo.stop.reason == "auth"


def test_auth_plan_stop_makes_finish_buy_nothing(world, monkeypatch: pytest.MonkeyPatch) -> None:
    evo = ready(world)
    accept(evo, evo.seed, {"dev_a": 0.0, "dev_b": 0.0})
    accept(evo, child(evo.seed, test_a="1"), {"dev_a": 0.9, "dev_b": 0.9})

    def refused(*args, **kwargs):
        raise auth.ClaudeAuthError("apiKeySource ANTHROPIC_API_KEY")

    monkeypatch.setattr(run, "run_single", refused)
    evo.evaluate(child(evo.seed, dev_a="1", cheap_a="1"), "dev_a")
    assert evo.stop.reason == "auth"
    monkeypatch.setattr(run, "run_single", world.fake_run_single)
    calls = len(world.calls)

    assert evo.finish() is None
    assert len(world.calls) == calls
    assert "finish: incomplete (auth)" in log_text(world)


def test_evolve_preflight_checks_cheap_split_repos(world, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list] = []
    real = run.claude_auth_targets
    monkeypatch.setattr(run, "claude_auth_targets", lambda cells, **kw: seen.append(list(cells)) or real(cells, **kw))

    assert not isinstance(build(world), int)

    assert {task for task, _mode, _model in seen[0]} == {*CHEAP, *DEV, *TEST}


# --- the proposer works in an isolated export of the candidate's own tree ---


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


# --- the applier ---


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
    'pub const P: &str = include_str ! ("../Cargo.toml");',
], ids=["parent", "multiline-bytes", "absolute", "concat-env", "env", "not-a-literal", "brackets", "spaced-bang"])
def test_rejects_include_outside_src_and_prompts(world, line: str) -> None:
    materializer = Materializer(world.repo, world.seed_sha, "run1", world.tmp / "work")
    patch = patch_for(world, {"src/lib.rs": LIB + line + "\n"})

    with pytest.raises(ApplyRejected, match="include"):
        materializer.materialize({**seed_candidate(), "src_patch": patch})
    assert refs(world) == []


@pytest.mark.parametrize("attribute", [
    '#[path = "../../outside.rs"]',
    '#[ path="../../outside.rs" ]',
    '#\n[\npath = "../../outside.rs"]',
    '#![path = "../../outside.rs"]',
], ids=["outer", "spaced", "split", "inner"])
def test_rejects_path_attribute_without_naming_the_harness(world, attribute: str) -> None:
    materializer = Materializer(world.repo, world.seed_sha, "run1", world.tmp / "work")
    patch = patch_for(world, {"src/lib.rs": LIB + attribute + "\nmod outside;\n"})

    with pytest.raises(ApplyRejected, match=r"#\[path\] attribute") as rejected:
        materializer.materialize({**seed_candidate(), "src_patch": patch})
    assert "benchmark" not in rejected.value.tail.lower()
    assert refs(world) == []


def test_path_word_in_a_comment_or_string_is_kept(world) -> None:
    materializer = Materializer(world.repo, world.seed_sha, "run1", world.tmp / "work")
    lines = '// #[path = "x.rs"] is not used here\npub const P: &str = "#[path = \\"x.rs\\"]";\n'
    sha = materializer.materialize({**seed_candidate(), "src_patch": patch_for(world, {"src/lib.rs": LIB + lines})})
    assert sha != world.seed_sha

def test_includes_inside_src_and_prompts_are_kept(world) -> None:
    materializer = Materializer(world.repo, world.seed_sha, "run1", world.tmp / "work")
    lines = 'pub const A: &str = include_str!("main.rs");\npub const B: &str = include_str!("../prompts/mcp.md");\n'
    sha = materializer.materialize({**seed_candidate(), "src_patch": patch_for(world, {"src/lib.rs": LIB + lines})})
    assert "include_str!(\"main.rs\")" in git("show", f"{sha}:src/lib.rs", cwd=world.repo)


@pytest.mark.parametrize("term", ["benchmark", "Benchmark", ".cheese", "tilth_bench", "tilth-bench", "TILTH_BENCH_DATA",
                                  "TILTH_EVOLVE_CANDIDATE", "panel-path", "panel-name", "data-dir"])
def test_rejects_harness_terms_in_src(world, monkeypatch: pytest.MonkeyPatch, term: str) -> None:
    data = world.tmp / "harness-data"
    monkeypatch.setenv("TILTH_BENCH_DATA", str(data))
    evo = build(world)
    text = {"panel-path": str(world.tmp / "panel.json"), "panel-name": "panel.json", "data-dir": str(data)}.get(term, term)
    patch = patch_for(world, {"src/lib.rs": LIB + f"// reads {text} at run time\n"})

    with pytest.raises(ApplyRejected, match="names"):
        evo.materializer.materialize({**seed_candidate(), "src_patch": patch})
    assert refs(world) == []


@pytest.mark.parametrize("name", ["prompts/mcp.md", "prompts/tools/read.md", "prompts/tools/search.md",
                                  "prompts/tools/write.md"])
@pytest.mark.parametrize("text", ["x" * 2049, "—" * 683], ids=["ascii", "multibyte"])
def test_rejects_a_text_over_its_mcp_field_cap_before_a_worktree(world, name: str, text: str) -> None:
    evo = build(world)

    with pytest.raises(ApplyRejected, match=f"{name} is 2049 bytes; .* at most 2048 bytes"):
        evo.materializer.materialize({**seed_candidate(), name: text})
    assert refs(world) == []
    assert not (evo.run_dir / "worktrees").exists() or list((evo.run_dir / "worktrees").iterdir()) == []


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


def test_rejects_a_candidate_that_flips_the_phrase_pin_exemption(tilth_clone, tmp_path: Path) -> None:
    repo, seed = tilth_clone
    mod = git("show", f"{seed}:src/mcp/mod.rs", cwd=repo)
    definitions = git("show", f"{seed}:src/mcp/tools/definitions.rs", cwd=repo)
    materializer = Materializer(repo, seed, "exempt", tmp_path / "work")
    base = candidates.read_seed(repo, seed)
    always = _edit_line(mod, f'::std::option_env!("{PHRASE_PIN_EXEMPT_ENV}") == Some("1")', "true")
    unguarded = _edit_line(definitions, "if !crate::mcp::phrase_pins_exempt() {", "if false {")

    for name, text in (("src/mcp/mod.rs", always), ("src/mcp/tools/definitions.rs", unguarded)):
        with pytest.raises(ApplyRejected, match=r"cfg\(test\)"):
            materializer.materialize({**base, "src_patch": _clone_patch(repo, seed, {name: text})})
    assert git("for-each-ref", "refs/evolve/exempt", cwd=repo).strip() == ""

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
    # Replace the real prompt's first and last lines, so the text stays under its 2048-byte cap.
    body = with_last_line(base["prompts/mcp.md"], "Evolved last line.").split("\n", 1)[1]
    new_mcp = "evolved first line\nevolved second line\n" + body
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
    parent = {**base, "prompts/mcp.md": with_last_line(base["prompts/mcp.md"], "Parent line."),
              "src_patch": _clone_patch(repo, seed, {"src/lib.rs": lib})}
    text_child = {**parent,
                  "prompts/mcp.md": with_last_line(base["prompts/mcp.md"], "Child line, longer than the parent's.")}
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


# --- the cascade ---


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
    evo.evaluate(evo.seed, "dev_a")
    checks_before = len(_broken_check.calls)

    score, info = evo.evaluate(failing, "dev_a")

    assert score == 0
    assert info["stage"] == stage
    result = evo.results[candidates.content_id(failing)]
    assert result.stage == stage and not result.scores
    assert f"candidate {result.cid[:12]}: failed at {stage}" in log_text(world)
    if stage == "just check":
        assert "error[E0425]: cannot find value `x` in this scope" in log_text(world)
    non_seed = [call for call in world.calls if call[1] == "tilth_forced" and call[3] != world.seed_sha]
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
    assert f"candidate {failed.cid[:12]}: failed at build" in log_text(world)
    assert "error[E0308]: mismatched types" in log_text(world)
    assert "finish: " in log_text(world)
    assert not any("BUILDFAIL" in git("show", f"{sha}:prompts/mcp.md", cwd=world.repo)
                   for _task, mode, _rep, sha in world.calls if mode == "tilth_forced")


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
    assert result.stage == "build" and not result.scores
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


def test_same_candidate_gets_the_same_sha_across_runs(world) -> None:
    first = Materializer(world.repo, world.seed_sha, "run1", world.tmp / "work1")
    second = Materializer(world.repo, world.seed_sha, "run2", world.tmp / "work2")
    candidate = child(first.seed, dev_a="1")

    assert first.materialize(candidate) == second.materialize(candidate)


def test_candidate_cells_use_candidate_binary(world) -> None:
    evo = ready(world)
    candidate = child(evo.seed, dev_a="1", dev_b="1", cheap_a="1")
    evo.evaluate(candidate, "dev_a")
    sha = evo.results[candidates.content_id(candidate)].sha

    assert sha in world.builds
    assert git("rev-parse", "HEAD", cwd=evo.materializer.worktree(sha)).strip() == sha
    served = [call for call in world.calls if call[1] == "tilth_forced" and call[3] == sha]
    assert served
    rows = [row for row in world.stored() if row["mode"] == "tilth_forced" and row.get("git_sha") == sha]
    assert len(rows) == len(served)
    for row in rows:
        assert row["variant"]["git_sha"] == sha and row["binary_sha256"] == f"sha256-of-{sha}"


# --- frozen baselines and panel stamps ---


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
    assert {row["mode"] for row in rows} == {"baseline", "tilth_forced"}
    assert {row["repetition"] for row in rows} == {0, 1}
    for row in rows:
        stamp = world.panel.stamp(row["task"])
        assert {key: row[key] for key in stamp} == stamp


# --- accepted frontier, re-runs, deltas, plateau ---


def test_reruns_use_fresh_repetitions(world) -> None:
    evo = ready(world, "--reruns", "2")
    candidate = child(evo.seed, dev_a="1", dev_b="1", cheap_a="1")
    evo.evaluate(candidate, "dev_a")
    sha = evo.results[candidates.content_id(candidate)].sha

    dev_calls = sorted(call for call in tilth_calls(world, sha) if call[0] in DEV)
    assert dev_calls == sorted((task, rep) for task in DEV for rep in (0, 1, 2))
    assert evo.results[candidates.content_id(candidate)].accepted


def test_dominated_candidate_not_rerun(world) -> None:
    evo = ready(world)
    evo.evaluate(evo.seed, "dev_a")
    dominated = child(evo.seed, dev_a="0", dev_b="1", cheap_a="1")
    score, _info = evo.evaluate(dominated, "dev_a")
    result = evo.results[candidates.content_id(dominated)]

    assert score == 0
    assert sorted(call for call in tilth_calls(world, result.sha) if call[0] in DEV) == [("dev_a", 0), ("dev_b", 0)]
    assert not result.accepted
    assert result not in evo.frontier


def test_rerun_dominated_candidate_not_accepted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = weak_world(monkeypatch, tmp_path)
    evo = ready(world)
    evo.evaluate(evo.seed, "dev_a")
    member = accept(evo, child(evo.seed, tag="member"), {"dev_a": 1.0, "dev_b": 0.5})
    # One rollout scores (1, 1), which the member does not dominate; the re-run brings the means to (0.5, 0.5).
    candidate = child(evo.seed, dev_a="10", dev_b="10", cheap_a="1")
    score, _info = evo.evaluate(candidate, "dev_a")
    result = evo.results[candidates.content_id(candidate)]

    assert sorted(call for call in tilth_calls(world, result.sha) if call[0] in DEV) == [
        ("dev_a", 0), ("dev_a", 1), ("dev_b", 0), ("dev_b", 1)]
    assert result.means == {"dev_a": 0.5, "dev_b": 0.5} and not result.accepted
    assert score == 0.5  # GEPA sees the re-run mean, not the first rollout's 1.0
    assert member in evo.frontier and result not in evo.frontier
    assert "dominated after 2 rollouts" in log_text(world)


def test_first_rollout_that_did_not_complete_is_not_rerun(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = weak_world(monkeypatch, tmp_path)
    evo = ready(world)
    evo.evaluate(evo.seed, "dev_a")
    candidate = child(evo.seed, dev_a="E", dev_b="1", cheap_a="1")
    evo.evaluate(candidate, "dev_a")
    result = evo.results[candidates.content_id(candidate)]

    assert sorted(call for call in tilth_calls(world, result.sha) if call[0] in DEV) == [("dev_a", 0), ("dev_b", 0)]
    assert not result.accepted and result not in evo.frontier
    assert "a first rollout did not complete; not re-run" in log_text(world)


def test_rerun_that_did_not_complete_is_not_accepted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = weak_world(monkeypatch, tmp_path)
    evo = ready(world)
    evo.evaluate(evo.seed, "dev_a")
    candidate = child(evo.seed, dev_a="1E", dev_b="11", cheap_a="1")
    evo.evaluate(candidate, "dev_a")
    result = evo.results[candidates.content_id(candidate)]

    assert sorted(call for call in tilth_calls(world, result.sha) if call[0] in DEV) == [
        ("dev_a", 0), ("dev_a", 1), ("dev_b", 0), ("dev_b", 1)]
    assert not result.accepted and result not in evo.frontier
    assert "a re-run did not complete; not accepted" in log_text(world)


def test_candidate_buys_nothing_when_the_seed_is_not_on_the_frontier(monkeypatch: pytest.MonkeyPatch,
                                                                   tmp_path: Path) -> None:
    files = seed_files(mcp(dev_a="E", dev_b="1", cheap_a="1", test_a="0"))
    world = make_world(monkeypatch, tmp_path, files)
    use_engine(monkeypatch, [child(seed_candidate(files), dev_a="1", dev_b="1", cheap_a="1", test_a="1", tag="c")])

    assert main(world) == 1

    assert [call for call in world.calls if call[1] == "tilth_forced" and call[3] != world.seed_sha] == []
    assert "the seed is not on the accepted frontier" in log_text(world)


def test_dev_delta_never_buys_baseline_cells(world) -> None:
    evo = build(world)
    assert evo.preflight() is None
    evo.evaluate(child(evo.seed, dev_a="1", dev_b="1", cheap_a="1"), "dev_a")

    assert evo.deltas and world.runner_calls("baseline") == []


def test_empty_cheap_tier_goes_straight_to_dev(world, monkeypatch: pytest.MonkeyPatch) -> None:
    members = tuple(panels.Member(task, "local", "rust", split) for split, names in (("dev", DEV), ("test", TEST))
                    for task in names)
    world.panel = panels.Panel(name="no-cheap", cheap=(), dev=DEV, test=TEST,
                               split_digest=panels.split_digest(members), members=members)
    evo = build(world)
    planned: list[list] = []
    plan = evo._plan
    monkeypatch.setattr(evo, "_plan", lambda cells, *args, **kwargs: planned.append(list(cells)) or plan(cells, *args, **kwargs))
    assert evo.preflight() is None

    candidate = child(evo.seed, dev_a="1", dev_b="1", cheap_a="0")
    evo.evaluate(candidate, "dev_a")
    result = evo.results[candidates.content_id(candidate)]

    assert result.done and result.cheap_score is None and result.scores.keys() == set(DEV)
    assert planned and all(cells for cells in planned)
    assert {cell.task for cells in planned for cell in cells} <= set(DEV)


def test_single_rollout_never_finalist_on_tie(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    dev3 = ("dev_a", "dev_b", "dev_c")
    files = seed_files(mcp(dev_a="0", dev_b="0", dev_c="1", cheap_a="1", test_a="1"))
    world = make_world(monkeypatch, tmp_path, files)
    members = tuple(panels.Member(task, "local", "rust", split)
                    for split, tasks in (("cheap", CHEAP), ("dev", dev3), ("test", TEST)) for task in tasks)
    world.panel = panels.Panel(name="tie", cheap=CHEAP, dev=dev3, test=TEST,
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
    accuracy = paired.paired_accuracy_delta(rows, "tilth_forced", "baseline")
    cpc = paired.paired_cpc_delta(rows, "tilth_forced", "baseline")
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


# --- finish ---


def _finalist_runs(world, evo, *results) -> dict[str, list[tuple[str, int]]]:
    return {result.cid[:12]: sorted(call for call in tilth_calls(world, result.sha) if call[0] in TEST)
            for result in results}


def test_finalists_are_the_dev_winner_and_the_seed(world) -> None:
    evo = ready(world)
    seed_result = accept(evo, evo.seed, {"dev_a": 0.0, "dev_b": 0.0})
    members = [accept(evo, child(evo.seed, test_a="1", tag=str(mean)), {"dev_a": mean, "dev_b": mean})
               for mean in (0.5, 0.7, 0.6, 0.4)]
    evo.finish()

    reps = [("test_a", rep) for rep in (0, 1)]
    runs = _finalist_runs(world, evo, seed_result, *members)
    assert runs[members[1].cid[:12]] == reps and runs[seed_result.cid[:12]] == reps
    assert all(runs[members[index].cid[:12]] == [] for index in (0, 2, 3))


def test_winner_is_chosen_on_dev_and_its_test_delta_is_reported(monkeypatch: pytest.MonkeyPatch,
                                                                tmp_path: Path) -> None:
    world = make_world(monkeypatch, tmp_path,
                       seed_files(mcp(dev_a="1", dev_b="1", cheap_a="1", test_a="1" * 10 + "0" * 10)))
    evo = ready(world, "--reruns", "19")
    accept(evo, evo.seed, {"dev_a": 0.0, "dev_b": 0.0})
    best_dev = accept(evo, child(evo.seed, test_a="1" * 11 + "0" * 9, tag="a"), {"dev_a": 0.9, "dev_b": 0.9})
    second_dev = accept(evo, child(evo.seed, test_a="1" * 12 + "0" * 8, tag="b"), {"dev_a": 0.8, "dev_b": 0.8})

    assert evo.finish() is not None
    assert world.pr.pushes == [(best_dev.sha, "evolve/run1-winner")]
    assert tilth_calls(world, second_dev.sha) == []
    assert [report["candidate"] for report in evo.deltas if report["split"] == "test"] == [best_dev.cid[:12]]


def test_seed_wins_a_dev_tie_without_buying_the_test_split(world) -> None:
    evo = ready(world)
    accept(evo, evo.seed, {"dev_a": 0.5, "dev_b": 0.5})
    tied = accept(evo, child(evo.seed, test_a="1", tag="tied"), {"dev_a": 0.5, "dev_b": 0.5})
    calls = len(world.calls)

    assert evo.finish() is None
    assert len(world.calls) == calls and tilth_calls(world, tied.sha) == [] and world.pr.calls == []
    assert "no improvement" in log_text(world)


def test_stopped_search_without_a_dev_winner_fails_finish(world) -> None:
    evo = ready(world)
    accept(evo, evo.seed, {"dev_a": 0.5, "dev_b": 0.5})
    worse = accept(evo, child(evo.seed, test_a="1", tag="worse"), {"dev_a": 0.4, "dev_b": 0.4})
    evo.stop.set("ceiling")

    assert evo.finish() is None
    assert evo.finish_failure is not None and tilth_calls(world, worse.sha) == [] and world.pr.calls == []
    assert "finish: incomplete (ceiling)" in log_text(world)


@pytest.mark.parametrize("incomplete", ["winner", "seed"])
def test_finish_refuses_to_compare_incomplete_test_cells(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                         incomplete: str) -> None:
    files = seed_files(mcp(dev_a="1", dev_b="1", cheap_a="1", test_a="E" if incomplete == "seed" else "0"))
    world = make_world(monkeypatch, tmp_path, files)
    evo = ready(world)
    accept(evo, evo.seed, {"dev_a": 0.0, "dev_b": 0.0})
    accept(evo, child(evo.seed, test_a="E" if incomplete == "winner" else "1"), {"dev_a": 0.9, "dev_b": 0.9})

    assert evo.finish() is None
    assert world.pr.calls == [] and evo.finish_failure is not None
    assert "test-split cells did not complete" in log_text(world)


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
    loser = accept(evo, child(evo.seed, test_a="0", tag="loser"), {"dev_a": 0.5, "dev_b": 0.5})
    winner = accept(evo, child(evo.seed, test_a="1", tag="winner"), {"dev_a": 0.9, "dev_b": 0.9})
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
    assert "## Re-pin before landing" in body and REPIN_NOTE in body
    assert f"{PHRASE_PIN_EXEMPT_ENV}=1" in REPIN_NOTE and "Re-pin those asserts" in REPIN_NOTE
    assert f"finish: {REPIN_NOTE}" in log_text(world)
    assert not any("merge" in part for part in create)


def test_finish_refuses_winner_without_just_check_pass(world) -> None:
    evo = ready(world)
    accept(evo, evo.seed, {"dev_a": 0.0, "dev_b": 0.0})
    accept(evo, child(evo.seed, test_a="1"), {"dev_a": 0.9, "dev_b": 0.9}, just_check=False)
    assert evo.finish() is None
    assert world.pr.calls == []
    assert "just check" in log_text(world)


def test_finish_no_pr_when_seed_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = make_world(monkeypatch, tmp_path, seed_files(mcp(dev_a="1", dev_b="1", cheap_a="1", test_a="1")))
    evo = ready(world)
    accept(evo, evo.seed, {"dev_a": 0.0, "dev_b": 0.0})
    accept(evo, child(evo.seed, test_a="0"), {"dev_a": 0.9, "dev_b": 0.9})
    assert evo.finish() is None
    assert world.pr.calls == []
    assert "no improvement" in log_text(world)


def three_test_task_world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, seed_test: str = "0"):
    tasks = ("test_a", "test_b", "test_c")
    world = make_world(monkeypatch, tmp_path, seed_files(mcp(dev_a="1", dev_b="1", cheap_a="1", test_a=seed_test,
                                                              test_b=seed_test, test_c=seed_test)))
    for task in tasks[1:]:
        monkeypatch.setitem(run.TASKS, task, EvolveTask(name=task))
    members = tuple(panels.Member(id=task, family="local", language="rust", split=split)
                    for split, names in (("cheap", CHEAP), ("dev", DEV), ("test", tasks)) for task in names)
    world.panel = panels.Panel(name="evolve-fixture", cheap=CHEAP, dev=DEV, test=tasks,
                               split_digest=panels.split_digest(members), members=members)
    world.baseline.update({task: "0" for task in tasks})
    return world


def test_finish_rejects_gain_on_one_of_three_test_tasks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = three_test_task_world(monkeypatch, tmp_path)
    evo = ready(world)
    accept(evo, evo.seed, {"dev_a": 0.0, "dev_b": 0.0})
    accept(evo, child(evo.seed, test_a="11", test_b="00", test_c="00"), {"dev_a": 0.9, "dev_b": 0.9})

    assert evo.finish() is None

    text = log_text(world)
    assert world.pr.calls == [] and evo.finish_failure is None
    assert "finish: paired test delta +0.333 (95% CI [0.000, " in text and ", 3 tasks)" in text
    assert "finish: no improvement (the paired test-split interval does not exclude 0)" in text


def test_finish_declares_winner_that_beats_seed_on_every_test_task(monkeypatch: pytest.MonkeyPatch,
                                                                  tmp_path: Path) -> None:
    world = three_test_task_world(monkeypatch, tmp_path)
    evo = ready(world)
    accept(evo, evo.seed, {"dev_a": 0.0, "dev_b": 0.0})
    winner = accept(evo, child(evo.seed, test_a="11", test_b="11", test_c="11"), {"dev_a": 0.9, "dev_b": 0.9})

    assert evo.finish() is not None

    assert world.pr.pushes == [(winner.sha, "evolve/run1-winner")]
    assert "finish: paired test delta +1.000 (95% CI [1.000, 1.000], 3 tasks)" in log_text(world)
    assert "Winner minus seed, paired test delta +1.000 (95% CI [1.000, 1.000], 3 tasks)" in world.pr.creates[0]["body"]


def test_test_split_scored_once_at_finish(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = weak_world(monkeypatch, tmp_path)
    use_engine(monkeypatch, [child(seed_candidate(), dev_a="1", dev_b="1", cheap_a="1", test_a="1", tag="c")])
    evo = build(world)
    real_finish = evo.finish
    before_finish: list[int] = []

    def finish():
        before_finish.append(len(world.calls))
        return real_finish()

    evo.finish = finish
    assert evo.run() == 0
    assert all(call[0] != "test_a" or call[1] == "baseline" for call in world.calls[:before_finish[0]])
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
    assert sorted(tilth_calls(world, winner.sha)) == [("test_a", 0), ("test_a", 1)]
    assert sorted(tilth_calls(world, world.seed_sha)) == [("test_a", 0), ("test_a", 1)]
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


def test_test_split_baseline_drift_refuses_before_paid_calls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                             capsys: pytest.CaptureFixture[str]) -> None:
    world = weak_world(monkeypatch, tmp_path)
    use_engine(monkeypatch, [child(seed_candidate(), dev_a="1", dev_b="1", cheap_a="1", test_a="1", tag="c")])
    assert main(world) == 0
    monkeypatch.setattr(run, "env_fingerprint", lambda task: "env-test-b" if "test_a" in str(task) else world.env)
    labelled, calls, spawned = len(world.judge.labelled), len(world.calls), len(world.spawned)

    assert main(world, "--run-id", "run2") == 2
    assert (len(world.judge.labelled), len(world.calls), len(world.spawned)) == (labelled, calls, spawned)
    assert "test_a" in capsys.readouterr().err

    assert main(world, "--run-id", "run3", "--refreeze-baselines") == 0
    assert [call for call in world.calls[calls:] if call[:2] == ("test_a", "baseline")]


def test_finish_baseline_drift_is_incomplete_not_traceback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = weak_world(monkeypatch, tmp_path)
    use_engine(monkeypatch, [child(seed_candidate(), dev_a="1", dev_b="1", cheap_a="1", test_a="1", tag="c")])
    assert main(world) == 0
    world.pr.calls.clear()

    def drifting(seed, *, evaluator, dataset, **kwargs):
        for example in dataset:
            evaluator(child(seed, dev_a="1", dev_b="1", cheap_a="1", test_a="1", tag="drift"), example=example)
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


@pytest.mark.parametrize(("url", "expected"), [
    ("https://github.com/acme/tilth.git", ["--repo", "acme/tilth"]),
    ("https://github.com/acme/tilth", ["--repo", "acme/tilth"]),
    ("git@github.com:acme/tilth.git", ["--repo", "acme/tilth"]),
    ("ssh://git@github.com/acme/tilth.git", ["--repo", "acme/tilth"]),
    ("/srv/git/tilth.git", []),
    ("https://example.com/acme/tilth.git", []),
], ids=["https-git", "https", "scp", "ssh", "local-path", "other-host"])
def test_create_draft_targets_the_remote_repo(world, url: str, expected: list[str]) -> None:
    _remote(world)
    git("remote", "set-url", "origin", url, cwd=world.repo)
    gh: list[list[str]] = []
    run_gh = lambda argv, **kwargs: gh.append(list(argv)) or subprocess.CompletedProcess(argv, 0, "url\n", "")

    GitHubPRClient(world.repo, remote="origin", run=run_gh).create_draft(base="main", head="h", title="t", body="b")

    [argv] = gh
    assert [argv[i:i + 2] for i in range(len(argv)) if argv[i] == "--repo"] == ([expected] if expected else [])


def test_create_draft_omits_repo_when_the_remote_is_missing(world) -> None:
    gh: list[list[str]] = []
    run_gh = lambda argv, **kwargs: gh.append(list(argv)) or subprocess.CompletedProcess(argv, 0, "url\n", "")

    GitHubPRClient(world.repo, remote="nowhere", run=run_gh).create_draft(base="main", head="h", title="t", body="b")

    assert "--repo" not in gh[0]


def test_stopped_preflight_buys_no_baselines(world) -> None:
    evo = build(world)
    evo.preflight = lambda: evo.stop.set("quota")
    evo.search = lambda: None
    evo.finish = lambda: None

    assert evo.run() == 0
    assert world.calls == []


@pytest.mark.parametrize("error", [subprocess.CalledProcessError(1, ["gh"], stderr="gh: boom"), OSError("gh: boom")])
def test_failed_draft_pr_is_a_logged_failure(world, error: Exception) -> None:
    class FailingDraft:
        pushes: list[tuple[str, str]] = []

        def push(self, sha: str, branch: str) -> None:
            self.pushes.append((sha, branch))

        def create_draft(self, *, base: str, head: str, title: str, body: str) -> dict:
            raise error

    client = FailingDraft()
    evo = ready(world, pr_client=client)
    accept(evo, evo.seed, {"dev_a": 0.0, "dev_b": 0.0})
    accept(evo, child(evo.seed, test_a="1", tag="winner"), {"dev_a": 0.9, "dev_b": 0.9})
    evo.search = lambda: None

    assert evo.run() == 1
    assert len(client.pushes) == 1
    assert "pushed evolve/run1-winner but opening the draft PR failed: gh: boom" in log_text(world)


@pytest.mark.parametrize("error", [GitError(["status"], "boom"), OSError("boom"), EvolveError("boom")])
def test_search_error_is_a_recorded_failure_with_a_spend_summary(world, error: Exception) -> None:
    evo = ready(world)

    def failing_search():
        raise error

    evo.search = failing_search
    assert evo.run() == 1
    text = log_text(world)
    assert f"finish: run failed ({type(error).__name__}): " in text and "boom" in text
    assert "spend: " in text


def test_finish_without_a_nonseed_candidate_buys_nothing(world) -> None:
    evo = ready(world)
    accept(evo, evo.seed, {"dev_a": 0.0, "dev_b": 0.0})
    calls = len(world.calls)

    assert evo.finish() is None
    assert len(world.calls) == calls
    assert not [call for call in world.calls if call[0] == "test_a" and call[1] != "baseline"]
    assert world.pr.calls == [] and evo.finish_failure is None
    assert "finish: no improvement (no non-seed candidate on the frontier)" in log_text(world)


def test_dev_baseline_drift_refuses_before_paid_calls(world, monkeypatch: pytest.MonkeyPatch,
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    use_engine(monkeypatch)
    assert main(world) == 0
    monkeypatch.setattr(run, "env_fingerprint", lambda task: "env-dev-b" if "dev_a" in str(task) else world.env)
    labelled, calls, spawned = len(world.judge.labelled), len(world.calls), len(world.spawned)

    assert main(world, "--run-id", "run2") == 2
    assert (len(world.judge.labelled), len(world.calls), len(world.spawned)) == (labelled, calls, spawned)
    assert "dev_a" in capsys.readouterr().err

# --- one ceiling, one ledger, one stop state ---


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
    world.costs.update(dev_a=1e-6, dev_b=1e-6, cheap_a=1e-6, test_a=0.1)
    warm = build(world, "--reruns", "0", "--cell-estimate-usd", "0.2", judge_factory=factory)
    assert warm.preflight() is None and warm.buy_baselines() is None
    warm.evaluate(warm.seed, "dev_a")
    client_calls.clear()

    evo = build(world, "--reruns", "0", "--cell-estimate-usd", "0.2", "--run-id", "run2", max_usd="1.3",
                judge_factory=factory)
    judge_store.write_agreement(Agreement(calibrated=True, **judge_store.current_stamp()))
    monkeypatch.setattr(evo.judge, "calibrate",
                        lambda labels: Agreement(calibrated=True, label_kappa=0.9, verdict_kappa=0.9))
    assert evo.preflight() is None and evo.buy_baselines() is None
    evo.ledger.reserve = 0.4
    evo.ledger.charge(0.8, source="search")

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
    accept(evo, child(evo.seed, test_a="1"), {"dev_a": 0.9, "dev_b": 0.9})
    evo.finish()
    assert set(reserve_at_finish) == {0.0}
    assert [call for call in world.calls if call[0] == "test_a"]
    assert evo.ledger.spent <= 1.3 + 1e-9


def test_insufficient_reserve_refuses_at_start(world, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(world, max_usd="0.15") != 0
    assert world.calls == [] and world.judge.labelled == [] and world.spawned == []
    error = capsys.readouterr().err
    assert "$0.15" in error and "$0.80" in error and "$0.40" in error


def test_max_usd_equal_to_the_reserve_is_refused(world, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(world, max_usd="0.8") == 2
    error = capsys.readouterr().err
    assert "$0.80" in error and "$0.40" in error
    assert world.calls == [] and world.judge.labelled == []

    assert build(world, max_usd="0.85").preflight() is None


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
    accept(evo, child(evo.seed, test_a="1"), {"dev_a": 0.9, "dev_b": 0.9})
    accept(evo, child(evo.seed, test_a="1"), {"dev_a": 0.9, "dev_b": 0.9})
    calls = len(world.calls)
    monkeypatch.setattr(run, "cli_version", lambda runner, *, fresh=False: "9.9.9" if fresh else "2.1.0")

    assert evo.evaluate(child(evo.seed, dev_a="1", cheap_a="1"), "dev_a") == (0.0, {"stopped": "cli-version"})
    assert evo.stop.reason == "cli-version" and len(world.calls) == calls
    assert evo.finish() is None and world.pr.calls == []
    assert "finish: incomplete (baseline drift:" in log_text(world) and "cli_version" in log_text(world)


def test_run_log_has_no_gate_verdict(world, monkeypatch: pytest.MonkeyPatch) -> None:
    use_engine(monkeypatch, [child(seed_candidate(), dev_a="1", dev_b="1", cheap_a="1", test_a="1", tag="c")])
    evo = build(world)
    assert evo.run() == 0
    assert evo.deltas
    for text in (log_text(world), json.dumps(evo.deltas)):
        assert "grow TASK pool" not in text and "task-growth" not in text


def _ceiling_world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawn):
    """max 1.2, a 0.2 finish reserve, free search cells, and 0.1 test cells; a metric budget the stop must cut short."""
    world = weak_world(monkeypatch, tmp_path)
    world.costs.update(dev_a=1e-6, dev_b=1e-6, cheap_a=1e-6, test_a=0.1)
    evo = build(world, "--reruns", "0", "--cell-estimate-usd", "0.05", max_usd="1.2", calls="2000", spawn=spawn)
    monkeypatch.setattr("gepa.optimize_anything.make_litellm_lm", lambda *a, **k: pytest.fail("litellm used"))
    assert evo.preflight() is None
    assert evo.ledger.reserve == pytest.approx(0.2)
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
        evo.evaluate(evo.seed, task)
    for task in DEV:
        evo.evaluate(better, task)
    evo.paid.costs.setdefault("reflection", []).append(0.2)
    evo.paid.costs.setdefault("proposer", []).append(0.2)
    evo.ledger.charge(0.85 - evo.ledger.spent, source="search")
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
    accept(evo, child(evo.seed, test_a="1"), {"dev_a": 0.9, "dev_b": 0.9})
    calls = len(world.calls)

    assert evo.finish() is None
    assert len(world.calls) == calls
    assert paid == ["reflection"] * 4 + ["proposer"]
    assert world.pr.calls == []
    text = log_text(world)
    assert "infra:quota" in text and "finish: incomplete (quota)" in text


# --- seed and engine wiring ---


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
        assert seen[name] == [failed, {"scores": {"task_scores": {"dev_a": 1.0}}}, *paid["records"]]


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


# --- the reflection call is isolated ---


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
    assert prompt == engine.reflection_prompt(SEED_MCP, records, max_bytes=2048)
    size = len(SEED_MCP.encode())
    assert "at most 2048 UTF-8 bytes" in prompt and f"The current file is {size} bytes" in prompt
    remainder = prompt.replace(SEED_MCP, "").replace(json.dumps(records, indent=2, sort_keys=True), "")
    expected = engine.reflection_prompt("", [], max_bytes=2048).replace("[]", "")
    assert remainder == expected.replace("The current file is 0 bytes", f"The current file is {size} bytes")
    assert "UTF-8 bytes" not in engine.reflection_prompt("", [])


def _scripted_reflection(world, replies: list[str]) -> tuple[object, list[str]]:
    prompts: list[str] = []
    pending = iter(replies)

    def spawn(argv, **kwargs):
        prompts.append(kwargs["input"])
        return subprocess.CompletedProcess(argv, 0, result_stream(f"<new_text>{next(pending)}</new_text>", cost=0.0), "")

    return ready(world, spawn=spawn), prompts


def test_oversize_reflection_reply_is_shortened_once(world) -> None:
    long_text = "x" * 2100
    evo, prompts = _scripted_reflection(world, [long_text, "short instructions"])

    assert evo.reflect("prompts/mcp.md", SEED_MCP, []) == "short instructions"
    assert len(prompts) == 2
    assert "is 2100 UTF-8 bytes" in prompts[1] and "at most 2048 bytes" in prompts[1] and long_text in prompts[1]
    assert "the reply is 2100 bytes, over the 2048-byte cap; asking once to shorten it" in log_text(world)


def test_reflection_asks_to_shorten_at_most_once(world) -> None:
    evo, prompts = _scripted_reflection(world, ["x" * 2100, "y" * 2200])

    assert evo.reflect("prompts/mcp.md", SEED_MCP, []) == "y" * 2200
    assert len(prompts) == 2


def test_tool_description_reply_over_the_cap_is_shortened(world) -> None:
    evo, prompts = _scripted_reflection(world, ["z" * 3000, "short read guidance"])

    assert evo.reflect("prompts/tools/read.md", "read", []) == "short read guidance"
    assert len(prompts) == 2 and "is 3000 UTF-8 bytes" in prompts[1]


def test_reply_under_the_cap_is_not_shortened(world) -> None:
    evo, prompts = _scripted_reflection(world, ["z" * 2048])

    assert evo.reflect("prompts/tools/read.md", "read", []) == "z" * 2048
    assert len(prompts) == 1

def test_reflection_refuses_api_key(world, monkeypatch: pytest.MonkeyPatch) -> None:
    evo = ready(world)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    with pytest.raises(auth.ClaudeAuthError):
        evo.reflect("prompts/mcp.md", "text", [])
    with pytest.raises(auth.ClaudeAuthError):
        evo.proposer.propose_src_patch(evo.seed, [])
    assert world.spawned == []


def test_reflection_and_proposer_stay_on_sonnet_when_the_judge_is_opus(world) -> None:
    evo = build(world)
    argv = evo.reflect.argv()

    assert judge_core.config.JUDGE_MODEL == "opus"
    assert evo.settings.helper_model == "sonnet"
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert evo.proposer.model == "sonnet"


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

    class FakePopen:
        returncode = 0

        def __init__(self, argv, **kwargs):
            seen.update(argv=argv, **kwargs)

        def communicate(self, timeout=None):
            seen["timeout"] = timeout
            return "ok", ""

    monkeypatch.setattr(cli.subprocess, "Popen", FakePopen)
    worktree = tmp_path / "wt"
    worktree.mkdir()

    assert cli.default_just_check(worktree) == (True, "ok")

    assert seen["argv"] == ["just", "check"]
    assert not set(_SECRET_ENV) & set(seen["env"])
    assert seen["env"]["PATH"] == os.environ["PATH"] and seen["env"]["HOME"] == os.environ["HOME"]
    assert seen["env"]["MISE_DATA_DIR"] == "/tmp/mise" and "CARGO_TARGET_DIR" not in seen["env"]
    # The Rust tests read this exact name through option_env!, so a candidate may reword prompts/.
    assert seen["env"]["TILTH_EVOLVE_CANDIDATE"] == "1"
    link = worktree / "target"
    assert link.is_symlink() and link.resolve() == run.candidate_target_dir().resolve()
    assert seen["timeout"] == cli.JUST_CHECK_TIMEOUT_S and seen["start_new_session"] is True


def test_default_just_check_timeout_kills_the_whole_process_group(monkeypatch: pytest.MonkeyPatch,
                                                                   tmp_path: Path) -> None:
    from evolve import cli
    monkeypatch.setattr(run, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(cli, "JUST_CHECK_TIMEOUT_S", 1)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_just = bin_dir / "just"
    # A stand-in for cargo: a child that outlives ``just`` unless the whole group is killed.
    fake_just.write_text("#!/bin/sh\nsleep 60 &\necho $! > child.pid\nwait\n")
    fake_just.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    worktree = tmp_path / "wt"
    worktree.mkdir()

    started = time.monotonic()
    ok, output = cli.default_just_check(worktree)

    # A surviving child holds the output pipes open, so a partial kill waits for the 60 s sleep.
    assert ok is False and "timed out" in output and time.monotonic() - started < 30
    child = int((worktree / "child.pid").read_text())
    assert _exits_within(child, 30)


def test_exits_within_counts_a_zombie_and_a_reaped_process_as_exited() -> None:
    process = subprocess.Popen(["true"])
    # Not reaped yet: a zombie has exited.
    assert _exits_within(process.pid, 30)
    process.wait()
    assert _exits_within(process.pid, 0)


def test_exits_within_is_false_while_the_process_runs() -> None:
    process = subprocess.Popen(["sleep", "60"])
    try:
        assert _exits_within(process.pid, 0.2) is False
    finally:
        process.kill()
        process.wait()


@pytest.mark.parametrize(("state", "exited"), [("Z", True), ("X", True), ("x", True), ("R", False),
                                               ("S", False), ("D", False)])
def test_exited_counts_every_post_exit_state(monkeypatch: pytest.MonkeyPatch, state: str, exited: bool) -> None:
    # A reaper moves a zombie Z to X before /proc drops it; X must not read as alive again.
    stat_path = Path(f"/proc/{os.getpid()}/stat")
    real_read_text = Path.read_text

    def fake_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self == stat_path:
            return f"{os.getpid()} (sleep 60) {state} 1 1 1"
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    assert _exited(os.getpid()) is exited


def test_exited_is_true_when_the_process_exits_during_the_stat_read(monkeypatch: pytest.MonkeyPatch) -> None:
    # Linux returns ESRCH from read() when the process exits after open() of /proc/<pid>/stat.
    stat_path = Path(f"/proc/{os.getpid()}/stat")
    real_read_text = Path.read_text

    def racing_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self == stat_path:
            raise ProcessLookupError(3, "No such process")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", racing_read_text)
    assert _exited(os.getpid()) is True


def _exits_within(pid: int, timeout_s: float) -> bool:
    """True when ``pid`` exits within ``timeout_s``.

    A killed child can still run its exit path after the pipes close, and a loaded host delays it. The last
    sample comes after the deadline check, so a stalled test still looks once more before it fails.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        late = time.monotonic() >= deadline
        exited = _exited(pid)
        if exited or late:
            return exited
        time.sleep(0.05)


def _exited(pid: int) -> bool:
    """True once ``pid`` has exited: zombie (Z), being reaped (X, x), or gone from /proc."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    # ProcessLookupError: the reaper can collect the process between open() and read().
    except (FileNotFoundError, ProcessLookupError):
        return True
    return stat.rsplit(")", 1)[1].split()[0] in {"Z", "X", "x"}


# --- paid calls: the CLI budget flag and the cost sources ---

_HAIKU = "claude-haiku-4-5-20251001"


def _paid(spawn, ledger):
    from evolve.calls import PaidCalls, StopState
    return PaidCalls(ledger, StopState(), 0.3, spawn=spawn, log=lambda _line: None)


def _usage_stream(*, input_tokens: int = 1_000_000, output_tokens: int = 100_000) -> str:
    events = [{"type": "system", "subtype": "init", "apiKeySource": "none"},
              {"type": "assistant", "message": {"id": "m1", "content": [], "usage": {
        "input_tokens": input_tokens, "output_tokens": output_tokens}}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "ok"}]
    return "\n".join(json.dumps(event) for event in events) + "\n"


def test_paid_call_stops_with_ceiling_on_an_unreadable_budget_file(world, tmp_path: Path) -> None:
    from spend import SpendLedger
    ledger = SpendLedger.persistent("shared", 5.0, root=tmp_path)
    (tmp_path / "shared.jsonl").write_text('{"run_key": "k", "charged_usd": 1.0')  # torn record
    spawned: list[list[str]] = []
    paid = _paid(lambda argv, **_kw: spawned.append(argv), ledger)
    lines: list[str] = []
    paid.log = lines.append

    assert paid.call("reflection", ["claude", "-p"], "prompt", tmp_path) is None
    assert paid.stop.reason == "ceiling"
    assert spawned == []
    assert any("shared.jsonl" in line for line in lines)

def test_paid_call_passes_the_remaining_headroom_to_the_cli(world, tmp_path: Path) -> None:
    from spend import SpendLedger
    seen: list[list[str]] = []

    def spawn(argv, **kwargs):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, result_stream("ok", cost=0.01), "")

    ledger = SpendLedger(2.0, reserve=0.5)
    ledger.charge(0.25, source="cells")
    assert _paid(spawn, ledger).call("reflection", ["claude", "-p"], "prompt", tmp_path) is not None
    assert seen[0][-2:] == ["--max-budget-usd", "1.2500"]

    unbounded = []
    _paid(lambda argv, **kw: unbounded.append(list(argv)) or subprocess.CompletedProcess(
        argv, 0, result_stream("ok"), ""), SpendLedger(None)).call("reflection", ["claude", "-p"], "p", tmp_path)
    assert "--max-budget-usd" not in unbounded[0]


@pytest.mark.parametrize(("stream", "cost", "source"), [
    (result_stream("ok", cost=0.07), 0.07, "reflection:native"),
    (_usage_stream(), 1.5, "reflection:pricing"),
    (_usage_stream(input_tokens=0, output_tokens=0), 0.3, "reflection:estimate"),
    ("not json\n", 0.3, "reflection:estimate"),
], ids=["native", "pricing", "zero-priced", "unparseable"])
def test_paid_call_cost_source(world, tmp_path: Path, stream: str, cost: float, source: str) -> None:
    from spend import SpendLedger
    ledger = SpendLedger(100.0)
    paid = _paid(lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stream, ""), ledger)

    paid.call("reflection", ["claude", "-p", "--model", _HAIKU], "prompt", tmp_path)

    assert ledger.charges == [(pytest.approx(cost), source)]


# --- cure: ties, early stops, critiques only when read, released labels, headroom ---


def test_tie_with_the_seed_opens_no_draft(world) -> None:
    evo = ready(world)
    accept(evo, evo.seed, {"dev_a": 0.0, "dev_b": 0.0})
    accept(evo, child(evo.seed, test_a="0", tag="tied"), {"dev_a": 0.9, "dev_b": 0.9})
    evo.finish()

    assert world.pr.pushes == [] and evo.finish_failure is None
    assert "the paired test-split interval does not exclude 0" in log_text(world)


@pytest.mark.parametrize(("reason", "failed"), [("quota", True), ("ceiling", True), ("plateau", False)])
def test_stop_before_any_accepted_candidate(world, reason: str, failed: bool) -> None:
    evo = ready(world)
    accept(evo, evo.seed, {"dev_a": 0.0, "dev_b": 0.0})
    evo.stop.set(reason)
    evo.finish()

    assert (evo.finish_failure is not None) is failed
    assert world.pr.pushes == []


def test_cheap_tier_critiques_are_bought_only_for_a_cheap_failure(world) -> None:
    world.judge.calibrated = True
    evo = ready(world)
    evo.evaluate(evo.seed, "dev_a")
    evo.evaluate(child(evo.seed, dev_a="1", dev_b="1", cheap_a="1", tag="passes"), "dev_a")
    assert not [row for row, _ in world.judge.critiques if row["task"] in CHEAP]

    _score, info = evo.evaluate(child(evo.seed, dev_a="1", dev_b="1", cheap_a="0", tag="fails"), "dev_a")
    assert info["stage"] == "cheap tier" and info["records"]
    assert all("critique" in record for record in info["records"])
    assert [row for row, _ in world.judge.critiques if row["task"] in CHEAP]


def test_records_carry_only_labels_the_agreement_releases(world, monkeypatch: pytest.MonkeyPatch) -> None:
    world.judge.calibrated = True
    world.judge.labels = {"dev_a": "weak", "dev_b": "strong"}
    monkeypatch.setattr(world.judge, "calibrate", lambda labels: Agreement(
        calibrated=True, label_kappa=0.9, verdict_kappa=0.9, tasks=("dev_b",)))
    evo = ready(world)

    assert evo.released == {"dev_b": "strong"}
    _score, info = evo.evaluate(evo.seed, "dev_a")
    assert {record["label"] for record in info["records"]} == {None}
    _score, info = evo.evaluate(evo.seed, "dev_b")
    assert {record["label"] for record in info["records"]} == {"strong"}


def test_paid_call_refuses_below_the_minimum_headroom(world, tmp_path: Path) -> None:
    from evolve.calls import PaidCalls, StopState
    from spend import SpendLedger
    started: list[list[str]] = []
    ledger = SpendLedger(1.0, reserve=0.995)
    stop = StopState()
    paid = PaidCalls(ledger, stop, 0.0, spawn=lambda argv, **kw: started.append(argv), log=lambda _line: None)

    assert paid.call("reflection", ["claude", "-p"], "prompt", tmp_path) is None
    assert started == [] and stop.reason == "ceiling"


def test_real_gepa_reflection_prompt_carries_the_records(world) -> None:
    world.judge.calibrated = True
    evo = build(world, calls="12")
    assert evo.preflight() is None and evo.buy_baselines() is None
    evo.search()

    prompts = [call["input"] for call in world.spawned if "<records>" in call.get("input", "")]
    assert prompts, "gepa never reached the reflection"
    records = prompts[0].split("<records>", 1)[1]
    assert "[dev_a:" in records  # the rollout's tool output reached the real gepa reflection prompt
    assert '"label": "strong"' in records and "verdict: apt" in records


HIDDEN = 'concat!(env!("CARGO_MANIFEST_DIR"), "/../x")'


@pytest.mark.parametrize("files", [
    {"src/lib.rs": LIB + f"use std::include_str as grab;\npub const P: &str = grab!({HIDDEN});\n"},
    {"src/lib.rs": LIB + f"use std::{{include_str as grab, mem}};\npub const P: &str = grab!({HIDDEN});\n"},
    {"src/lib.rs": LIB + "pub use core::include_bytes as grab;\n"},
    {"src/lib.rs": LIB + 'include!("evil.inc");\n',
     "src/evil.inc": f"pub const P: &str = include_str!({HIDDEN});\n"},
    {"src/lib.rs": LIB + 'include!("evil.inc");\n',
     "src/evil.inc": 'pub const P: &str = include_str!("../../outside.txt");\n'},
], ids=["alias", "grouped-alias", "reexport", "nested-inc-concat", "nested-inc-outside"])
def test_rejects_include_bypasses(world, files: dict[str, str]) -> None:
    materializer = Materializer(world.repo, world.seed_sha, "run1", world.tmp / "work")

    with pytest.raises(ApplyRejected, match="include"):
        materializer.materialize({**seed_candidate(), "src_patch": patch_for(world, files)})
    assert refs(world) == []


MACRO_BY_NAME = (
    "macro_rules! call {{ ($m:ident) => {{ $m!({arg}) }} }}\n"
    "pub const X: &str = call!({name});\n")


@pytest.mark.parametrize("name, arg", [
    ("include_str", HIDDEN),
    ("include_bytes", HIDDEN),
    ("cfg", "test"),
], ids=["include_str", "include_bytes", "cfg"])
def test_rejects_a_guarded_macro_passed_by_name(world, name: str, arg: str) -> None:
    materializer = Materializer(world.repo, world.seed_sha, "run1", world.tmp / "work")
    patch = patch_for(world, {"src/lib.rs": LIB + MACRO_BY_NAME.format(name=name, arg=arg)})

    with pytest.raises(ApplyRejected, match="without a call"):
        materializer.materialize({**seed_candidate(), "src_patch": patch})
    assert refs(world) == []


def test_names_that_only_resemble_the_guarded_macros_are_kept(world) -> None:
    materializer = Materializer(world.repo, world.seed_sha, "run1", world.tmp / "work")
    lines = ("#[cfg(feature = \"x\")]\npub fn gated() {}\n"
             "#[cfg_attr(feature = \"x\", derive(Debug))]\npub struct S;\n"
             "pub fn included(include_count: usize) -> usize { include_count }\n")
    sha = materializer.materialize({**seed_candidate(), "src_patch": patch_for(world, {"src/lib.rs": LIB + lines})})
    assert sha != world.seed_sha


def test_rejects_a_changed_path_with_spaces(world) -> None:
    materializer = Materializer(world.repo, world.seed_sha, "run1", world.tmp / "work")
    patch = patch_for(world, {"src/x b/evil.rs": 'pub const P: &str = include_str!("../../../Cargo.toml");\n'})

    with pytest.raises(ApplyRejected, match=r"src/x b/evil\.rs:1"):
        materializer.materialize({**seed_candidate(), "src_patch": patch})
    assert refs(world) == []


@pytest.mark.parametrize("expression", ["cfg!(test)", "cfg!(any(test, debug_assertions))", "cfg ! [all(unix, test)]",
                                        "cfg!(debug_assertions)", "cfg!(not(debug_assertions))",
                                        "cfg!(any(unix, debug_assertions))"])
def test_rejects_cfg_macro_naming_test(world, expression: str) -> None:
    materializer = Materializer(world.repo, world.seed_sha, "run1", world.tmp / "work")
    patch = patch_for(world, {"src/lib.rs": LIB + f"pub fn gated() -> bool {{ {expression} }}\n"})

    with pytest.raises(ApplyRejected, match=r"cfg!"):
        materializer.materialize({**seed_candidate(), "src_patch": patch})
    assert refs(world) == []


def test_cfg_macro_without_test_is_kept(world) -> None:
    materializer = Materializer(world.repo, world.seed_sha, "run1", world.tmp / "work")
    lines = 'pub fn unix() -> bool { cfg!(unix) }\npub const S: &str = "cfg!(test)"; // cfg!(test)\n'
    sha = materializer.materialize({**seed_candidate(), "src_patch": patch_for(world, {"src/lib.rs": LIB + lines})})
    assert sha != world.seed_sha


@pytest.mark.parametrize("attribute", ["# [cfg(test)]", "#/**/[cfg(test)]", "#\n[cfg(test)]", "# ! [cfg(test)]",
                                       "#!/**/[cfg(test)]", "# [ cfg_attr(test, inline) ]"],
                         ids=["spaced", "comment", "newline", "spaced-inner", "comment-inner", "cfg-attr"])
def test_rejects_cfg_test_attribute_with_whitespace_or_comment(world, attribute: str) -> None:
    materializer = Materializer(world.repo, world.seed_sha, "run1", world.tmp / "work")
    patch = patch_for(world, {"src/lib.rs": LIB + attribute + "\npub fn gated() {}\n"})

    with pytest.raises(ApplyRejected, match=r"#\[cfg\(test\)\]"):
        materializer.materialize({**seed_candidate(), "src_patch": patch})
    assert refs(world) == []


@pytest.mark.parametrize("attribute", [
    "#[cfg(debug_assertions)]", "#[cfg(not(debug_assertions))]", "#[cfg(all(unix, debug_assertions))]",
    "#[cfg_attr(debug_assertions, inline)]", "# [cfg(debug_assertions)]",
])
def test_rejects_cfg_debug_assertions_item(world, attribute: str) -> None:
    materializer = Materializer(world.repo, world.seed_sha, "run1", world.tmp / "work")
    patch = patch_for(world, {"src/lib.rs": LIB + attribute + "\npub fn gated() {}\n"})

    with pytest.raises(ApplyRejected, match="debug_assertions"):
        materializer.materialize({**seed_candidate(), "src_patch": patch})
    assert refs(world) == []


@pytest.mark.parametrize("separator", ["\x0b", "\x0c", "\x1c", "\x85", " "])
def test_line_numbers_count_only_newlines(world, separator: str) -> None:
    materializer = Materializer(world.repo, world.seed_sha, "run1", world.tmp / "work")
    patch = patch_for(world, {"src/lib.rs": LIB + f"// a{separator}b\n// benchmark\n"})

    with pytest.raises(ApplyRejected, match=r"src/lib\.rs:8 names"):
        materializer.materialize({**seed_candidate(), "src_patch": patch})
    assert refs(world) == []


def _special_file_patch(world, kind: str) -> str:
    """A diff that adds a symlink under ``src/``, or turns ``src/lib.rs`` into a symlink."""
    scratch = world.tmp / f"scratch-special-{kind}"
    git("worktree", "add", "-q", "--detach", str(scratch), world.seed_sha, cwd=world.repo)
    name = "src/lib.rs" if kind == "type-change" else "src/link.rs"
    (scratch / name).unlink(missing_ok=True)
    (scratch / name).symlink_to("main.rs")
    git("add", "-A", cwd=scratch)
    diff = git("diff", "--cached", world.seed_sha, cwd=scratch)
    git("worktree", "remove", "--force", str(scratch), cwd=world.repo)
    return diff


@pytest.mark.parametrize("kind", ["symlink", "type-change"])
def test_rejects_symlink_under_src(world, kind: str) -> None:
    materializer = Materializer(world.repo, world.seed_sha, "run1", world.tmp / "work")
    patch = _special_file_patch(world, kind)
    assert "120000" in patch

    with pytest.raises(ApplyRejected, match="symlink or submodule"):
        materializer.materialize({**seed_candidate(), "src_patch": patch})
    assert refs(world) == []


# --- subscription auth guard: budget files, init-event gate, preflight ---


def _budget_file(name: str) -> Path:
    return Path(os.environ["TILTH_BENCH_DATA"]) / "budgets" / f"{name}.jsonl"


def _budget_records(name: str) -> list[dict]:
    return [json.loads(line) for line in _budget_file(name).read_text().splitlines()]


def _status_spawn(calls: list[dict], method: str = "oauth_token"):
    def spawn(argv, **kwargs):
        calls.append({"argv": list(argv), **kwargs})
        return subprocess.CompletedProcess(argv, 0, json.dumps({"authMethod": method}), "")
    return spawn


_ASSISTANT_FIRST = "\n".join([
    json.dumps({"type": "assistant", "message": {"id": "m1", "content": []}}),
    json.dumps({"type": "system", "subtype": "init", "apiKeySource": "none"}),
    json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "x", "total_cost_usd": 0.4}),
]) + "\n"
_API_KEY_INIT = "\n".join([
    json.dumps({"type": "system", "subtype": "init", "apiKeySource": "ANTHROPIC_API_KEY"}),
    json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "x", "total_cost_usd": 0.4}),
]) + "\n"


def test_budget_id_seeds_and_appends_the_budget_file(world) -> None:
    path = _budget_file("shared")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"run_key": None, "charged_usd": 1.5, "cost_source": "earlier"}) + "\n")

    evo = build(world, "--budget-id", "shared")

    assert evo.ledger.spent == pytest.approx(1.5)
    assert evo.paid.call("reflection", ["claude", "-p"], "prompt", world.tmp) is not None
    records = _budget_records("shared")
    assert len(records) == 2 and records[1]["charged_usd"] == pytest.approx(0.01)
    assert records[1]["cost_source"] == "reflection:native"
    assert evo.ledger.spent == pytest.approx(1.51)


def test_no_budget_id_writes_no_budget_file(world) -> None:
    evo = build(world)
    evo.paid.call("reflection", ["claude", "-p"], "prompt", world.tmp)

    assert evo.ledger.spent == pytest.approx(0.01)
    assert not (Path(os.environ["TILTH_BENCH_DATA"]) / "budgets").exists()


@pytest.mark.parametrize("budget_id", ["../escape", "a/b", "", ".hidden"])
def test_bad_budget_id_is_refused(world, budget_id: str) -> None:
    assert main(world, "--budget-id", budget_id) == 2
    assert world.spawned == [] and world.calls == []


@pytest.mark.parametrize("stream", [_ASSISTANT_FIRST, _API_KEY_INIT], ids=["assistant-before-init", "api-key-source"])
@pytest.mark.parametrize("persistent", [False, True], ids=["per-process", "budget-file"])
def test_disallowed_stream_is_charged_and_stops_the_run(world, stream: str, persistent: bool) -> None:
    def spawn(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stream, "")

    evo = build(world, *(["--budget-id", "auth-stop"] if persistent else []), spawn=spawn)

    assert evo.paid.call("reflection", ["claude", "-p"], "prompt", world.tmp) is None

    assert evo.stop.reason == "auth"
    assert evo.ledger.charges == [(pytest.approx(0.4), "reflection:native")]
    if persistent:
        assert [r["charged_usd"] for r in _budget_records("auth-stop")] == [pytest.approx(0.4)]
    assert evo.paid.call("reflection", ["claude", "-p"], "prompt", world.tmp) is None
    assert len(evo.ledger.charges) == 1


def test_build_runs_one_status_call_per_distinct_target(world) -> None:
    statuses: list[dict] = []

    evo = build(world, auth_spawn=_status_spawn(statuses))

    assert not isinstance(evo, int)
    assert all(call["argv"] == ["claude", "auth", "status"] for call in statuses)
    # The isolated call shape and the shared cell target are the two distinct targets of three cells.
    assert len(statuses) == 2
    configs = {call["env"]["CLAUDE_CONFIG_DIR"] for call in statuses}
    assert len(configs) == 2
    isolated = [c for c in configs if Path(c).name.startswith("tilth-auth-")]
    assert len(isolated) == 1 and Path(isolated[0]).parent == Path(tempfile.gettempdir())
    assert world.spawned == [] and world.calls == []


def test_non_oauth_status_refuses_before_any_paid_call(world) -> None:
    statuses: list[dict] = []
    spawn = _status_spawn(statuses, method="claude.ai")

    assert build(world, auth_spawn=spawn) == 2
    assert main(world, auth_spawn=spawn) == 2

    assert statuses and world.spawned == [] and world.calls == [] and world.builds == []


def test_missing_oauth_token_is_refused_before_any_paid_call(world, monkeypatch: pytest.MonkeyPatch) -> None:
    statuses: list[dict] = []
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")

    assert main(world, auth_spawn=_status_spawn(statuses)) == 2

    assert statuses == [] and world.spawned == [] and world.calls == []


# --- a definitively rejected candidate's kept build is cleared ---


def keep_builds(world, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake builds that leave a kept ``candidates/<sha>`` dir like the real build; ``BUILDFAIL`` text fails after it."""
    real = world.fake_build

    def build_candidate(sha: str, repo: Path | None = None) -> run.CandidateBuild:
        keep(sha)
        run.candidate_target_dir().mkdir(parents=True, exist_ok=True)
        if "BUILDFAIL" in git("show", f"{sha}:prompts/mcp.md", cwd=world.repo):
            raise RuntimeError(f"cargo build --release --locked failed at {sha}:\nerror[E0308]")
        return real(sha, repo)

    monkeypatch.setattr(run, "_build_candidate", build_candidate)


def keep(sha: str) -> None:
    (run.RESULTS_DIR / "candidates" / sha).mkdir(parents=True, exist_ok=True)
    (run.RESULTS_DIR / "candidates" / sha / "tilth").write_text("binary")


def kept(sha: str) -> bool:
    return (run.RESULTS_DIR / "candidates" / sha).exists()


def test_cheap_tier_and_build_failures_clear_the_candidate_build(world, monkeypatch: pytest.MonkeyPatch) -> None:
    keep_builds(world, monkeypatch)
    evo = ready(world)
    cheap = child(evo.seed, dev_a="1", dev_b="1", cheap_a="0")
    broken = child(evo.seed, dev_a="1", dev_b="1", cheap_a="1", tag="BUILDFAIL")

    assert evo.evaluate(cheap, "dev_a")[1]["stage"] == "cheap tier"
    assert evo.evaluate(broken, "dev_a")[1]["stage"] == "build"

    for candidate in (cheap, broken):
        assert not kept(evo.results[candidates.content_id(candidate)].sha)
    assert kept(world.seed_sha) and run.candidate_target_dir().is_dir()


def test_dominated_after_one_rollout_clears_the_candidate_build(world, monkeypatch: pytest.MonkeyPatch) -> None:
    keep_builds(world, monkeypatch)
    evo = ready(world)
    evo.evaluate(evo.seed, "dev_a")
    dominated = child(evo.seed, dev_a="0", dev_b="1", cheap_a="1")
    evo.evaluate(dominated, "dev_a")
    result = evo.results[candidates.content_id(dominated)]

    assert "dominated after one rollout" in log_text(world)
    assert not kept(result.sha) and kept(world.seed_sha)


def test_dominated_after_reruns_clears_the_candidate_build(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = weak_world(monkeypatch, tmp_path)
    keep_builds(world, monkeypatch)
    evo = ready(world)
    evo.evaluate(evo.seed, "dev_a")
    member = accept(evo, child(evo.seed, tag="member"), {"dev_a": 1.0, "dev_b": 0.5})
    keep(member.sha)
    candidate = child(evo.seed, dev_a="10", dev_b="10", cheap_a="1")
    evo.evaluate(candidate, "dev_a")
    result = evo.results[candidates.content_id(candidate)]

    assert "dominated after 2 rollouts" in log_text(world)
    assert not kept(result.sha) and kept(member.sha) and kept(world.seed_sha)


def test_evicted_frontier_member_clears_its_build_but_not_the_seed(monkeypatch: pytest.MonkeyPatch,
                                                                tmp_path: Path) -> None:
    world = weak_world(monkeypatch, tmp_path)
    keep_builds(world, monkeypatch)
    evo = ready(world)
    evo.evaluate(evo.seed, "dev_a")
    member = accept(evo, child(evo.seed, tag="member"), {"dev_a": 0.5, "dev_b": 0.5})
    keep(member.sha)
    winner = child(evo.seed, dev_a="11", dev_b="11", cheap_a="1")
    evo.evaluate(winner, "dev_a")
    result = evo.results[candidates.content_id(winner)]

    assert result.accepted and member not in evo.frontier and evo.seed_id not in {m.cid for m in evo.frontier}
    assert not kept(member.sha)
    assert kept(world.seed_sha) and kept(result.sha)


def test_rejection_never_clears_the_seed_or_a_frontier_members_build(world, monkeypatch: pytest.MonkeyPatch) -> None:
    keep_builds(world, monkeypatch)
    evo = ready(world)
    evo.evaluate(evo.seed, "dev_a")
    member = accept(evo, child(evo.seed, tag="member"), {"dev_a": 1.0, "dev_b": 1.0})
    keep(member.sha)
    keep(world.seed_sha)
    twin = evo.new_cascade(child(evo.seed, tag="twin"))

    twin.sha = world.seed_sha
    evo._fail(twin, "build")
    twin.sha = member.sha
    evo._fail(twin, "build")

    assert kept(world.seed_sha) and kept(member.sha)


def test_clearing_a_build_fails_soft(world, monkeypatch: pytest.MonkeyPatch) -> None:
    evo = ready(world)
    monkeypatch.setattr(run, "discard_candidate_build", lambda _sha: (_ for _ in ()).throw(OSError("busy")))
    twin = evo.new_cascade(child(evo.seed, tag="twin"))
    twin.sha = "a" * 40

    assert evo._fail(twin, "build").stage == "build"
    assert "could not clear its build: busy" in log_text(world)


def test_incomplete_rejections_keep_the_candidate_build(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = weak_world(monkeypatch, tmp_path)
    keep_builds(world, monkeypatch)
    evo = ready(world)
    evo.evaluate(evo.seed, "dev_a")
    first = child(evo.seed, dev_a="E", dev_b="1", cheap_a="1")
    rerun = child(evo.seed, dev_a="1E", dev_b="11", cheap_a="1")
    evo.evaluate(first, "dev_a")
    evo.evaluate(rerun, "dev_a")

    assert "a first rollout did not complete" in log_text(world)
    assert "a re-run did not complete" in log_text(world)
    for candidate in (first, rerun):
        assert kept(evo.results[candidates.content_id(candidate)].sha)


# --- the score is cost per correct: p * base / c ---


def row(task: str, correct: bool, **fields) -> dict:
    return {"task": task, "correct": correct, "contaminated": False, "repetition": 0, "run_key": "k", **fields}


def test_task_score_is_pass_rate_times_baseline_over_mean_cost() -> None:
    rows = [row("t", True, total_cost_usd=0.2), row("t", False, total_cost_usd=0.4), row("u", True, total_cost_usd=9.0)]

    assert task_score(rows, "t", 0.15) == pytest.approx(0.5 * 0.15 / 0.3)


def test_task_score_is_zero_when_never_solved_or_contaminated() -> None:
    unsolved = [row("t", False, total_cost_usd=0.2)]
    contaminated = [row("t", True, contaminated=True, total_cost_usd=0.2)]

    assert task_score(unsolved, "t", 0.1) == 0.0 and task_score(contaminated, "t", 0.1) == 0.0
    assert task_score([], "t", 0.1) == 0.0


def test_task_score_costs_a_failed_cell_at_its_charged_usd() -> None:
    rows = [row("t", True, total_cost_usd=0.1), row("t", False, charged_usd=0.3)]

    assert task_score(rows, "t", 0.1) == pytest.approx(0.5 * 0.1 / 0.2)


def test_task_score_stops_on_a_row_with_no_numeric_cost() -> None:
    with pytest.raises(EvolveError, match="a finite, non-negative cost is required"):
        task_score([row("t", False, total_cost_usd=None)], "t", 0.1)
    with pytest.raises(EvolveError, match="a finite, non-negative cost is required"):
        task_score([row("t", True, total_cost_usd="0.1")], "t", 0.1)


def test_task_score_stops_on_a_solved_task_with_no_positive_cost() -> None:
    with pytest.raises(EvolveError):
        task_score([row("t", True, total_cost_usd=0.0)], "t", 0.1)


def cascade_at_scale(world, evo, candidate: dict, scale: float) -> Cascade:
    """Evaluate ``candidate`` whose cells cost ``scale`` times the baseline arm's."""
    sha = evo.materializer.materialize(candidate)
    evo.new_cascade(candidate).sha = sha
    world.sha_scale[sha] = scale
    return evo.cascade(candidate)


SAME_PASS = dict(dev_a="1", dev_b="1", cheap_a="1", test_a="0")


def test_cheaper_candidate_with_equal_pass_rates_dominates_the_seed(world) -> None:
    evo = ready(world)
    seed = evo.cascade(evo.seed)
    cheaper = cascade_at_scale(world, evo, child(evo.seed, tag="cheaper", **SAME_PASS), 0.8)

    assert dominates(cheaper.means, seed.means, DEV) and not dominates(seed.means, cheaper.means, DEV)
    assert cheaper.accepted and evo.frontier == [cheaper]
    assert all(seed.means[task] == pytest.approx(1.0) and cheaper.means[task] == pytest.approx(1.25) for task in DEV)


def test_costlier_candidate_with_equal_pass_rates_scores_below_the_seed(world) -> None:
    evo = ready(world)
    seed = evo.cascade(evo.seed)
    costlier = cascade_at_scale(world, evo, child(evo.seed, tag="costlier", **SAME_PASS), 1.5)

    assert costlier.cheap_score == pytest.approx(1 / 1.5) and not costlier.accepted
    assert evo.frontier == [seed] and seed.cheap_score == pytest.approx(1.0)


def test_evaluator_returns_the_score_and_side_info_carries_the_cost_fields(world) -> None:
    evo = ready(world)
    evo.cascade(evo.seed)
    cheaper = child(evo.seed, tag="cheaper", **SAME_PASS)
    sha = evo.materializer.materialize(cheaper)
    evo.new_cascade(cheaper).sha = sha
    world.sha_scale[sha] = 0.8

    score, info = evo.evaluate(cheaper, "dev_a")

    assert score == pytest.approx(1.25)
    assert info["task_scores"] == pytest.approx({"dev_a": 1.25, "dev_b": 1.25})
    assert info["pass_rate"] == {"dev_a": 1.0, "dev_b": 1.0}
    assert info["mean_cost"] == pytest.approx({"dev_a": 0.08, "dev_b": 0.08})
    assert info["baseline_cost"] == pytest.approx({"dev_a": 0.1, "dev_b": 0.1})


def test_cheap_score_is_the_mean_of_the_per_task_cheap_scores(world) -> None:
    evo = ready(world)
    evo.cascade(evo.seed)
    cheaper = cascade_at_scale(world, evo, child(evo.seed, tag="cheaper", **SAME_PASS), 0.8)

    assert cheaper.cheap_scores == pytest.approx({"cheap_a": 1.25})
    assert cheaper.cheap_score == pytest.approx(1.25)


def finish_world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, winner_scale: float, winner_tests: tuple[str, ...]):
    """Three test tasks, a seed passing all of them, and a winner at ``winner_scale`` of the seed's cost."""
    world = three_test_task_world(monkeypatch, tmp_path, seed_test="1")
    evo = ready(world)
    accept(evo, evo.seed, {"dev_a": 0.0, "dev_b": 0.0})
    winner = accept(evo, child(evo.seed, test_a=winner_tests[0], test_b=winner_tests[1], test_c=winner_tests[2],
                               tag="w"), {"dev_a": 0.9, "dev_b": 0.9})
    world.sha_scale[winner.sha] = winner_scale
    return world, evo, winner


def test_finish_accepts_a_cheaper_winner_with_equal_test_pass_rate(monkeypatch: pytest.MonkeyPatch,
                                                                  tmp_path: Path) -> None:
    world, evo, winner = finish_world(monkeypatch, tmp_path, 0.8, ("11", "11", "11"))

    assert evo.finish() is not None

    assert world.pr.pushes == [(winner.sha, "evolve/run1-winner")]
    body = world.pr.creates[0]["body"]
    assert "Winner minus seed, paired test delta +0.250" in body
    assert "Winner minus seed, test pass rate delta +0.000" in body


def test_finish_refuses_a_costlier_winner_with_equal_test_pass_rate(monkeypatch: pytest.MonkeyPatch,
                                                                   tmp_path: Path) -> None:
    world, evo, _winner = finish_world(monkeypatch, tmp_path, 1.5, ("11", "11", "11"))

    assert evo.finish() is None

    assert world.pr.calls == [] and evo.finish_failure is None
    assert "the paired test-split interval does not exclude 0" in log_text(world)


def test_finish_refuses_a_cheaper_winner_with_a_lower_test_pass_rate(monkeypatch: pytest.MonkeyPatch,
                                                                    tmp_path: Path) -> None:
    world, evo, _winner = finish_world(monkeypatch, tmp_path, 0.1, ("11", "11", "10"))

    assert evo.finish() is None

    text = log_text(world)
    assert world.pr.calls == [] and evo.finish_failure is None
    assert "finish: paired test delta +" in text and "test pass rate delta -0.167" in text
    assert "the paired test-split interval does not exclude 0" not in text
    assert "finish: no improvement (the winner's test pass rate is below the seed's)" in text


@pytest.mark.parametrize("fault", ["unsolved baseline cell", "zero cost", "missing cost"])
def test_a_bad_baseline_row_stops_the_run_before_any_candidate_cell(world, monkeypatch: pytest.MonkeyPatch,
                                                                  fault: str) -> None:
    if fault == "unsolved baseline cell":
        world.baseline["test_a"] = "E"
    elif fault == "zero cost":
        world.costs["dev_a"] = 0.0
    else:
        real_run_plan = run.run_plan

        def stripped(*args, **kwargs):
            rows = real_run_plan(*args, **kwargs)
            for found in rows:
                if found["mode"] == "baseline" and found["task"] == "dev_a":
                    found.pop("total_cost_usd", None)
            return rows

        monkeypatch.setattr(run, "run_plan", stripped)
    evo = build(world)
    evo.base_cost = {}

    assert evo.buy_baselines() is not None

    assert tilth_calls(world) == [] and evo.base_cost == {}


def test_a_candidate_row_with_no_numeric_cost_stops_scoring(world, monkeypatch: pytest.MonkeyPatch) -> None:
    evo = ready(world)
    real_run_plan = run.run_plan

    def costless(*args, **kwargs):
        rows = real_run_plan(*args, **kwargs)
        for found in rows:
            found.pop("total_cost_usd", None)
            found.pop("charged_usd", None)
        return rows

    monkeypatch.setattr(run, "run_plan", costless)
    with pytest.raises(EvolveError, match="a finite, non-negative cost is required"):
        evo.evaluate(child(evo.seed, tag="fresh", **SAME_PASS), "dev_a")


def test_a_candidate_row_without_total_cost_is_costed_at_charged_usd(world, monkeypatch: pytest.MonkeyPatch) -> None:
    evo = ready(world)
    real_run_plan = run.run_plan

    def charged(*args, **kwargs):
        rows = real_run_plan(*args, **kwargs)
        for found in rows:
            found["charged_usd"] = found.pop("total_cost_usd") / 2
        return rows

    monkeypatch.setattr(run, "run_plan", charged)

    assert evo.cascade(evo.seed).scores == pytest.approx({"dev_a": 2.0, "dev_b": 2.0})
