"""One evolve run: judge preflight, frozen baselines, the cascade, evolve's accepted frontier, and finish.

A candidate's score is grader correctness only: the mean ``correct`` over its
paid-tier dev rollouts, a contaminated rollout counted incorrect. Reflection and
the proposer see only c4 stripped records; test-split rollouts appear only in
``finish``.
"""

import json
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean

import baselines
import external.data
import paired
import panels
import run
from judge import core as judge
from spend import SpendLedger

from . import engine
from .calls import PaidCalls, StopState
from .candidate import content_id
from .finish import PRClient, outside_allowlist
from .gitops import git
from .materialize import ApplyRejected, Materializer
from .proposer import Proposer

CANDIDATE_MODE = "tilth"
BASELINE_MODE = "baseline"
TAIL_LINES = 40


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
    refreeze_baselines: bool
    panel_path: Path
    helper_model: str = judge.config.JUDGE_MODEL


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
    check_tail: str = ""
    cheap_score: float | None = None
    cheap_scores: dict[str, float] = field(default_factory=dict)
    cheap_records: list[dict] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    means: dict[str, float] = field(default_factory=dict)
    rows: list[dict] = field(default_factory=list)
    records: dict[str, list[dict]] = field(default_factory=dict)
    reached_paid: bool = False
    admitted: bool = False
    accepted: bool = False
    counted: bool = False
    done: bool = False
    delta: dict | None = None

    @property
    def dev_mean(self) -> float:
        values = self.means or self.scores
        return mean(values.values()) if values else 0.0

    def score(self, task: str) -> float:
        return (self.means if self.accepted else self.scores).get(task, 0.0)

    def fail(self, stage: str, tail: str = "") -> "Cascade":
        self.stage, self.tail, self.done = stage, tail, True
        return self

    def side_info(self, task: str) -> dict:
        if self.stage in {"apply", "just check", "build"}:
            return {"stage": self.stage, "tail": self.tail}
        if self.stage == "cheap tier":
            return {"stage": self.stage, "task_scores": dict(self.cheap_scores), "records": self.cheap_records}
        return {"stage": "paid", "task_scores": {name: self.score(name) for name in self.scores},
                "records": self.records.get(task, [])}


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
        self.calibrated = False
        self.reserve = 0.0
        self.spend: dict[str, float] = {"cells": 0.0, "judge": 0.0}
        self.best: float | None = None
        self.unimproved = 0
        self.gepa_result = None

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
        return range(1, self.settings.reruns + 2)

    def _cells(self, tasks: Iterable[str], mode: str, reps: Iterable[int]) -> list[run.CellSpec]:
        reps = list(reps)
        return [run.CellSpec(task, mode, self.settings.model, rep) for task in tasks for rep in reps]

    def _plan(self, cells: list[run.CellSpec], sha: str | None, *, store_only: bool = False) -> list[dict]:
        try:
            with self._spending("cells"):
                rows = run.run_plan(cells, panel=self.panel, ledger=self.ledger, candidate_sha=sha,
                                    refreeze_baselines=self.settings.refreeze_baselines, output=self.rows_path,
                                    cell_estimate_usd=self.settings.cell_estimate_usd, store_only=store_only,
                                    repo=self.settings.repo)
        except run.PlanStopped as stopped:
            self.log(f"cells: stopped ({stopped})")
            self.stop.set(stopped.reason)
            raise _Stopped(stopped.reason) from stopped
        for row in rows:
            correct(row)
        return rows

    def new_cascade(self, candidate: Mapping[str, str]) -> Cascade:
        cid = content_id(candidate)
        if cid not in self.results:
            self.results[cid] = Cascade(cid=cid, seq=len(self.results) + 1, candidate=dict(candidate))
        return self.results[cid]

    # --- preflight ---

    def finalist_test_cost(self) -> float:
        """c1's estimate over one finalist's test-split cells plus the test-split baseline cells not stored."""
        history = baselines.load_rows(run.RESULTS_DIR / baselines.STORE_FILENAME)
        stored = baselines.completed_by_key(history)
        model = run.MODELS[self.settings.model]
        cost = 0.0
        for cell in self._cells(self.panel.test, CANDIDATE_MODE, self._reps()):
            cost += run.estimate_cell_cost(history, task=cell.task, mode=CANDIDATE_MODE, model=model,
                                           run_max_cost=None, fallback=self.settings.cell_estimate_usd)
        for cell in self._cells(self.panel.test, BASELINE_MODE, self._reps()):
            if run.planned_identity(cell)["run_key"] not in stored:
                cost += run.estimate_cell_cost(history, task=cell.task, mode=BASELINE_MODE, model=model,
                                               run_max_cost=None, fallback=self.settings.cell_estimate_usd)
        return cost

    def refuse(self, message: str) -> int:
        self.log(f"refused: {message}")
        print(f"error: {message}", file=sys.stderr)
        return 2

    def preflight(self) -> int | None:
        """Hold the finish reserve, label every panel task, and calibrate; an exit code when the run is refused."""
        per_finalist = self.finalist_test_cost()
        if self.settings.max_usd < per_finalist:
            return self.refuse(f"--max-usd ${self.settings.max_usd:.2f} cannot cover one finalist's test-split cost "
                               f"${per_finalist:.2f}; short by ${per_finalist - self.settings.max_usd:.2f}")
        self.reserve = min(3 * per_finalist, self.settings.max_usd)
        self.ledger.reserve = self.reserve
        self.log(f"reserve: ${self.reserve:.4f} held for finish (per finalist ${per_finalist:.4f})")

        unlabeled = []
        for name in self.panel.select("all"):
            try:
                with self._spending("judge"):
                    self.labels[name] = self.judge.applicability(run.TASKS[name])
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
        except judge.JudgeError as error:
            reason = str(error)
        self.calibrated = bool(agreement is not None and agreement.calibrated)
        if agreement is not None:
            kappas = " ".join(f"{name}={value:.2f}" if value is not None else f"{name}=undefined"
                              for name, value in (("label_kappa", agreement.label_kappa),
                                                  ("verdict_kappa", agreement.verdict_kappa)))
            self.log(f"labels: agreement {kappas} calibrated={self.calibrated}")
        if not self.calibrated:
            self.log(f"labels: uncalibrated ({reason or 'no agreement record'})")
        return None

    def buy_baselines(self) -> int | None:
        """Buy each dev and cheap baseline cell once; later runs answer them from the store."""
        cells = self._cells((*self.panel.cheap, *self.panel.dev), BASELINE_MODE, self._reps())
        try:
            self._plan(cells, None)
        except run.BaselineDrift as error:
            return self.refuse(str(error))
        except _Stopped:
            pass
        return None

    # --- search ---

    def search(self):
        self.gepa_result = engine.optimize(self.seed, evaluator=self.evaluate, dataset=self.panel.dev,
                                           max_metric_calls=self.settings.max_metric_calls, stop=self.stop,
                                           proposer=self.dispatcher)
        self.log(f"search: ended ({self.stop.reason or 'metric-call budget'})")
        return self.gepa_result

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
        return result.score(example), result.side_info(example)

    def cascade(self, candidate: Mapping[str, str]) -> Cascade:
        """Apply, ``just check``, build, cheap tier, paid tier; each stage memoized per content id."""
        result = self.new_cascade(candidate)
        if result.done:
            return result
        if result.sha is None:
            try:
                result.sha = self.materializer.materialize(candidate)
            except ApplyRejected as rejected:
                return result.fail("apply", tail(rejected.tail))
        if result.just_check_ok is None:
            ok, output = self.just_check(self.materializer.worktree(result.sha))
            result.just_check_ok, result.check_tail = ok, tail(output)
        if not result.just_check_ok:
            return result.fail("just check", result.check_tail)
        try:
            return self._tiers(result)
        except run.CandidateBuildFailed as failed:
            return result.fail("build", tail(str(failed)))

    def _tiers(self, result: Cascade) -> Cascade:
        """The cheap and paid tiers; the first cell of either builds the candidate binary."""
        if result.cheap_score is None:
            rows = self._plan(self._cells(self.panel.cheap, CANDIDATE_MODE, [1]), result.sha)
            records = self._records(rows)
            result.cheap_records = [record for task in self.panel.cheap for record in records.get(task, [])]
            result.cheap_scores = {task: self._task_mean(rows, task) for task in self.panel.cheap}
            result.cheap_score = mean(correct(row) for row in rows) if rows else 0.0
        if result.cid != self.seed_id:
            seed = self.cascade(self.seed)
            if result.cheap_score < (seed.cheap_score or 0.0):
                return result.fail("cheap tier")
        if not result.scores:
            rows = self._plan(self._cells(self.panel.dev, CANDIDATE_MODE, [1]), result.sha)
            records = self._records(rows)
            result.rows = rows
            result.records = {task: records.get(task, []) for task in self.panel.dev}
            result.scores = {task: self._task_mean(rows, task) for task in self.panel.dev}
        result.reached_paid = True
        self._admit(result)
        if result.delta is None:
            result.delta = self._dev_delta(result)
        if not result.counted:
            result.counted = True
            self._count_plateau()
        result.done = True
        return result

    def _task_mean(self, rows: list[dict], task: str) -> float:
        scored = [correct(row) for row in rows if row["task"] == task]
        return mean(scored) if scored else 0.0

    def _records(self, rows: list[dict]) -> dict[str, list[dict]]:
        """One c4 stripped record per uncontaminated rollout, plus ``label`` and ``critique`` when calibrated."""
        records: dict[str, list[dict]] = {}
        for row in rows:
            if row["contaminated"] is True:
                continue
            path = row.get("trajectory_path")
            sidecar = Path(path).read_text() if path and Path(path).is_file() else None
            record = judge.stripped_record(row, sidecar or "")
            if self.calibrated:
                record["label"] = self.labels.get(row["task"])
                try:
                    with self._spending("judge"):
                        record["critique"] = self.judge.critique(row, sidecar)
                except judge.JudgeQuota:
                    self.stop.set("quota")
                    raise _Stopped("quota") from None
                except judge.JudgeSpendCeiling:
                    self.stop.set("ceiling")
                    raise _Stopped("ceiling") from None
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
        first = [row for row in result.rows if row["repetition"] == 1]
        if not all(baselines.is_completed(row) for row in first) or len(first) < len(dev):
            result.admitted = True
            self.log(f"candidate {result.cid[:12]}: a first rollout did not complete; not re-run")
            return
        if any(dominates(member.means, result.scores, dev) for member in members):
            result.admitted = True
            self.log(f"candidate {result.cid[:12]}: dominated after one rollout; not re-run")
            return
        reruns = self._plan(self._cells(dev, CANDIDATE_MODE, range(2, self.settings.reruns + 2)), result.sha)
        records = self._records(reruns)
        for task in dev:
            result.records[task] = result.records.get(task, []) + records.get(task, [])
        result.rows = [*result.rows, *reruns]
        result.admitted = True
        means = {task: self._task_mean(result.rows, task) for task in dev}
        if not all(baselines.is_completed(row) for row in reruns) or len(reruns) < len(dev) * self.settings.reruns:
            self.log(f"candidate {result.cid[:12]}: a re-run did not complete; not accepted")
            return
        if any(dominates(member.means, means, dev) for member in members):
            self.log(f"candidate {result.cid[:12]}: dominated after {self.settings.reruns + 1} rollouts; not accepted")
            return
        result.means, result.accepted = means, True
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
            report["labels"] = {label: accuracy([row for row in rows if self.labels.get(row["task"]) == label])
                                for label in sorted(set(self.labels.values()))}
        else:
            report["labels"] = "uncalibrated"
        self.deltas.append(report)
        self.log(f"delta {result.cid[:12]} {split}: {json.dumps(report, sort_keys=True)}")
        return report

    def _dev_delta(self, result: Cascade) -> dict:
        baseline_rows = self._plan(self._cells(self.panel.dev, BASELINE_MODE, self._reps()), None, store_only=True)
        return self._delta_report(result, "dev", result.rows, baseline_rows)

    # --- finish ---

    def finish(self):
        """Score the finalists once on the test split and open a draft PR for a non-seed winner."""
        self.ledger.reserve = 0.0
        quota = self.stop.reason == "quota"
        seed = self.new_cascade(self.seed)
        if seed.sha is None:
            seed.sha = self.settings.seed_sha
        nonseed = sorted((member for member in self.frontier if member.cid != self.seed_id),
                         key=lambda member: (-member.dev_mean, member.seq))[:2]
        finalists = [*nonseed, seed]
        self.log("finish: finalists " + ", ".join(f"{member.cid[:12]} (dev mean {member.dev_mean:.3f})"
                                                  for member in finalists))
        reps = self._reps()
        try:
            baseline_rows = self._plan(self._cells(self.panel.test, BASELINE_MODE, reps), None, store_only=quota)
            test_rows = {member.cid: self._plan(self._cells(self.panel.test, CANDIDATE_MODE, reps), member.sha,
                                                store_only=quota)
                         for member in finalists}
        except _Stopped:
            self.log(f"finish: incomplete ({self.stop.reason})")
            return None
        test_means = {cid: mean(correct(row) for row in rows) if rows else 0.0 for cid, rows in test_rows.items()}
        scores = ", ".join(f"{member.cid[:12]}={test_means[member.cid]:.3f}" for member in finalists)
        expected = len(self.panel.test) * len(reps)
        if quota and any(len(test_rows[member.cid]) < expected for member in finalists):
            self.log(f"finish: incomplete (quota); stored test-split scores: {scores}")
            return None
        self.log(f"finish: test-split means {scores}")
        winner = max(finalists, key=lambda member: (test_means[member.cid], member.dev_mean, -member.seq))
        if winner is seed:
            self.log("finish: no improvement (the seed has the best test-split mean)")
            return None
        if not winner.just_check_ok:
            self.log(f"finish: refused; winner {winner.cid[:12]} has no just check pass in its cascade record")
            return None
        changed = git("diff", "--name-only", "--no-renames", self.settings.seed_sha, winner.sha,
                      cwd=self.settings.repo).split()
        if outside := outside_allowlist(changed):
            self.log(f"finish: refused; winner {winner.cid[:12]} changes {', '.join(outside)}")
            return None
        dev_delta = winner.delta or self._dev_delta(winner)
        test_delta = self._delta_report(winner, "test", test_rows[winner.cid], baseline_rows)
        branch = f"evolve/{self.settings.run_id}-winner"
        body = "\n".join([
            f"Evolve run `{self.settings.run_id}` winner `{winner.cid[:12]}` (commit {winner.sha}).", "",
            f"Test-split means: {scores}", "",
            "## dev delta", "```json", json.dumps(dev_delta, indent=2, sort_keys=True), "```", "",
            "## test delta", "```json", json.dumps(test_delta, indent=2, sort_keys=True), "```",
        ])
        self.pr_client.push(winner.sha, branch)
        pr = self.pr_client.create_draft(base=self.settings.base_branch, head=branch,
                                         title=f"evolve {self.settings.run_id}: candidate {winner.cid[:12]}",
                                         body=body)
        self.log(f"finish: draft PR opened from {branch}: {pr}")
        return pr

    # --- the whole run ---

    def run(self) -> int:
        code = self.preflight() or self.buy_baselines()
        if code:
            return code
        try:
            if not self.stop.is_set:
                self.search()
            self.finish()
        finally:
            self.materializer.cleanup()
        totals = {**self.spend, "reflection": self.paid.total("reflection"), "proposer": self.paid.total("proposer")}
        self.log("spend: " + " ".join(f"{kind}=${value:.4f}" for kind, value in totals.items())
                 + f" total=${self.ledger.spent:.4f} of ${self.settings.max_usd:.2f}")
        return 0
