"""Chained cells: several tasks as turns of one claude session, graded one by one."""

import contextlib
import io
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import chains
import panels
import run
from evolve.loop import task_score
from spend import SpendLedger
from tasks.base import GroundTruth, Task, TaskSource

ANSWERS = {"c_one": "alpha", "c_two": "beta", "c_three": "gamma"}


@dataclass
class Member(Task):
    """A chain member graded by the default substring grader."""

    name: str = ""
    repo: str = "synthetic"
    prompt: str = ""
    capability: str = "trace"
    hide_git: bool = False
    ground_truth: GroundTruth | None = None
    muts: list = field(default_factory=list)
    source: TaskSource = TaskSource(origin="fixture", license="MIT", commit_or_tag="test-pin", transformation="test-only")

    def __post_init__(self) -> None:
        if self.ground_truth is None:
            self.ground_truth = GroundTruth(required_strings=[ANSWERS[self.name]])

    @property
    def mutations(self) -> list:
        return self.muts


@dataclass
class Mutation:
    file_path: str


class GlobalGrader(Member):
    def check_correctness(self, result_text: str, repo_path: str) -> tuple[bool, str]:
        return True, "workspace"


@pytest.fixture
def world(bench, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(run, "TASKS", dict(run.TASKS))
    for name in ANSWERS:
        run.TASKS[name] = Member(name=name, prompt=f"prompt {name}")
    monkeypatch.setitem(chains.CHAINS, "t3", chains.Chain("t3", ("c_one", "c_two", "c_three")))
    monkeypatch.setitem(chains.CHAINS, "t2", chains.Chain("t2", ("c_one", "c_two")))
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    @contextlib.contextmanager
    def agent_repo(_path, _hide_git):
        yield workspace

    monkeypatch.setattr(run, "_agent_repo", agent_repo)
    monkeypatch.setattr(run, "get_repo_path", lambda _repo: workspace)
    run.select_tasks("chain:t3,chain:t2")
    return bench


def stream(session: str, text: str, cost: float) -> str:
    usage = {"input_tokens": 100, "output_tokens": 20, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    events = [
        {"type": "system", "subtype": "init", "session_id": session, "model": "claude-sonnet-5", "tools": ["Read"],
         "apiKeySource": "none",
         "mcp_servers": []},
        {"type": "assistant", "message": {"id": f"{session}-1", "model": "claude-sonnet-5", "role": "assistant",
                                         "usage": usage, "content": [{"type": "text", "text": "looking"}]}},
        {"type": "assistant", "message": {"id": f"{session}-2", "model": "claude-sonnet-5", "role": "assistant",
                                         "usage": usage, "content": [{"type": "text", "text": text}]}},
        {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 1000, "duration_api_ms": 900,
         "num_turns": 2, "result": text, "session_id": session, "usage": usage, "total_cost_usd": cost},
    ]
    return "\n".join(json.dumps(event) for event in events) + "\n"


class Replay:
    def __init__(self, text: str, returncode: int = 0) -> None:
        self.stdout = io.StringIO(text)
        self.stderr = io.StringIO("")
        self.returncode = returncode

    def __enter__(self) -> "Replay":
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def wait(self, *_args, **_kwargs) -> int:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


class Script:
    """Replays one canned turn per claude call and logs prompts and grading in order."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, turns: list[tuple[str, str, float]], returncodes=()) -> None:
        self.turns = list(turns)
        self.returncodes = list(returncodes)
        self.argvs: list[list[str]] = []
        self.events: list[str] = []
        self.real_popen = run.subprocess.Popen
        monkeypatch.setattr(run.subprocess, "Popen", self.popen)
        original = Member.check_correctness

        def graded(member, result_text, repo_path):
            self.events.append(f"grade:{member.name}")
            return original(member, result_text, repo_path)

        monkeypatch.setattr(Member, "check_correctness", graded)

    def popen(self, argv, *args, **kwargs):
        argv = [str(part) for part in argv]
        if Path(argv[0]).name != "claude":
            return self.real_popen(argv, *args, **kwargs)
        self.argvs.append(argv)
        self.events.append(f"prompt:{argv[-1]}")
        session, text, cost = self.turns.pop(0)
        code = self.returncodes.pop(0) if self.returncodes else 0
        return Replay(stream(session, text, cost), code)


def run_chain(name: str = "chain:t3", budget: float = 2.0, log: Path | None = None) -> dict:
    run.select_tasks(name)
    log = log or Path(tempfile.mkdtemp()) / "cell.jsonl"
    return run.run_single(name, "plain", "sonnet5", 0, max_budget_usd=budget, stream_log_path=log)


def flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


GOOD = [("s1", "alpha", 0.10), ("s1", "beta", 0.20), ("s1", "gamma", 0.30)]


# --- parsing and validation ---


def test_a_chain_resolves_its_members_in_order(world) -> None:
    members = chains.parse_chain("t3", ("c_one", "c_two", "c_three"), run.TASKS)

    assert [member.name for member in members] == ["c_one", "c_two", "c_three"]


def test_an_unknown_member_is_refused(world) -> None:
    with pytest.raises(chains.ChainError, match="unknown task nope"):
        chains.parse_chain("bad", ("c_one", "nope"), run.TASKS)


def test_members_on_different_repos_are_refused(world) -> None:
    run.TASKS["c_two"] = Member(name="c_two", repo="gin")

    with pytest.raises(chains.ChainError, match="several repos"):
        chains.parse_chain("bad", ("c_one", "c_two"), run.TASKS)


@pytest.mark.parametrize("names", [("c_one",), ("c_one", "c_one")])
def test_a_chain_needs_two_distinct_tasks(world, names) -> None:
    with pytest.raises(chains.ChainError):
        chains.parse_chain("bad", names, run.TASKS)


def test_two_workspace_graders_are_refused(world) -> None:
    run.TASKS["c_one"] = GlobalGrader(name="c_one")
    run.TASKS["c_two"] = GlobalGrader(name="c_two")

    with pytest.raises(chains.ChainError, match="grade the whole workspace"):
        chains.parse_chain("bad", ("c_one", "c_two"), run.TASKS)


def test_a_workspace_grader_cannot_share_a_chain_with_mutations(world) -> None:
    run.TASKS["c_one"] = GlobalGrader(name="c_one")
    run.TASKS["c_two"] = Member(name="c_two", muts=[Mutation("a.py")])

    with pytest.raises(chains.ChainError, match="no task may carry mutations"):
        chains.parse_chain("bad", ("c_one", "c_two"), run.TASKS)


def test_members_that_mutate_one_file_are_refused(world) -> None:
    run.TASKS["c_one"] = Member(name="c_one", muts=[Mutation("a.py")])
    run.TASKS["c_two"] = Member(name="c_two", muts=[Mutation("a.py")])

    with pytest.raises(chains.ChainError, match="both mutate a.py"):
        chains.parse_chain("bad", ("c_one", "c_two"), run.TASKS)


def test_every_committed_chain_composes() -> None:
    for chain in chains.CHAINS.values():
        chains.parse_chain(chain.id, chain.members, run.TASKS)


# --- selection ---


def test_select_tasks_registers_a_chain(world) -> None:
    assert run.select_tasks("chain:t3") == ["chain:t3"]
    assert isinstance(run.TASKS["chain:t3"], chains.ChainTask)


def test_select_tasks_refuses_an_unknown_chain(world) -> None:
    with pytest.raises(ValueError, match="unknown chain: nope"):
        run.select_tasks("chain:nope")


def test_all_leaves_chains_out(world) -> None:
    run.select_tasks("chain:t3")

    assert "chain:t3" not in run.select_tasks("all")


# --- run keys ---


def key(name: str, **kwargs) -> str:
    return run.cell_identity(name, "plain", "sonnet5", 0, bare=False, reasoning_effort=None,
                             max_budget_usd=2.0, strict_file_tools=False, **kwargs)["run_key"]


def test_chain_keys_differ_from_each_other_and_from_members(world) -> None:
    run.select_tasks("chain:t3,chain:t2")

    keys = {key(name) for name in ("chain:t3", "chain:t2", "c_one", "c_two", "c_three")}

    assert len(keys) == 5


def test_changing_a_member_changes_the_chain_key(world) -> None:
    run.select_tasks("chain:t2")
    before = key("chain:t2")
    del run.TASKS["chain:t2"]
    run.TASKS["c_two"] = Member(name="c_two", prompt="another prompt")
    run.select_tasks("chain:t2")

    assert key("chain:t2") != before


def test_a_member_key_does_not_depend_on_chains(world) -> None:
    before = key("c_one")
    run.select_tasks("chain:t3")

    assert key("c_one") == before


def test_editing_chains_py_leaves_a_chain_key_alone(world, monkeypatch) -> None:
    run.select_tasks("chain:t2")
    before = key("chain:t2")
    real = run._file_sha256
    monkeypatch.setattr(run, "_file_sha256", lambda path: "edited" if Path(path).name == "chains.py" else real(path))

    assert key("chain:t2") == before


def test_bumping_the_chain_protocol_changes_the_chain_key(world, monkeypatch) -> None:
    run.select_tasks("chain:t2")
    before = key("chain:t2")
    monkeypatch.setattr(chains, "CHAIN_PROTOCOL", chains.CHAIN_PROTOCOL + 1)

    assert key("chain:t2") != before


# --- one session, graded turn by turn ---


def test_turns_resume_one_session(world, monkeypatch, tmp_path) -> None:
    script = Script(monkeypatch, GOOD)

    run_chain()

    first, second, third = script.argvs
    assert "--resume" not in first
    assert flag(second, "--resume") == "s1"
    assert flag(third, "--resume") == "s1"


def test_a_rotated_session_id_is_followed(world, monkeypatch) -> None:
    script = Script(monkeypatch, [("s1", "alpha", 0.1), ("s2", "beta", 0.1), ("s2", "gamma", 0.1)])

    run_chain()

    assert flag(script.argvs[1], "--resume") == "s1"
    assert flag(script.argvs[2], "--resume") == "s2"


def test_chain_turns_keep_the_session_and_single_cells_do_not(world, monkeypatch) -> None:
    script = Script(monkeypatch, GOOD + [("s9", "alpha", 0.1)])

    run_chain()
    run.run_single("c_one", "plain", "sonnet5", 0, stream_log_path=Path(tempfile.mkdtemp()) / "single.jsonl")

    assert all("--no-session-persistence" not in argv for argv in script.argvs[:3])
    assert "--no-session-persistence" in script.argvs[3]


def test_each_sub_task_is_graded_before_the_next_prompt(world, monkeypatch) -> None:
    script = Script(monkeypatch, GOOD)

    run_chain()

    assert script.events == [
        "prompt:prompt c_one", "grade:c_one",
        "prompt:prompt c_two", "grade:c_two",
        "prompt:prompt c_three", "grade:c_three",
    ]


def test_a_failing_sub_task_does_not_stop_the_later_ones(world, monkeypatch) -> None:
    script = Script(monkeypatch, [("s1", "alpha", 0.1), ("s1", "wrong", 0.1), ("s1", "gamma", 0.1)])

    row = run_chain()

    assert len(script.argvs) == 3
    assert [sub["correct"] for sub in row["sub_tasks"]] == [True, False, True]
    assert row["correct"] is False
    assert row["pass_rate"] == pytest.approx(2 / 3)


def test_a_crashing_turn_stops_the_chain(world, monkeypatch) -> None:
    script = Script(monkeypatch, GOOD, returncodes=[0, 1])

    with pytest.raises(Exception):
        run_chain()

    assert len(script.argvs) == 2


def test_each_turn_is_capped_at_what_the_chain_has_left(world, monkeypatch) -> None:
    script = Script(monkeypatch, GOOD)

    row = run_chain(budget=2.0)

    caps = [float(flag(argv, "--max-budget-usd")) for argv in script.argvs]
    assert caps == pytest.approx([6.0, 5.9, 5.7])
    assert row["chain_max_budget_usd"] == pytest.approx(6.0)
    assert row["max_budget_usd"] == 2.0


# --- the chain row ---


def test_the_chain_row_totals_the_conversation(world, monkeypatch, tmp_path) -> None:
    Script(monkeypatch, GOOD)
    log = tmp_path / "streams" / "chain.jsonl"

    row = run_chain(log=log)

    assert row["task"] == "chain:t3"
    assert row["chain_id"] == "t3"
    assert row["total_cost_usd"] == pytest.approx(0.6)
    assert [sub["task"] for sub in row["sub_tasks"]] == ["c_one", "c_two", "c_three"]
    assert [sub["cost_usd"] for sub in row["sub_tasks"]] == pytest.approx([0.1, 0.2, 0.3])
    assert row["api_calls"] == len(row["per_turn_context_tokens"]) == 6
    assert row["correct"] is True
    assert row["pass_rate"] == 1.0
    assert row["contaminated"] is False
    assert row["reused"] is False
    assert row["run_key"] == key("chain:t3")
    assert len(row["trajectory_paths"]) == 3
    assert log.read_text().count('"type": "result"') == 3


def test_a_chain_row_scores_through_the_existing_path(world, monkeypatch) -> None:
    Script(monkeypatch, [("s1", "alpha", 0.1), ("s1", "wrong", 0.1), ("s1", "gamma", 0.1)])
    failed = run_chain()
    Script(monkeypatch, GOOD)
    passed = run_chain()

    assert task_score([passed], "chain:t3", 0.6) == pytest.approx(1.0)
    assert task_score([failed], "chain:t3", 0.6) == 0.0


def test_a_non_claude_model_cannot_chain(world, monkeypatch) -> None:
    Script(monkeypatch, GOOD)
    model = next(name for name, runner in run.RUNNERS.items() if runner != "claude")

    run.select_tasks("chain:t3")

    with pytest.raises(ValueError, match="Claude"):
        run.run_single("chain:t3", "plain", model, 0)


# --- run_plan, panels, CLI ---


class ChainPanel:
    name = "chain-panel"

    def register(self, tasks) -> None:
        pass

    def stamp(self, task: str) -> dict:
        return {}


def test_run_plan_runs_a_chain_cell(world) -> None:
    world.runner(lambda _stream: 0.1)

    [row] = run.run_plan([run.CellSpec("chain:t3", "plain", "sonnet5", 0)], panel=ChainPanel(),
                         ledger=SpendLedger(100.0), candidate_sha=None, refreeze_baselines=False)

    assert world.calls == [("chain:t3", "plain", 0)]
    assert row["task"] == "chain:t3"
    assert world.stored_rows()[0]["run_key"] == run.planned_identity(
        run.CellSpec("chain:t3", "plain", "sonnet5", 0))["run_key"]


def test_a_panel_takes_a_chain_member(world) -> None:
    member = panels.Member(id="chain:t3", family="chain", language="python", split="dev")
    panel = panels.Panel(name="p", cheap=(), dev=("chain:t3",), test=(), split_digest="x", members=(member,))
    tasks = dict(run.TASKS)

    panel.register(tasks)

    assert isinstance(tasks["chain:t3"], chains.ChainTask)


def test_the_cli_accepts_a_chain_and_dry_run_calls_no_model(world, monkeypatch, capsys) -> None:
    script = Script(monkeypatch, GOOD)

    code = world.main("--tasks", "chain:t3", "--modes", "plain", "--models", "sonnet5", "--reps", "1", "--dry-run")

    out = capsys.readouterr().out
    assert code == 0
    assert script.argvs == []
    assert "chain:t3" in out and "c_one, c_two, c_three" in out
    assert "budget $" in out


def test_the_cli_refuses_an_unknown_chain(world, capsys) -> None:
    code = world.main("--tasks", "chain:nope", "--modes", "plain", "--models", "sonnet5", "--reps", "1", "--dry-run")

    assert code != 0
    assert "unknown chain" in capsys.readouterr().err


def test_a_single_task_is_not_a_chain(world) -> None:
    assert not isinstance(run.TASKS["cell_a"], chains.ChainTask)
