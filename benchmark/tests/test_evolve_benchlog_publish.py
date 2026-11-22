"""Each evolve run opens its own PR that adds its entry to the benchmark log.

The tests run real git against a temp checkout whose ``origin`` is a local bare repository. A recorder stands in
for ``gh``. No test reaches the network, pushes to GitHub, or opens a real PR.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import run
from evolve import benchlog, chart, engine, logpr
from evolve_support import child, git, log_text, main, make_world, mcp, scripted_engine, seed_candidate, seed_files

# The real client, taken at import: the autouse conftest fixture replaces the module name with an offline one.
RealLogClient = logpr.GitHubLogClient
LOG = "benchmark/log/evolve.jsonl"
PR_URL = "https://example.invalid/log/1"
BASE_ENTRY = {"schema": 2, "run_id": "earlier", "recorded_at": "2026-11-01T10:00:00Z", "panel": "evolve-fixture",
              "model": "sonnet5", "seed_dev_mean": 0.5, "outcome": "no_improvement"}


class GhRecorder:
    """Stands in for ``subprocess.run`` of ``gh``: it records each call and prints a PR URL."""

    def __init__(self, open_url: str = "") -> None:
        self.calls: list[list[str]] = []
        self.open_url = open_url

    def __call__(self, argv, **_kwargs):
        self.calls.append(list(argv))
        out = self.open_url if argv[:3] == ["gh", "pr", "list"] else PR_URL
        return subprocess.CompletedProcess(argv, 0, out + "\n", "")

    def creates(self) -> list[list[str]]:
        return [call for call in self.calls if call[:3] == ["gh", "pr", "create"]]


def winning_world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    world = make_world(monkeypatch, tmp_path, seed_files(mcp(dev_a="0", dev_b="0", cheap_a="1", test_a="0")))
    fake, _seen = scripted_engine([child(seed_candidate(), dev_a="1", dev_b="1", cheap_a="1", test_a="1")])
    monkeypatch.setattr(engine, "optimize_anything", fake)
    return world


def with_origin(world) -> tuple[Path, str]:
    """A bare ``origin`` whose ``main`` holds a base log one commit ahead of the run checkout."""
    origin = world.tmp / "origin.git"
    git("init", "-q", "--bare", "-b", "main", str(origin), cwd=world.tmp)
    log = world.repo / LOG
    benchlog.append(log, BASE_ENTRY)
    benchlog.redraw(log)
    git("add", "benchmark/log", cwd=world.repo)
    git("commit", "-q", "-m", "base log", cwd=world.repo)
    git("remote", "add", "origin", str(origin), cwd=world.repo)
    git("push", "-q", "origin", "main", cwd=world.repo)
    base = git("rev-parse", "HEAD", cwd=world.repo).strip()
    # The run checkout lags origin: a publish must take the log from the fetched base, not from this tree.
    git("reset", "-q", "--hard", "HEAD~1", cwd=world.repo)
    return origin, base


def client_for(world, gh: GhRecorder) -> logpr.GitHubLogClient:
    return RealLogClient(world.repo, run=gh)


def show(origin: Path, ref: str, path: str) -> str:
    return git("show", f"{ref}:{path}", cwd=origin)


def run_entry_line(world) -> str:
    entry = json.loads((run.RESULTS_DIR / "evolve" / "run1" / benchlog.ENTRY_FILE).read_text())
    return json.dumps(entry, sort_keys=True) + "\n"


def temp_worktrees(world) -> list[str]:
    listed = git("worktree", "list", "--porcelain", cwd=world.repo).splitlines()
    return [line for line in listed if line.startswith("worktree ") and logpr.WORKTREE_PREFIX in line]


def test_a_default_run_opens_one_ready_pr_with_its_entry_on_the_fetched_base(monkeypatch: pytest.MonkeyPatch,
                                                                              tmp_path: Path) -> None:
    world = winning_world(monkeypatch, tmp_path)
    origin, base = with_origin(world)
    gh = GhRecorder()

    assert main(world, log_client=client_for(world, gh)) == 0

    branch = "refs/heads/benchmark-log/run1"
    assert git("rev-parse", f"{branch}^", cwd=origin).strip() == base
    base_log, pushed_log = show(origin, base, LOG), show(origin, branch, LOG)
    assert pushed_log == base_log + run_entry_line(world)
    entries = [json.loads(line) for line in pushed_log.splitlines()]
    assert show(origin, branch, "benchmark/log/evolve.svg") == chart.render(entries, "light")
    assert show(origin, branch, "benchmark/log/evolve-dark.svg") == chart.render(entries, "dark")
    changed = git("diff", "--name-only", base, branch, cwd=origin).split()
    assert changed == ["benchmark/log/evolve-dark.svg", LOG, "benchmark/log/evolve.svg"]
    [create] = gh.creates()
    assert "--draft" not in create
    assert create[create.index("--base") + 1] == "main"
    assert create[create.index("--head") + 1] == "benchmark-log/run1"
    assert not any("merge" in arg.lower() for call in gh.calls for arg in call)
    # The winner PR still goes through its own client, as a draft.
    assert world.pr.pushes and world.pr.creates[0]["draft"] is True
    assert git("status", "--porcelain", "--", "benchmark/log", cwd=world.repo) == ""
    assert not (world.repo / "benchmark" / "log").exists()
    assert temp_worktrees(world) == []
    assert f"benchmark log: opened {PR_URL} from benchmark-log/run1" in log_text(world)


FAULTS = ["fetch", "branch", "worktree", "append", "redraw", "commit", "push", "pr"]


@pytest.mark.parametrize("step", FAULTS)
def test_a_publish_fault_keeps_the_exit_code_the_entry_and_no_worktree(monkeypatch: pytest.MonkeyPatch,
                                                                       tmp_path: Path, step: str) -> None:
    world = winning_world(monkeypatch, tmp_path)
    origin, _base = with_origin(world)
    gh = GhRecorder()
    client = client_for(world, gh)

    def broken(*_args, **_kwargs):
        raise OSError(f"forced {step} fault")

    targets = {"fetch": (client, "fetch"), "push": (client, "push_log"), "pr": (client, "create_ready"),
               "worktree": (logpr, "_add_worktree"), "append": (benchlog, "append"),
               "redraw": (benchlog, "redraw"), "commit": (logpr, "_commit")}
    if step == "branch":
        git("push", "-q", "origin", "main:refs/heads/benchmark-log/run1", cwd=world.repo)
    else:
        monkeypatch.setattr(*targets[step], broken)

    assert main(world, log_client=client) == 0

    text = log_text(world)
    assert "finish: draft PR opened from evolve/run1-winner" in text
    [failure] = [line for line in text.splitlines() if line.startswith("benchmark log: ")]
    assert failure.startswith(f"benchmark log: {step} failed (")
    if step == "branch":
        assert "--replace" in failure
    assert (run.RESULTS_DIR / "evolve" / "run1" / benchlog.ENTRY_FILE).is_file()
    assert temp_worktrees(world) == []
    assert git("status", "--porcelain", "--", "benchmark/log", cwd=world.repo) == ""
    if step not in {"pr"}:
        assert gh.creates() == []


def test_publish_refuses_an_existing_branch_without_replace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    world = winning_world(monkeypatch, tmp_path)
    origin, base = with_origin(world)
    first = GhRecorder()
    assert main(world, log_client=client_for(world, first)) == 0
    old_tip = git("rev-parse", "refs/heads/benchmark-log/run1", cwd=origin).strip()
    run_dir = run.RESULTS_DIR / "evolve" / "run1"
    again = GhRecorder()

    assert benchlog.main(["publish", str(run_dir)], client=client_for(world, again), repo=world.repo) == 1

    assert "--replace" in capsys.readouterr().err
    assert git("rev-parse", "refs/heads/benchmark-log/run1", cwd=origin).strip() == old_tip
    assert again.creates() == []

    # Another log PR lands first: --replace rebuilds the run's branch on the new base and keeps the open PR.
    git("fetch", "-q", "origin", cwd=world.repo)
    git("checkout", "-q", "--detach", "origin/main", cwd=world.repo)
    benchlog.append(world.repo / LOG, {**BASE_ENTRY, "run_id": "landed", "recorded_at": "2026-11-02T10:00:00Z"})
    git("commit", "-q", "-am", "another log PR", cwd=world.repo)
    git("push", "-q", "origin", "HEAD:main", cwd=world.repo)
    new_base = git("rev-parse", "HEAD", cwd=world.repo).strip()
    replaced = GhRecorder(open_url=PR_URL)

    assert benchlog.main(["publish", str(run_dir), "--replace"], client=client_for(world, replaced),
                         repo=world.repo) == 0

    assert git("rev-parse", "refs/heads/benchmark-log/run1^", cwd=origin).strip() == new_base
    assert show(origin, "refs/heads/benchmark-log/run1", LOG) == show(origin, new_base, LOG) + run_entry_line(world)
    assert replaced.creates() == []
    assert temp_worktrees(world) == []
    assert base != new_base


def test_a_local_benchmark_log_opens_no_pr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = winning_world(monkeypatch, tmp_path)
    origin, _base = with_origin(world)
    gh = GhRecorder()
    log = tmp_path / "local" / "evolve.jsonl"

    assert main(world, "--benchmark-log", str(log), log_client=client_for(world, gh)) == 0

    assert gh.calls == []
    assert git("ls-remote", "--heads", str(origin), "benchmark-log/*", cwd=world.repo) == ""
    assert benchlog.load(log)[0]["run_id"] == "run1"


def test_the_chart_draws_the_last_entry_of_a_run_id(tmp_path: Path) -> None:
    """A resumed run publishes a second entry for its run id; the log keeps both lines, the chart one point."""
    first = {**BASE_ENTRY, "run_id": "resumed", "seed_dev_mean": 0.4, "outcome": "incomplete"}
    second = {**first, "recorded_at": "2026-11-02T10:00:00Z", "seed_dev_mean": 0.6, "outcome": "no_improvement"}
    log = tmp_path / "evolve.jsonl"
    benchlog.append(log, first)
    benchlog.append(log, second)

    light, _dark = benchlog.redraw(log)

    assert len(benchlog.load(log)) == 2
    assert light.read_text() == chart.render([second], "light")
    assert light.read_text().count("resumed: seed dev mean") == 1
