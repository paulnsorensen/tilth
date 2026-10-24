"""One evolve run: judge preflight, frozen baselines, the cascade, evolve's accepted frontier, and finish.

A candidate's score is grader correctness only: the mean ``correct`` over its
paid-tier dev rollouts. A contaminated rollout counts as incorrect.
Reflection and the proposer see only stripped records.
Test-split rollouts appear only in ``finish``. Finish picks the winner on dev means, so the test split
only confirms one pre-chosen candidate against the seed and never selects among candidates.
"""

import json
import math
import subprocess
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean

import auth
import baselines
import external.data
import paired
import panels
import run
import stats
from judge import core as judge
from judge import store as judge_store
from spend import SpendLedger

from . import engine, hookgate
from .calls import PaidCalls, StopState
from .candidate import content_id
from .finish import REPIN_NOTE, PRClient, outside_allowlist
from .gitops import GitError, git
from .materialize import ApplyRejected, Materializer
from .proposer import Proposer

# Candidates run with tilth MCP only, so a score measures tilth itself and not a fallback to built-in tools.
CANDIDATE_MODE = "tilth_forced"
BASELINE_MODE = "baseline"
TAIL_LINES = 40
NO_SPEND_REASONS = frozenset({"quota", "mcp-unavailable", "invalid-cell", "auth"})
FINISH_CELL_SETS = 2  # finish scores two candidates on the test split: the dev winner and the seed


class EvolveError(RuntimeError):
    """The run cannot continue: a scored row is malformed or the run was misconfigured."""


class _Stopped(Exception):
    """A paid step did not start because the stop state is set."""


@dataclass(frozen=True)
class Settings:
    run_id: str
    repo: Path
    seed_sha: str
    base_branch: str
    model: str
    reruns: int
    plateau: int
    max_usd: float
    max_metric_calls: int
    cell_estimate_usd: float
    # The per-cell Claude --max-budget-usd of every cell in the run; it is part of each cell's run key.
    cell_budget_usd: float
    refreeze_baselines: bool
    panel_path: Path
    # The claude CLI alias "sonnet" for reflection and the proposer; the judge model does not move them.
    helper_model: str = "sonnet"
    # Strict cells guard Bash with the hook: candidates run their own hook, the baseline runs the seed's.
    strict_file_tools: bool = True


@dataclass(eq=False)
class Cascade:
    """Everything evolve knows about one candidate content id; memoized for the run."""

    cid: str
    seq: int
    candidate: dict[str, str]
    sha: str | None = None
    stage: str | None = None
    tail: str = ""
    just_check_ok: bool | None = None
    hook_ok: bool | None = None
    cheap_score: float | None = None
    cheap_scores: dict[str, float] = field(default_factory=dict)
    cheap_rows: list[dict] = field(default_factory=list)
    cheap_records: list[dict] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    means: dict[str, float] = field(default_factory=dict)
    rows: list[dict] = field(default_factory=list)
    records: dict[str, list[dict]] = field(default_factory=dict)
    admitted: bool = False
    accepted: bool = False
    counted: bool = False
    done: bool = False
    delta: dict | None = None
    task_stats: dict[str, tuple[float, float]] = field(default_factory=dict)
    base_costs: dict[str, float] = field(default_factory=dict)
    base_rows: dict[str, list[dict]] = field(default_factory=dict)

    @property
    def dev_mean(self) -> float:
        values = self.means or self.scores
        return mean(values.values()) if values else 0.0

    def score(self, task: str) -> float:
        return (self.means or self.scores).get(task, 0.0)

    def fail(self, stage: str, tail: str = "") -> "Cascade":
        self.stage, self.tail, self.done = stage, tail, True
        return self

    def side_info(self, task: str) -> dict:
        if self.stage in {"apply", "just check", "build", "hook gate"}:
            return {"stage": self.stage, "tail": self.tail}
        if self.stage == "cheap tier":
            return {"stage": self.stage, "task_scores": dict(self.cheap_scores), "records": self.cheap_records}
        return {"stage": "paid", "task_scores": {name: self.score(name) for name in self.scores},
                "pass_rate": {name: stats[0] for name, stats in self.task_stats.items()},
                "mean_cost": {name: stats[1] for name, stats in self.task_stats.items()},
                "baseline_cost": dict(self.base_costs), "records": self.records.get(task, []),
                "cost_pairs": {"candidate": [cost_summary(row) for row in self.rows if row["task"] == task],
                               "baseline": list(self.base_rows.get(task, []))}}


def tail(output: str) -> str:
    return "\n".join(output.rstrip().splitlines()[-TAIL_LINES:])


def dominates(left: Mapping[str, float], right: Mapping[str, float], tasks: Iterable[str]) -> bool:
    pairs = [(left.get(task, 0.0), right.get(task, 0.0)) for task in tasks]
    return all(a >= b for a, b in pairs) and any(a > b for a, b in pairs)


def correct(row: Mapping) -> bool:
    """Grader correctness for scoring; a contaminated rollout is incorrect, a missing flag stops the run."""
    if "contaminated" not in row:
        raise EvolveError(f"row {row.get('task')} rep {row.get('repetition')} (run key {row.get('run_key')}) "
                          "has no contaminated flag; refusing to score it")
    return bool(row.get("correct")) and row["contaminated"] is not True


def _cost_value(value: object) -> bool:
    """True for a finite, non-negative int or float that is not a bool."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def row_cost(row: Mapping) -> float:
    """A row's cost: ``total_cost_usd``, else ``charged_usd`` when a failed cell reports no total; else a stop.

    Only an absent or null ``total_cost_usd`` falls back. A present but malformed, negative, or non-finite
    value is corrupt data and stops the run.
    """
    key = "total_cost_usd" if row.get("total_cost_usd") is not None else "charged_usd"
    value = row.get(key)
    if _cost_value(value):
        return float(value)
    raise EvolveError(f"row {row.get('task')} rep {row.get('repetition')} (run key {row.get('run_key')}) "
                      f"has {key} {value!r}; a finite, non-negative cost is required to score it")


def task_stats(rows: Iterable[Mapping], task: str) -> tuple[float, float] | None:
    """Pass rate and mean row cost over ``task``'s rows; None when there are none."""
    mine = [row for row in rows if row["task"] == task]
    if not mine:
        return None
    return mean(correct(row) for row in mine), mean(row_cost(row) for row in mine)


def task_score(rows: Iterable[Mapping], task: str, base_cost: float) -> float:
    """``p * base / c``: pass rate times the frozen baseline mean cost over the candidate's mean row cost.

    1.0 is baseline parity at equal pass rate; a task never solved scores 0.
    """
    stats = task_stats(rows, task)
    if stats is None:
        return 0.0
    passed, cost = stats
    if passed == 0:
        return 0.0
    if not cost > 0:
        raise EvolveError(f"task {task} is solved but its mean candidate cost is {cost}; refusing to score it")
    return passed * base_cost / cost


# Row fields that explain a cost difference; reflection compares them per task, candidate against baseline.
COST_SUMMARY_FIELDS = (
    "repetition", "total_cost_usd", "num_turns", "num_tool_calls", "tool_calls", "tool_sequence", "batch_sizes",
    "input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens", "context_tokens",
)


def cost_summary(row: Mapping) -> dict:
    """The cost-explaining fields of one row, with no trajectory and no grader output."""
    return {name: row[name] for name in COST_SUMMARY_FIELDS if name in row}


def _baseline_costs(rows: Iterable[Mapping], tasks: Iterable[str]) -> dict[str, float]:
    """Mean ``total_cost_usd`` of each task's completed baseline rows; a missing or non-positive cost is a stop."""
    rows = [row for row in rows if baselines.is_completed(row)]
    costs = {}
    for task in tasks:
        mine = [row for row in rows if row["task"] == task]
        if not mine:
            raise EvolveError(f"task {task} has no completed baseline row; refusing to score against it")
        for row in mine:
            value = row.get("total_cost_usd")
            if not _cost_value(value) or not value > 0:
                raise EvolveError(f"baseline row {task} rep {row.get('repetition')} has total_cost_usd {value!r}; "
                                  "a finite, positive cost is required")
        costs[task] = mean(row["total_cost_usd"] for row in mine)
    return costs


def _paired_score_deltas(winner_rows: Iterable[Mapping], seed_rows: Iterable[Mapping], tasks: Iterable[str],
                         base_cost: Mapping[str, float]) -> list[float]:
    """Winner minus seed score per task over each task's reps; a task missing rows on either side is skipped."""
    winner_rows, seed_rows = list(winner_rows), list(seed_rows)
    deltas = []
    for task in tasks:
        if task_stats(winner_rows, task) is not None and task_stats(seed_rows, task) is not None:
            deltas.append(task_score(winner_rows, task, base_cost[task]) - task_score(seed_rows, task, base_cost[task]))
    return deltas


class Evolution:
    def __init__(self, settings: Settings, *, panel: panels.Panel, ledger: SpendLedger, judge_: judge.Judge,
                 seed: dict[str, str], pr_client: PRClient, just_check: Callable[[Path], tuple[bool, str]],
                 spawn: Callable) -> None:
        self.settings = settings
        self.panel = panel
        self.ledger = ledger
        self.judge = judge_
        self.seed = dict(seed)
        self.seed_id = content_id(self.seed)
        self.pr_client = pr_client
        self.just_check = just_check
        self.run_dir = run.RESULTS_DIR / "evolve" / settings.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.run_dir / "log.txt"
        self.rows_path = self.run_dir / "rows.jsonl"
        self.stop = StopState()
        self.stop.listeners.append(self._on_stop)
        self.paid = PaidCalls(ledger, self.stop, settings.cell_estimate_usd, spawn=spawn, log=self.log)
        self.reflect = engine.ReflectionClient(self.paid, settings.helper_model)
        self.proposer = Proposer(settings.repo, settings.seed_sha, self.paid, settings.helper_model, log=self.log,
                                 forbidden=self._forbidden_terms())
        self.dispatcher = engine.Dispatcher(self.stop, reflect=self.reflect, propose=self.proposer.propose_src_patch)
        self.materializer = Materializer(settings.repo, settings.seed_sha, settings.run_id, self.run_dir / "worktrees",
                                         forbidden=self._forbidden_terms())
        self.results: dict[str, Cascade] = {}
        self.frontier: list[Cascade] = []
        self.deltas: list[dict] = []
        self.labels: dict[str, str] = {}
        self.released: dict[str, str] = {}
        self.calibrated = False
        self.dev_baseline_rows: list[dict] | None = None
        self.base_cost: dict[str, float] = {}
        self.base_rows: dict[str, list[dict]] = {}
        self.spend: dict[str, float] = {"cells": 0.0, "judge": 0.0}
        self.best: float | None = None
        self.unimproved = 0
        self.finish_failure: str | None = None

    # --- plumbing ---

    def log(self, line: str) -> None:
        print(line)
        with self.log_path.open("a") as log:
            log.write(line + "\n")

    def _on_stop(self, reason: str) -> None:
        self.log(f"stop: {reason}")
        if reason == "quota":
            self.log("infra:quota: a usage limit rejected a call; stored rows stay reusable")

    def _forbidden_terms(self) -> list[str]:
        panel = Path(self.settings.panel_path)
        data = external.data.data_dir()
        return ["benchmark/", str(panel), str(panel.resolve()), panel.name, str(data)]

    @contextmanager
    def _spending(self, kind: str) -> Iterator[None]:
        before = self.ledger.spent
        try:
            yield
        finally:
            self.spend[kind] += self.ledger.spent - before

    def _reps(self) -> range:
        return range(self.settings.reruns + 1)

    def _cells(self, tasks: Iterable[str], mode: str, reps: Iterable[int]) -> list[run.CellSpec]:
        reps = list(reps)
        return [run.CellSpec(task, mode, self.settings.model, rep) for task in tasks for rep in reps]

    def _plan(self, cells: list[run.CellSpec], sha: str | None, *, store_only: bool = False) -> list[dict]:
        try:
            with self._spending("cells"):
                rows = run.run_plan(cells, panel=self.panel, ledger=self.ledger, candidate_sha=sha,
                                    refreeze_baselines=self.settings.refreeze_baselines, output=self.rows_path,
                                    cell_estimate_usd=self.settings.cell_estimate_usd,
                                    max_budget_usd=self.settings.cell_budget_usd, store_only=store_only,
                                    repo=self.settings.repo, **self._strict_plan_kwargs())
        except run.PlanStopped as stopped:
            self.log(f"cells: stopped ({stopped})")
            self.stop.set(stopped.reason)
            raise _Stopped(stopped.reason) from stopped
        for row in rows:
            correct(row)
        return rows

    def _strict_plan_kwargs(self) -> dict:
        if not self.settings.strict_file_tools:
            return {}
        return {"strict_file_tools": True, "hook_sha": self.settings.seed_sha}

    def _strict_identity_kwargs(self) -> dict:
        if not self.settings.strict_file_tools:
            return {}
        return {**self._strict_plan_kwargs(), "hook_repo": self.settings.repo}

    def new_cascade(self, candidate: Mapping[str, str]) -> Cascade:
        cid = content_id(candidate)
        if cid not in self.results:
            self.results[cid] = Cascade(cid=cid, seq=len(self.results) + 1, candidate=dict(candidate))
        return self.results[cid]

    # --- preflight ---

    def finalist_test_cost(self) -> float:
        """The estimate for one finalist: its test-split cells plus the test-split baseline cells not yet stored."""
        history = baselines.load_rows(run.RESULTS_DIR / baselines.STORE_FILENAME)
        stored = baselines.completed_by_key(history)
        model = run.MODELS[self.settings.model]
        cost = 0.0
        for cell in self._cells(self.panel.test, CANDIDATE_MODE, self._reps()):
            cost += run.estimate_cell_cost(history, task=cell.task, mode=CANDIDATE_MODE, model=model,
                                           run_max_cost=None, fallback=self.settings.cell_estimate_usd)
        for cell in self._cells(self.panel.test, BASELINE_MODE, self._reps()):
            if run.planned_identity(cell, max_budget_usd=self.settings.cell_budget_usd,
                                    **self._strict_identity_kwargs())["run_key"] not in stored:
                cost += run.estimate_cell_cost(history, task=cell.task, mode=BASELINE_MODE, model=model,
                                               run_max_cost=None, fallback=self.settings.cell_estimate_usd)
        return cost

    def refuse(self, message: str) -> int:
        self.log(f"refused: {message}")
        print(f"error: {message}", file=sys.stderr)
        return 2

    def _seed_hook_preflight(self) -> int | None:
        """Refuse a strict run, before any paid call, when the seed's own hook gets a corpus row wrong."""
        try:
            run.require_strict_cli()
        except ValueError as error:
            return self.refuse(str(error))
        try:
            binary = run.build_candidate(self.settings.seed_sha, repo=self.settings.repo).binary_path
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            return self.refuse("seed hook does not build: " + tail(run.build_failure_detail(error)))
        failures = hookgate.corpus_failures(binary)
        if failures:
            return self.refuse("the seed hook fails the frozen hook corpus (strict runs need a faithful seed hook):\n"
                               + "\n".join(failures))
        try:
            for hint in ("native", "tilth"):
                run.probe_hook(binary, hint)
            run.hook_source_key(self.settings.seed_sha, self.settings.repo)
        except (run.CandidateBuildFailed, ValueError, subprocess.SubprocessError, OSError) as error:
            return self.refuse(f"seed hook probe failed: {tail(str(error))}")
        self.new_cascade(self.seed).hook_ok = True  # the cascade must not rerun the corpus for the seed
        self.log("hook: seed hook passes the frozen corpus and the hint-text probe in both hint modes")
        return None

    def preflight(self) -> int | None:
        """Hold the finish reserve, label every panel task, and calibrate; an exit code when the run is refused.

        A drifted cheap, dev, or test-split baseline cell refuses the run here, before any paid call, unless
        ``--refreeze-baselines`` lets finish buy it again.
        """
        if self.settings.strict_file_tools:
            if (code := self._seed_hook_preflight()) is not None:
                return code
        if not self.settings.refreeze_baselines:
            baseline_cells = self._cells((*self.panel.cheap, *self.panel.dev, *self.panel.test), BASELINE_MODE,
                                         self._reps())
            try:
                run.check_baseline_drift(baseline_cells, panel=self.panel, max_budget_usd=self.settings.cell_budget_usd,
                                         **self._strict_identity_kwargs())
            except run.BaselineDrift as error:
                return self.refuse(f"baselines: {error}")
        # The estimate counts every unstored baseline test cell, so the reserve over-counts them; that error is kept
        # because holding too much is safe and holding too little would starve finish.
        per_finalist = self.finalist_test_cost()
        reserve = FINISH_CELL_SETS * per_finalist
        if self.settings.max_usd <= reserve:
            return self.refuse(f"--max-usd ${self.settings.max_usd:.2f} must exceed the finish reserve ${reserve:.2f} "
                               f"({FINISH_CELL_SETS} x one finalist's test-split cost ${per_finalist:.2f}); the search would have no budget")
        self.ledger.reserve = reserve
        self.log(f"reserve: ${reserve:.4f} held for finish (per finalist ${per_finalist:.4f})")

        unlabeled = []
        for name in self.panel.select("all"):
            try:
                with self._spending("judge"):
                    self.labels[name] = self.judge.applicability(run.TASKS[name])
            except auth.ClaudeAuthError as error:
                self.log(f"labels: auth refused ({error})")
                self.stop.set("auth")
                return None
            except judge.JudgeError as error:
                unlabeled.append(f"{name} ({error})")
        if unlabeled:
            return self.refuse(f"no valid applicability label for: {'; '.join(unlabeled)}")

        agreement, reason = None, ""
        try:
            with self._spending("judge"):
                agreement = self.judge.calibrate(judge.load_calibration())
            reason = agreement.reason
        except judge.CalibrationInvalid as error:
            reason = f"calibration invalid: {error}"
        except judge.JudgeQuota as error:
            reason = str(error)
            self.stop.set("quota")
        except judge.JudgeSpendCeiling as error:
            reason = str(error)
            self.stop.set("ceiling")
        except auth.ClaudeAuthError as error:
            reason = str(error)
            self.stop.set("auth")
        except judge.JudgeError as error:
            reason = str(error)
        self.calibrated = bool(agreement is not None and agreement.calibrated)
        for name, label in self.labels.items():
            digest = run._cell_task_digest(run.TASKS[name])
            if released := judge_store.released_label(agreement, name, digest, {digest: label}):
                self.released[name] = released
        if agreement is not None:
            kappas = " ".join(f"{name}={value:.2f}" if value is not None else f"{name}=undefined"
                              for name, value in (("label_kappa", agreement.label_kappa),
                                                  ("verdict_kappa", agreement.verdict_kappa)))
            self.log(f"labels: agreement {kappas} calibrated={self.calibrated}")
        if not self.calibrated:
            self.log(f"labels: uncalibrated ({reason or 'no agreement record'})")
        return None

    def buy_baselines(self) -> int | None:
        """Buy each cheap, dev, and test baseline cell once; later runs answer them from the store.

        The baseline arm's mean cost per task is frozen here as the score's reference.
        """
        tasks = (*self.panel.cheap, *self.panel.dev, *self.panel.test)
        try:
            rows = self._plan(self._cells(tasks, BASELINE_MODE, self._reps()), None)
            self.base_cost = _baseline_costs(rows, tasks)
            self.base_rows = {task: [cost_summary(row) for row in rows
                                     if row["task"] == task and baselines.is_completed(row)] for task in tasks}
        except run.BaselineDrift as error:
            return self.refuse(str(error))
        except run.CandidateBuildFailed as error:
            return self.refuse(f"baselines: {tail(str(error))}")
        except EvolveError as error:
            return self.refuse(f"baselines: {error}")
        except _Stopped:
            pass
        return None

    # --- search ---

    def search(self):
        found = engine.optimize(self.seed, evaluator=self.evaluate, dataset=self.panel.dev,
                                max_metric_calls=self.settings.max_metric_calls, stop=self.stop,
                                proposer=self.dispatcher)
        self.log(f"search: ended ({self.stop.reason or 'metric-call budget'})")
        return found

    def evaluate(self, candidate: dict[str, str], example: str) -> tuple[float, dict]:
        """GEPA's evaluator: the candidate's score on one dev task, with grader-free side info."""
        if self.stop.is_set:
            return 0.0, {"stopped": self.stop.reason}
        try:
            result = self.cascade(candidate)
        except _Stopped:
            return 0.0, {"stopped": self.stop.reason}
        if result.stage is not None:
            return 0.0, result.side_info(example)
        result.task_stats = {task: stats for task in self.panel.dev if (stats := task_stats(result.rows, task))}
        result.base_costs = {task: self._base(task) for task in result.task_stats}
        result.base_rows = {task: self.base_rows.get(task, []) for task in result.task_stats}
        return result.score(example), result.side_info(example)

    def cascade(self, candidate: Mapping[str, str]) -> Cascade:
        """Apply, ``just check``, build, cheap tier, paid tier; each stage memoized per content id."""
        result = self.new_cascade(candidate)
        if result.done:
            return result
        if result.cid != self.seed_id and not self.cascade(self.seed).accepted:
            raise EvolveError("the seed is not on the accepted frontier (its dev run failed or did not complete); "
                              "refusing to buy candidate rollouts")
        if result.sha is None:
            try:
                result.sha = self.materializer.materialize(candidate)
            except ApplyRejected as rejected:
                return self._fail(result, "apply", tail(rejected.tail))
        if result.just_check_ok is None:
            ok, output = self.just_check(self.materializer.worktree(result.sha))
            result.just_check_ok = ok
            self.materializer.release(result.sha)
            if not ok:
                return self._fail(result, "just check", tail(output))
        if self.settings.strict_file_tools and not result.hook_ok:
            if (failed := self._hook_gate(result)) is not None:
                return failed
        try:
            return self._tiers(result)
        except run.HookProbeFailed as failed:
            return self._fail(result, "hook gate", tail(str(failed)))
        except run.CandidateBuildFailed as failed:
            return self._fail(result, "build", tail(str(failed)))

    def _hook_gate(self, result: Cascade) -> Cascade | None:
        """Run the corpus through the candidate's hook before any cell; the failed cascade, or None when it passes."""
        try:
            binary = run.build_candidate(result.sha, repo=self.settings.repo).binary_path
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            return self._fail(result, "build", tail(run.build_failure_detail(error)))
        failures = hookgate.corpus_failures(binary)
        if failures:
            shown = failures[:TAIL_LINES - 2]
            if len(failures) > len(shown):
                shown.append(f"... and {len(failures) - len(shown)} more")
            return self._fail(result, "hook gate", "The candidate's Bash hook fails the frozen hook corpus:\n"
                              + "\n".join(shown))
        result.hook_ok = True
        return None

    def _fail(self, result: Cascade, stage: str, output: str = "") -> Cascade:
        """Mark ``result`` failed at ``stage`` and log the stage with its failure tail."""
        self.log(f"candidate {result.cid[:12]}: failed at {stage}")
        for line in output.splitlines():
            self.log(f"  {line}")
        return result.fail(stage, output)

    def _tiers(self, result: Cascade) -> Cascade:
        """The cheap and paid tiers; the first cell of either builds the candidate binary."""
        if result.cheap_score is None:
            rows = self._plan(self._cells(self.panel.cheap, CANDIDATE_MODE, [0]), result.sha)
            result.cheap_rows = rows
            result.cheap_scores = {task: self._task_mean(rows, task) for task in self.panel.cheap}
            result.cheap_score = mean(result.cheap_scores.values()) if result.cheap_scores else 0.0
        if result.cid != self.seed_id:
            seed = self.results[self.seed_id]
            if result.cheap_score < (seed.cheap_score or 0.0):
                # Only a failing candidate shows its cheap-tier records to reflection; critiques cost money.
                records = self._records(result.cheap_rows)
                result.cheap_records = [record for task in self.panel.cheap for record in records.get(task, [])]
                return self._fail(result, "cheap tier")
        if not result.scores:
            rows = self._plan(self._cells(self.panel.dev, CANDIDATE_MODE, [0]), result.sha)
            records = self._records(rows)
            result.rows = rows
            result.records = {task: records.get(task, []) for task in self.panel.dev}
            result.scores = {task: self._task_mean(rows, task) for task in self.panel.dev}
        self._admit(result)
        if result.delta is None:
            result.delta = self._dev_delta(result)
        if not result.counted:
            result.counted = True
            self._count_plateau()
        result.done = True
        return result

    def _task_mean(self, rows: list[dict], task: str) -> float:
        return task_score(rows, task, self._base(task))

    def _base(self, task: str) -> float:
        if task not in self.base_cost:
            raise EvolveError(f"no frozen baseline cost for task {task}; refusing to score it")
        return self.base_cost[task]

    def _records(self, rows: list[dict]) -> dict[str, list[dict]]:
        """One stripped record per uncontaminated rollout, plus ``label`` and ``critique`` when calibrated."""
        records: dict[str, list[dict]] = {}
        for row in rows:
            if row["contaminated"] is True:
                continue
            path = row.get("trajectory_path")
            sidecar = Path(path).read_text() if path and Path(path).is_file() else None
            record = judge.stripped_record(row, sidecar or "")
            if self.calibrated:
                record["label"] = self.released.get(row["task"])
                try:
                    with self._spending("judge"):
                        record["critique"] = self.judge.critique(row, sidecar)
                except judge.JudgeQuota:
                    self.stop.set("quota")
                    raise _Stopped("quota") from None
                except judge.JudgeSpendCeiling:
                    self.stop.set("ceiling")
                    raise _Stopped("ceiling") from None
                except auth.ClaudeAuthError:
                    self.stop.set("auth")
                    raise _Stopped("auth") from None
                except judge.JudgeError as error:
                    record["critique_error"] = str(error)
            records.setdefault(row["task"], []).append(record)
        return records

    # --- accepted frontier ---

    def _admit(self, result: Cascade) -> None:
        """Re-run an undominated candidate R times at fresh repetitions; accept it if still undominated."""
        if result.admitted:
            return
        dev = self.panel.dev
        members = [member for member in self.frontier if member is not result]
        first = [row for row in result.rows if row["repetition"] == 0]
        if not all(baselines.is_completed(row) for row in first) or len(first) < len(dev):
            result.admitted = True
            self.log(f"candidate {result.cid[:12]}: a first rollout did not complete; not re-run")
            return
        if any(dominates(member.means, result.scores, dev) for member in members):
            result.admitted = True
            self.log(f"candidate {result.cid[:12]}: dominated after one rollout; not re-run")
            return
        reruns = self._plan(self._cells(dev, CANDIDATE_MODE, range(1, self.settings.reruns + 1)), result.sha)
        records = self._records(reruns)
        for task in dev:
            result.records[task] = result.records.get(task, []) + records.get(task, [])
        result.rows = [*result.rows, *reruns]
        result.admitted = True
        means = {task: self._task_mean(result.rows, task) for task in dev}
        if not all(baselines.is_completed(row) for row in reruns) or len(reruns) < len(dev) * self.settings.reruns:
            self.log(f"candidate {result.cid[:12]}: a re-run did not complete; not accepted")
            return
        result.means = means
        if any(dominates(member.means, means, dev) for member in members):
            self.log(f"candidate {result.cid[:12]}: dominated after {self.settings.reruns + 1} rollouts; not accepted")
            return
        result.accepted = True
        self.frontier = [member for member in members if not dominates(means, member.means, dev)] + [result]
        self.log(f"candidate {result.cid[:12]}: accepted (dev mean {result.dev_mean:.3f}, "
                 f"frontier {len(self.frontier)})")

    def _count_plateau(self) -> None:
        best = max((member.dev_mean for member in self.frontier), default=None)
        if best is not None and (self.best is None or best > self.best):
            self.best, self.unimproved = best, 0
            return
        self.unimproved += 1
        if self.unimproved >= self.settings.plateau:
            self.log(f"plateau: {self.unimproved} paid-tier candidates left the best dev mean unimproved")
            self.stop.set("plateau")

    # --- delta reports ---

    def _delta_report(self, result: Cascade, split: str, tilth_rows: list[dict], baseline_rows: list[dict]) -> dict:
        rows = [{**row, "correct": correct(row)} for row in (*tilth_rows, *baseline_rows)]

        def accuracy(subset: list[dict]) -> dict:
            point, low, high, tasks = paired.paired_accuracy_delta(subset, CANDIDATE_MODE, BASELINE_MODE)
            return {"accuracy_delta": point, "accuracy_ci": [low, high], "paired_tasks": tasks}

        cpc, cpc_low, cpc_high = paired.paired_cpc_delta(rows, CANDIDATE_MODE, BASELINE_MODE)
        report = {"candidate": result.cid[:12], "seq": result.seq, "split": split, **accuracy(rows),
                  "cpc_delta": cpc, "cpc_ci": [cpc_low, cpc_high]}
        if self.calibrated:
            report["labels"] = {label: accuracy([row for row in rows if self.released.get(row["task"]) == label])
                                for label in sorted(set(self.released.values()))}
        else:
            report["labels"] = "uncalibrated"
        self.deltas.append(report)
        self.log(f"delta {result.cid[:12]} {split}: {json.dumps(report, sort_keys=True)}")
        return report

    def _dev_delta(self, result: Cascade) -> dict:
        if self.dev_baseline_rows is None:
            # Read once: each store-only plan writes its reused rows to rows.jsonl again.
            self.dev_baseline_rows = self._plan(self._cells(self.panel.dev, BASELINE_MODE, self._reps()), None,
                                                store_only=True)
        return self._delta_report(result, "dev", result.rows, self.dev_baseline_rows)

    # --- finish ---

    def _finish_failed(self, line: str) -> None:
        """Log why finish opened no PR when it should have tried; the run then exits nonzero."""
        self.finish_failure = line
        self.log(line)

    def finish(self):
        """Pick the best-dev non-seed candidate, confirm it against the seed on the test split, and open a draft PR.

        The dev mean selects the winner; the seed wins a tie. The test split scores only the seed and that
        winner. The winner must beat the seed on the paired, task-level 95% bootstrap interval: the per-task
        score differences (winner minus seed, over each task's reps) need a lower bound above 0, and the
        winner's aggregate test pass rate must not fall below the seed's.
        """
        self.ledger.reserve = 0.0
        no_spend = self.stop.reason in NO_SPEND_REASONS
        seed = self.new_cascade(self.seed)
        if seed.sha is None:
            seed.sha = self.settings.seed_sha
        nonseed = sorted((member for member in self.frontier if member.cid != self.seed_id),
                         key=lambda member: (-member.dev_mean, member.seq))
        if not nonseed:
            if self.stop.is_set and self.stop.reason != "plateau":
                return self._finish_failed(f"finish: incomplete ({self.stop.reason}); the search accepted no candidate")
            self.log("finish: no improvement (no non-seed candidate on the frontier)")
            return None
        winner = nonseed[0]
        if winner.dev_mean <= seed.dev_mean:
            if self.stop.is_set and self.stop.reason != "plateau":
                return self._finish_failed(f"finish: incomplete ({self.stop.reason}); no accepted candidate beats "
                                           "the seed's dev mean")
            self.log("finish: no improvement (no candidate beats the seed's dev mean)")
            return None
        finalists = [winner, seed]
        self.log("finish: finalists " + ", ".join(f"{member.cid[:12]} (dev mean {member.dev_mean:.3f})"
                                                  for member in finalists))
        reps = self._reps()
        try:
            baseline_rows = self._plan(self._cells(self.panel.test, BASELINE_MODE, reps), None, store_only=no_spend)
            test_rows = {member.cid: self._plan(self._cells(self.panel.test, CANDIDATE_MODE, reps), member.sha,
                                                store_only=no_spend)
                         for member in finalists}
        except _Stopped as stopped:
            # The reason this finish stopped, which may be a ceiling or quota after an earlier plateau.
            return self._finish_failed(f"finish: incomplete ({stopped})")
        except run.BaselineDrift as error:
            return self._finish_failed(f"finish: incomplete (baseline drift: {error})")
        except run.CandidateBuildFailed as error:
            return self._finish_failed(f"finish: incomplete (build failed)\n{tail(str(error))}")
        test_means = {cid: mean(correct(row) for row in rows) if rows else 0.0 for cid, rows in test_rows.items()}
        scores = ", ".join(f"{member.cid[:12]}={test_means[member.cid]:.3f}" for member in finalists)
        expected = len(self.panel.test) * len(reps)
        if no_spend and any(len(test_rows[member.cid]) < expected for member in finalists):
            return self._finish_failed(f"finish: incomplete ({self.stop.reason}); stored test-split scores: {scores}")
        unfinished = [member.cid[:12] for member in finalists
                      if not all(baselines.is_completed(row) for row in test_rows[member.cid])]
        if not all(baselines.is_completed(row) for row in baseline_rows):
            unfinished.append("baseline")
        if unfinished:
            return self._finish_failed(f"finish: incomplete (test-split cells did not complete for "
                                       f"{', '.join(unfinished)}); test-split means not compared: {scores}")
        self.log(f"finish: test-split means {scores}")
        test_base = _baseline_costs(baseline_rows, self.panel.test)
        deltas = _paired_score_deltas(test_rows[winner.cid], test_rows[seed.cid], self.panel.test, test_base)
        if not deltas:
            self.log("finish: no improvement (no test task has paired winner and seed rows)")
            return None
        point = sum(deltas) / len(deltas)
        low, high = stats.paired_bootstrap_ci(deltas, n_resamples=10_000, seed=0)
        pass_delta = test_means[winner.cid] - test_means[seed.cid]
        paired_line = f"paired test delta {point:+.3f} (95% CI [{low:.3f}, {high:.3f}], {len(deltas)} tasks)"
        pass_line = f"test pass rate delta {pass_delta:+.3f}"
        self.log(f"finish: {paired_line}; {pass_line}")
        if not low > 0:
            self.log("finish: no improvement (the paired test-split interval does not exclude 0)")
            return None
        if test_means[winner.cid] < test_means[seed.cid]:
            self.log("finish: no improvement (the winner's test pass rate is below the seed's)")
            return None
        if not winner.just_check_ok:
            return self._finish_failed(f"finish: refused; winner {winner.cid[:12]} has no just check pass "
                                       "in its cascade record")
        changed = git("diff", "--name-only", "--no-renames", self.settings.seed_sha, winner.sha,
                      cwd=self.settings.repo).split()
        if outside := outside_allowlist(changed):
            return self._finish_failed(f"finish: refused; winner {winner.cid[:12]} changes {', '.join(outside)}")
        dev_delta = winner.delta or self._dev_delta(winner)
        test_delta = self._delta_report(winner, "test", test_rows[winner.cid], baseline_rows)
        branch = f"evolve/{self.settings.run_id}-winner"
        body = "\n".join([
            f"Evolve run `{self.settings.run_id}` winner `{winner.cid[:12]}` (commit {winner.sha}).", "",
            f"Test-split means: {scores}", "",
            f"Winner minus seed, {paired_line}", "",
            f"Winner minus seed, {pass_line}", "",
            "## Re-pin before landing", REPIN_NOTE, "",
            "## dev delta", "```json", json.dumps(dev_delta, indent=2, sort_keys=True), "```", "",
            "## test delta", "```json", json.dumps(test_delta, indent=2, sort_keys=True), "```",
        ])
        self.log(f"finish: {REPIN_NOTE}")
        try:
            self.pr_client.push(winner.sha, branch)
        except GitError as error:
            return self._finish_failed(f"finish: refused; pushing {branch} failed (it may already exist): {error}")
        try:
            pr = self.pr_client.create_draft(base=self.settings.base_branch, head=branch,
                                             title=f"evolve {self.settings.run_id}: candidate {winner.cid[:12]}",
                                             body=body)
        except (subprocess.CalledProcessError, OSError) as error:
            detail = getattr(error, "stderr", None) or str(error)
            return self._finish_failed(f"finish: pushed {branch} but opening the draft PR failed: "
                                       f"{tail(str(detail))}")
        self.log(f"finish: draft PR opened from {branch}: {pr}")
        return pr

    # --- the whole run ---

    def run(self) -> int:
        code = self.preflight()
        if code is None and not self.stop.is_set:
            code = self.buy_baselines()
        if code:
            return code
        try:
            if not self.stop.is_set:
                self.search()
            self.finish()
        except (GitError, OSError, EvolveError, run.CandidateBuildFailed) as error:
            self._finish_failed(f"finish: run failed ({type(error).__name__}): {error}")
        finally:
            self.materializer.cleanup()
            totals = {**self.spend, "reflection": self.paid.total("reflection"),
                      "proposer": self.paid.total("proposer")}
            self.log("spend: " + " ".join(f"{kind}=${value:.4f}" for kind, value in totals.items())
                     + f" total=${self.ledger.spent:.4f} of ${self.settings.max_usd:.2f}")
        return 1 if self.finish_failure else 0
