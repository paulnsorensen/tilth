"""Hermetic scaffolding for the evolution-loop tests: a tiny tilth-shaped repo, a fixture panel, stub cells and judge.

Candidate outcomes are written into the candidate itself: a ``score:`` line in
``prompts/mcp.md`` such as ``score: dev_a=10 cheap_a=1`` gives each task one
character per repetition (``1`` correct, ``0`` incorrect, ``C`` correct but
contaminated, ``E`` an errored cell; the last character repeats). The stub
runner reads the line from the commit the cell is served from, so no test
reaches a model, a network, or cargo.
"""

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import baselines
import panels
import run
from config import REPO_ROOT, ModeConfig
from judge import core as judge_core
from judge import config as judge_config
from judge.store import Agreement
from tasks.base import GroundTruth, TaskSource

CHEAP = ("cheap_a",)
DEV = ("dev_a", "dev_b")
TEST = ("test_a",)
LONG_OUTPUT = "src/dispatch.rs walks every handler table entry AGENT_GT " * 4
assert len(LONG_OUTPUT) > 80
MODEL = "sonnet5"
COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@example.com",
    "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@example.com",
}

TOOLS = {name: f"{name} tool description\n" for name in ("read", "search", "write")}


def git(*args: str, cwd: Path, input: str | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, input=input,
                          env={**os.environ, **COMMIT_ENV}).stdout


def rust_literal(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def mcp(**scores: str) -> str:
    """An instruction file whose ``score:`` line drives the stub runner; no trailing newline."""
    marks = " ".join(f"{task}={value}" for task, value in scores.items())
    return f"fixture instructions\nsecond line of guidance\nscore: {marks}\nDO NOT stop here."


def mod_rs(instructions: str) -> str:
    lead = "\n".join(instructions.split("\n")[:2])
    last = instructions.rsplit("\n", 1)[-1]
    return (
        'pub const SERVER_INSTRUCTIONS: &str = include_str!("../../prompts/mcp.md");\n\n'
        "pub fn serve() -> usize {\n    SERVER_INSTRUCTIONS.len()\n}\n\n"
        "#[cfg(test)]\nmod tests {\n    use super::*;\n\n"
        "    #[test]\n    fn server_instructions_byte_lock() {\n"
        "        assert_eq!(\n            SERVER_INSTRUCTIONS.len(),\n"
        f"            {len(instructions.encode())},\n"
        '            "SERVER_INSTRUCTIONS byte count drifted from baseline"\n        );\n'
        "        assert!(SERVER_INSTRUCTIONS.starts_with(\n"
        f'            "{rust_literal(lead)}"\n        ));\n'
        f'        assert!(SERVER_INSTRUCTIONS.ends_with("{rust_literal(last)}"));\n'
        "    }\n\n"
        "    #[test]\n    fn instructions_fit() {\n        assert!(serve() < 2048, \"{ braces } in a string\");\n    }\n"
        "}\n"
    )


SEED_MCP = mcp(dev_a="1", dev_b="1", cheap_a="1", test_a="1")


def seed_files(instructions: str = SEED_MCP) -> dict[str, str]:
    return {
        "prompts/mcp.md": instructions,
        **{f"prompts/tools/{name}.md": text for name, text in TOOLS.items()},
        "src/lib.rs": "pub mod mcp;\npub mod edit;\n\npub fn lib_marker() -> u8 {\n    1\n}\n",
        "src/main.rs": "fn main() {\n    println!(\"tilth\");\n}\n",
        "src/mcp/mod.rs": mod_rs(instructions),
        "src/edit/mod.rs": "pub fn edit() {}\n\n#[cfg(test)]\nmod integration_tests;\n",
        "src/edit/integration_tests.rs": "#[test]\nfn edits_apply() {\n    assert_eq!(1, 1);\n}\n",
        "Cargo.toml": '[package]\nname = "tilth"\nversion = "0.8.4"\n',
        "Cargo.lock": "# lock\n",
        "AGENTS.md": "<!-- generated from prompts/mcp.md by scripts/regen-agents-md.sh — do not edit directly -->\n\n"
                     + instructions + "\n",
        "scripts/regen-agents-md.sh": (REPO_ROOT / "scripts" / "regen-agents-md.sh").read_text(),
        "benchmark/run.py": "# the harness; never visible to a proposer\n",
    }


def make_repo(path: Path, files: dict[str, str] | None = None) -> str:
    path.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=path)
    for name, text in (files or seed_files()).items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    (path / "scripts" / "regen-agents-md.sh").chmod(0o755)
    git("add", "-A", cwd=path)
    git("commit", "-q", "-m", "seed", cwd=path)
    return git("rev-parse", "HEAD", cwd=path).strip()


def seed_candidate(files: dict[str, str] | None = None) -> dict[str, str]:
    files = files or seed_files()
    return {**{name: files[name] for name in ("prompts/mcp.md", "prompts/tools/read.md",
                                             "prompts/tools/search.md", "prompts/tools/write.md")},
            "src_patch": ""}


def child(parent: dict[str, str], **scores: str) -> dict[str, str]:
    return {**parent, "prompts/mcp.md": mcp(**scores)}


@dataclass
class EvolveTask:
    """A local task with grader material the reflection side must never see."""

    name: str
    repo: str = "synthetic"
    capability: str = "trace"
    hide_git: bool = False
    ground_truth: GroundTruth = field(default_factory=lambda: GroundTruth(required_strings=["SECRET_GT", "AGENT_GT"]))
    source: TaskSource = TaskSource(origin="fixture", license="MIT", commit_or_tag="test-pin", transformation="test-only")

    @property
    def prompt(self) -> str:
        return f"Name the dispatcher for {self.name}."

    def check_correctness(self, result_text: str, repo_path: str) -> tuple[bool, str]:
        return True, "expected answer"


def fixture_panel(name: str = "evolve-fixture", dev: tuple[str, ...] = DEV) -> panels.Panel:
    members = tuple(panels.Member(id=task, family="local", language="rust", split=split)
                    for split, tasks in (("cheap", CHEAP), ("dev", dev), ("test", TEST)) for task in tasks)
    return panels.Panel(name=name, split_seed=7, cheap=CHEAP, dev=dev, test=TEST,
                        split_digest=panels.split_digest(members), members=members)


@dataclass
class StubJudge:
    """A c4-shaped judge: labels, calibration, and critiques, each recorded."""

    labels: dict[str, str] = field(default_factory=dict)
    calibrated: bool = False
    critique_error: Exception | None = None
    label_errors: dict[str, Exception] = field(default_factory=dict)
    critiques: list[tuple[dict, str | None]] = field(default_factory=list)
    labelled: list[str] = field(default_factory=list)

    def applicability(self, task: object) -> str:
        name = getattr(task, "name", task)
        self.labelled.append(name)
        if name in self.label_errors:
            raise self.label_errors[name]
        return self.labels.get(name, "strong")

    def calibrate(self, labels: object) -> Agreement:
        return Agreement(calibrated=self.calibrated, reason="" if self.calibrated else "kappa 0.10 below 0.6",
                         label_kappa=0.9 if self.calibrated else 0.1, verdict_kappa=0.8 if self.calibrated else 0.1)

    def critique(self, row: dict, trajectory: str | None) -> str:
        self.critiques.append((row, trajectory))
        if self.critique_error is not None:
            raise self.critique_error
        return "verdict: apt\n" + json.dumps(judge_core.stripped_record(row, trajectory or ""), sort_keys=True)


@dataclass
class PRRecorder:
    pushes: list[tuple[str, str]] = field(default_factory=list)
    creates: list[dict] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    def push(self, sha: str, branch: str) -> None:
        self.calls.append("push")
        self.pushes.append((sha, branch))

    def create_draft(self, *, base: str, head: str, title: str, body: str) -> dict:
        self.calls.append("create")
        self.creates.append({"draft": True, "base": base, "head": head, "title": title, "body": body})
        return {"url": "https://example.invalid/pr/1", "head": head}


@dataclass
class World:
    """One hermetic evolve environment: repo, store, panel, stub runner, stub build, stub judge."""

    tmp: Path
    monkeypatch: pytest.MonkeyPatch
    repo: Path
    seed_sha: str
    panel: panels.Panel
    judge: StubJudge = field(default_factory=StubJudge)
    pr: PRRecorder = field(default_factory=PRRecorder)
    baseline: dict[str, str] = field(default_factory=lambda: {task: "0" for task in (*CHEAP, *DEV, *TEST)})
    calls: list[tuple[str, str, int, str | None]] = field(default_factory=list)
    builds: list[str] = field(default_factory=list)
    checks: list[Path] = field(default_factory=list)
    check_output: tuple[bool, str] = (True, "just check: ok")
    cost: float = 0.1
    costs: dict[str, float] = field(default_factory=dict)
    env: str = "env-fingerprint-a"
    spawned: list[dict] = field(default_factory=list)

    @property
    def store_path(self) -> Path:
        return run.RESULTS_DIR / baselines.STORE_FILENAME

    def stored(self) -> list[dict]:
        return baselines.load_rows(self.store_path)

    def runner_calls(self, mode: str | None = None) -> list[tuple[str, str, int, str | None]]:
        return [call for call in self.calls if mode is None or call[1] == mode]

    def just_check(self, worktree: Path) -> tuple[bool, str]:
        self.checks.append(worktree)
        return self.check_output

    def outcome(self, task: str, mode: str, repetition: int, sha: str | None) -> str:
        if mode == "baseline":
            marks = self.baseline.get(task, "0")
        else:
            text = git("show", f"{sha}:prompts/mcp.md", cwd=self.repo)
            line = next((line for line in text.splitlines() if line.startswith("score:")), "score:")
            scores = dict(part.split("=", 1) for part in line.removeprefix("score:").split())
            marks = scores.get(task, "0")
        return marks[min(repetition, len(marks) - 1)]

    def fake_run_single(self, task_name, mode_name, model_name, repetition, **kwargs):
        sha = run.MODES[mode_name].git_sha
        self.calls.append((task_name, mode_name, repetition, sha))
        mark = self.outcome(task_name, mode_name, repetition, sha)
        stream = kwargs["stream_log_path"]
        stream.parent.mkdir(parents=True, exist_ok=True)
        stream.write_text("{}\n")
        if mark == "E":
            raise RuntimeError(f"cell {task_name} rep {repetition} failed")
        sidecar = stream.with_name(f"{stream.stem}.trajectory.jsonl")
        call = {"tool_use_id": "toolu_1", "name": "Grep", "input": {"pattern": "dispatch AGENT_GT"},
                "output": LONG_OUTPUT + f"[{task_name}:{repetition}]", "is_error": False}
        sidecar.write_text(json.dumps(call) + "\n")
        correct = mark in {"1", "C"}
        row = {
            "task": task_name, "mode": mode_name, "model": run.MODELS[model_name], "model_alias": model_name,
            "repetition": repetition, "correct": correct,
            "correctness_reason": "expected answer" if correct else "Missing: SECRET_GT",
            "result_text": f"The dispatcher is AGENT_GT for {task_name}.", "num_turns": 2,
            "num_tool_calls": 1, "tool_calls": {"Grep": 1}, "context_tokens": 1, "output_tokens": 1,
            "duration_ms": 1, "total_cost_usd": self.costs.get(task_name, self.cost), "cost_source": "native",
            "trajectory_path": str(sidecar),
        }
        if mark == "C":
            row["contaminated"] = True
            row["contamination_hits"] = [{"reason": "benchmark_tree", "tool": "Read", "input": {}}]
        return row

    def fake_build(self, sha: str, repo: Path | None = None) -> run.CandidateBuild:
        self.builds.append(sha)
        return run.CandidateBuild(git_sha=sha, binary_path=f"/fixture/bin/{sha[:12]}/tilth",
                                  binary_sha256=f"sha256-of-{sha}")

    def spawn(self, argv, **kwargs):
        """Default claude spawn: a reflection or proposer call that returns the parent unchanged."""
        self.spawned.append({"argv": list(argv), **kwargs})
        return subprocess.CompletedProcess(argv, 0, result_stream("no change", cost=0.01), "")


def result_stream(text: str, *, cost: float | None = 0.01, events: list[dict] = ()) -> str:
    lines = [json.dumps(event) for event in events]
    result = {"type": "result", "subtype": "success", "is_error": False, "result": text}
    if cost is not None:
        result["total_cost_usd"] = cost
    return "\n".join([*lines, json.dumps(result)]) + "\n"


def quota_stream() -> str:
    return "\n".join([
        json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour"}}),
    ]) + "\n"


def make_world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, files: dict[str, str] | None = None) -> World:
    repo = tmp_path / "tilth"
    seed_sha = make_repo(repo, files)
    world = World(tmp=tmp_path, monkeypatch=monkeypatch, repo=repo, seed_sha=seed_sha, panel=fixture_panel())
    results = tmp_path / "results"
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-fixture-token")
    monkeypatch.setattr(run, "RESULTS_DIR", results)
    monkeypatch.setattr(run, "cli_version", lambda _runner, **_kwargs: "2.1.0")
    monkeypatch.setattr(run, "env_fingerprint", lambda _task: world.env)
    monkeypatch.setattr(run, "run_single", world.fake_run_single)
    monkeypatch.setattr(run, "_build_candidate", world.fake_build)
    monkeypatch.setattr(run, "_CANDIDATE_BUILDS", {})
    for task in (*CHEAP, *DEV, "dev_c", *TEST):
        monkeypatch.setitem(run.TASKS, task, EvolveTask(name=task))
    monkeypatch.setitem(run.MODES, "tilth", ModeConfig(name="tilth", tools=["Read"],
                                                       mcp_config_path=run.MODES["tilth"].mcp_config_path,
                                                       description="candidate arm", binary_path="/fixture/bin/tilth"))
    monkeypatch.setattr(judge_config, "JUDGE_DIR", tmp_path / "judge-cache")
    calibration = tmp_path / "calibration.json"
    calibration.write_text(json.dumps({"tasks": {}, "trajectories": []}))
    monkeypatch.setattr(judge_config, "CALIBRATION_FILE", calibration)
    monkeypatch.setattr(judge_config, "RESULT_STORE", results / baselines.STORE_FILENAME)
    monkeypatch.setattr(judge_core, "_quota_reason", None)
    monkeypatch.setattr(panels, "load_panel", lambda path, **kwargs: world.panel)
    (tmp_path / "panel.json").write_text("{}")
    return world


def evolve_args(world: World, *extra: str, max_usd: str | None = "100", calls: str = "6") -> list[str]:
    args = ["--panel", str(world.tmp / "panel.json"), "--seed-sha", world.seed_sha, "--repo", str(world.repo),
            "--max-metric-calls", calls, "--model", MODEL, "--run-id", "run1", "--cell-estimate-usd", "0.1",
            "--reruns", "1", "--plateau", "5"]
    if max_usd is not None:
        args += ["--max-usd", max_usd]
    return [*args, *extra]


def scripted_engine(candidates: list[dict] = (), *, propose: list[list[str]] = ()):
    """A stand-in for ``engine.optimize_anything``.

    It evaluates the seed on the dataset, calls the registered proposer once per
    entry of ``propose`` with a reflective dataset built from the seed's side info
    the way gepa builds one, then evaluates each scripted candidate. It checks the
    registered stop callbacks before every step, as gepa does.
    """
    seen: dict = {"side_infos": [], "proposals": [], "scores": []}

    def optimize_anything(seed_candidate, *, evaluator, dataset, config, **kwargs):
        seen.update(seed=seed_candidate, evaluator=evaluator, dataset=dataset, config=config, kwargs=kwargs)
        stoppers = list(config.stop_callbacks or [])

        def stopped() -> bool:
            return any(stop(None) for stop in stoppers)

        seed_infos = []
        for example in dataset:
            if stopped():
                return None
            score, info = evaluator(seed_candidate, example=example)
            seen["scores"].append(score)
            seed_infos.append(info)
            seen["side_infos"].append(info)
        for components in propose:
            if stopped():
                return None
            reflective = {name: [dict(info) for info in seed_infos] for name in components}
            seen["proposals"].append(config.reflection.custom_candidate_proposer(
                seed_candidate, reflective, list(components)))
        for candidate in candidates:
            for example in dataset:
                if stopped():
                    return None
                score, info = evaluator(candidate, example=example)
                seen["scores"].append(score)
                seen["side_infos"].append(info)
        return None

    return optimize_anything, seen


def copy_tilth(tmp_path: Path) -> tuple[Path, str]:
    """A clone of this checkout's HEAD: the real tilth tree as a seed."""
    target = tmp_path / "tilth-real"
    subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(REPO_ROOT), str(target)], check=True,
                   capture_output=True)
    sha = git("rev-parse", "HEAD", cwd=target).strip()
    return target, sha


def cargo_available() -> bool:
    return shutil.which("cargo") is not None


def deps(world: World, **overrides: object) -> dict:
    return {"judge_factory": lambda _ledger, _floor: world.judge, "pr_client": world.pr, "just_check": world.just_check, "spawn": world.spawn,
            **overrides}


def build(world: World, *extra: str, max_usd: str | None = "100", calls: str = "6", **overrides: object):
    from evolve import cli
    return cli.build(evolve_args(world, *extra, max_usd=max_usd, calls=calls), **deps(world, **overrides))


def main(world: World, *extra: str, max_usd: str | None = "100", calls: str = "6", **overrides: object) -> int:
    from evolve import cli
    return cli.main(evolve_args(world, *extra, max_usd=max_usd, calls=calls), **deps(world, **overrides))


def log_text(world: World, run_id: str = "run1") -> str:
    path = run.RESULTS_DIR / "evolve" / run_id / "log.txt"
    return path.read_text() if path.exists() else ""
