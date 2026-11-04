"""The pinned model judge: applicability labels, stripped-record critiques, and calibration.

Every judge call is an isolated, tool-less ``claude -p`` run on the subscription
(``CLAUDE_CODE_OAUTH_TOKEN``). It shares the run's spend ledger, and a usage-limit
rejection stops every later judge call in the process. Labels and critiques live in
the judge caches only; they never enter result rows or the result store.
"""

import json
import math
import re
import shlex
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from statistics import mean
from typing import Literal, Protocol

import auth
import baselines
import external
import run
from jsonl import tolerant_jsonl
from parse import detect_quota_rejection, parse_stream_json, stream_native_cost
from spend import SpendLedger
from tasks import TASKS

from . import config, store
from .store import Agreement, Kind

Label = Literal["strong", "weak", "none"]
WithholdReason = Literal["contaminated", "no-trajectory", "uncalibrated"]

# Row fields a critique may see, besides integer f2p_*/p2p_* test counts.
ROW_FIELDS = (
    "task", "mode", "model", "repetition", "num_turns", "num_tool_calls", "tool_calls",
    "total_cost_usd", "duration_ms", "result_text", "correct", "pass_rate",
)
COUNT_PREFIXES = ("f2p_", "p2p_")

# Set by the first usage-limit rejection; no judge call starts in this process after it.
_quota_reason: str | None = None


class JudgeError(RuntimeError):
    """A judge call was refused, failed, or answered outside its fixed answer set."""


class JudgeAnswerInvalid(JudgeError):
    """The applicability answer is not exactly one label."""


class CritiqueRejected(JudgeError):
    """The critique does not start with a valid ``verdict:`` line."""


class CritiqueWithheld(JudgeError):
    def __init__(self, reason: WithholdReason) -> None:
        super().__init__(f"critique withheld: {reason}")
        self.reason = reason


class UnresolvableTask(JudgeError):
    def __init__(self, task: object) -> None:
        super().__init__(f"task {task} does not resolve to a local task or a cached external instance")


class JudgeQuota(JudgeError):
    """A subscription usage limit rejected a judge call."""


class JudgeSpendCeiling(JudgeError):
    """The next judge call's estimate would cross the run's spend ceiling."""


class JudgeCallFailed(JudgeError):
    """The judge process failed or produced no result."""


class CalibrationInvalid(ValueError):
    """The hand-labelled calibration file has entries the judge cannot use."""


@dataclass(frozen=True)
class JudgeReply:
    text: str
    cost: float | None = None
    cost_source: Literal["native", "pricing"] = "native"
    quota: str | None = None
    error: str | None = None
    auth_error: str | None = None


class JudgeClient(Protocol):
    def __call__(self, prompt: str) -> JudgeReply: ...


_external_tasks: dict[str, object] = {}


def _load_external(name: str) -> object | None:
    """The ExternalTask for a cached instance row, else None; never admits, downloads, or builds."""
    if name not in _external_tasks:
        if external.cached_row(name) is None:
            return None
        _external_tasks[name] = external.registry.load(name)
    return _external_tasks[name]


def default_resolve_task(name: str) -> object | None:
    """A local task from the registry, else a cached external instance, else None.

    An external instance skips admission: the judge reads the task statement and gold patch only,
    so it needs no environment build and no paid admission round trip.
    """
    task = TASKS.get(name)
    return task if task is not None else _load_external(name)


def trusted_reference(task: object) -> str | None:
    """The held-out tests that sit in the task module's ``HELD_OUT`` directory, else None."""
    held_out = getattr(sys.modules.get(type(task).__module__), "HELD_OUT", None)
    if held_out is None:
        return None
    files = sorted(Path(held_out).glob("*_heldout_test.go"))
    return "\n".join(f"// {path.name}\n{path.read_text()}" for path in files) or None


def stripped_record(
    row: Mapping[str, object], sidecar: str,
    resolve_task: Callable[[str], object | None] = default_resolve_task,
) -> dict:
    """The one grader-free view of a rollout: task prompt, allowlisted row fields, verbatim trajectory."""
    task = resolve_task(row.get("task"))
    if task is None:
        raise UnresolvableTask(row.get("task"))
    fields = {name: row[name] for name in ROW_FIELDS if name in row}
    fields.update({
        name: value for name, value in row.items()
        if name.startswith(COUNT_PREFIXES) and isinstance(value, int) and not isinstance(value, bool)
    })
    return {"prompt": task.prompt, "row": fields, "trajectory": sidecar}


def _fenced(text: str) -> str:
    """Fence text as data; the fence is longer than any backtick run in the text, so the text cannot close it."""
    longest = max((len(ticks) for ticks in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def _prompt(kind: Kind, sections: list[tuple[str, str]]) -> str:
    body = "\n\n".join(f"## {heading}\n\n{_fenced(text)}" for heading, text in sections)
    return f"{store.prompt_template(kind)}\n{body}\n"


def label_prompt(task: object) -> str:
    """Applicability input: an external task's statement and gold patch, or a local task's grading inputs."""
    if hasattr(task, "problem_statement") and hasattr(task, "gold_patch"):
        return _prompt("applicability", [
            ("Problem statement", task.problem_statement), ("Gold patch", task.gold_patch)])
    ground_truth = task.ground_truth
    sections = [
        ("Task prompt", task.prompt),
        ("Ground truth", json.dumps(asdict(ground_truth) if is_dataclass(ground_truth) else ground_truth, indent=2)),
    ]
    test_command = list(getattr(task, "test_command", ()) or ())
    if test_command:
        sections.append(("Test command", shlex.join(test_command)))
    reference = trusted_reference(task)
    if reference:
        sections.append(("Trusted reference", reference))
    return _prompt("applicability", sections)


def critique_prompt(record: Mapping[str, object]) -> str:
    """Render a stripped record; the prompt, final answer, and trajectory stay verbatim."""
    fields = dict(record["row"])
    final_answer = str(fields.pop("result_text", ""))
    return _prompt("critique", [
        ("Task prompt", record["prompt"]),
        ("Row fields", json.dumps(fields, indent=2, sort_keys=True)),
        ("Final answer", final_answer),
        ("Trajectory (one JSON tool call per line)", record["trajectory"]),
    ])


def parse_verdict(answer: str) -> str | None:
    first_line = answer.split("\n", 1)[0].strip()
    verdict = first_line.removeprefix("verdict: ")
    return verdict if first_line.startswith("verdict: ") and verdict in config.VERDICTS else None


def cohen_kappa(pairs: list[tuple[str, str | None]]) -> float | None:
    """Unweighted Cohen's kappa of (hand, judge) pairs; a None judge answer never agrees.

    None when there are no pairs or the expected agreement is 1 (kappa undefined).
    """
    if not pairs:
        return None
    count = len(pairs)
    observed = sum(hand == judged for hand, judged in pairs) / count
    hand_counts = Counter(hand for hand, _ in pairs)
    judged_counts = Counter(judged for _, judged in pairs)
    expected = sum(hand_counts[label] * judged_counts[label] for label in hand_counts) / count ** 2
    if math.isclose(expected, 1.0):
        return None
    return (observed - expected) / (1 - expected)


@dataclass(frozen=True)
class CalibrationTrajectory:
    run_key: str
    verdict: str
    row: dict
    trajectory: str


@dataclass(frozen=True)
class CalibrationSet:
    tasks: dict[str, str]
    trajectories: tuple[CalibrationTrajectory, ...]
    digest: str


def load_calibration(
    path: Path | None = None, *,
    resolve_task: Callable[[str], object | None] = default_resolve_task,
) -> CalibrationSet:
    """Validate the hand-labelled calibration file against the result store before any judge call."""
    path = path or config.CALIBRATION_FILE
    try:
        raw = path.read_bytes()
    except FileNotFoundError as error:
        raise CalibrationInvalid(f"{path}: missing") from error
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise CalibrationInvalid(f"{path}: not JSON ({error})") from error
    if not (isinstance(data, dict) and isinstance(data.get("tasks"), dict)
            and isinstance(data.get("trajectories"), list)):
        raise CalibrationInvalid(f'{path}: expected {{"tasks": {{name: label}}, "trajectories": [{{run_key, verdict}}]}}')

    problems = []
    for name, label in data["tasks"].items():
        if label not in config.LABELS:
            problems.append(f"tasks.{name}: unknown label {label!r}; expected one of {', '.join(config.LABELS)}")
        if resolve_task(name) is None:
            problems.append(f"tasks.{name}: unknown task")
    stored = baselines.completed_by_key(baselines.load_rows(config.RESULT_STORE))
    trajectories = []
    seen: set[str] = set()
    for index, entry in enumerate(data["trajectories"]):
        run_key = entry.get("run_key") if isinstance(entry, dict) else None
        if not isinstance(run_key, str):
            problems.append(f"trajectories[{index}]: expected {{run_key, verdict}}")
            continue
        where = f"trajectories[{index}] run_key {run_key}"
        if run_key in seen:
            problems.append(f"{where}: duplicate run_key; each trajectory counts once in kappa")
            continue
        seen.add(run_key)
        verdict = entry.get("verdict")
        if verdict not in config.VERDICTS:
            problems.append(f"{where}: unknown verdict {verdict!r}; expected one of {', '.join(config.VERDICTS)}")
        row = stored.get(run_key)
        if row is None:
            problems.append(f"{where}: no completed stored row")
            continue
        if resolve_task(row.get("task")) is None:
            problems.append(f"{where}: task {row.get('task')} does not resolve")
        sidecar = row.get("trajectory_path")
        if not sidecar or not Path(sidecar).is_file():
            problems.append(f"{where}: no trajectory")
            continue
        if row.get("contaminated") is not False:
            problems.append(f"{where}: contaminated (contaminated is {row.get('contaminated', 'absent')})")
        trajectories.append(CalibrationTrajectory(run_key, verdict, row, Path(sidecar).read_text()))
    if problems:
        raise CalibrationInvalid(f"{path} refused:\n" + "\n".join(f"  {problem}" for problem in problems))
    return CalibrationSet(dict(data["tasks"]), tuple(trajectories), store.sha256(raw))


def calibration_shortfall(labels: CalibrationSet) -> str | None:
    """Why the set is too small to calibrate, or None."""
    shortfalls = []
    if not labels.tasks:
        shortfalls.append("calibration set hand-labels no task")
    if len(labels.trajectories) < config.MIN_CALIBRATION_TRAJECTORIES:
        shortfalls.append(f"calibration set has {len(labels.trajectories)} trajectories; "
                          f"needs at least {config.MIN_CALIBRATION_TRAJECTORIES}")
    return "; ".join(shortfalls) or None


def uncached_labels(tasks: Mapping[str, object]) -> list[str]:
    """The names of the tasks whose label is not cached."""
    return [name for name, task in tasks.items() if store.cached_label(run._cell_task_digest(task)) is None]


def pending_calls(
    labels: CalibrationSet, resolve_task: Callable[[str], object | None] = default_resolve_task,
) -> list[str]:
    """The calibration items ``Judge.calibrate`` would send to the judge uncached."""
    if calibration_shortfall(labels) is not None:
        return []
    return [
        *uncached_labels({name: resolve_task(name) for name in labels.tasks}),
        *(entry.run_key for entry in labels.trajectories if store.cached_critique(entry.run_key) is None),
    ]


class ClaudeJudgeClient:
    """One isolated, tool-less ``claude -p`` call per prompt, the prompt on stdin.

    With a bounded ``ledger``, each call passes the ledger's unreserved headroom (ceiling minus spend
    minus the finish reserve) as ``--max-budget-usd``, so one call cannot spend past the run's ceiling
    or into the reserve.
    """

    def __init__(self, *, spawn: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                 timeout_s: int = 600, ledger: SpendLedger | None = None) -> None:
        self.spawn = spawn
        self.timeout_s = timeout_s
        self.ledger = ledger

    @staticmethod
    def argv(max_budget_usd: float | None = None) -> list[str]:
        # No --bare (it refuses OAuth) and no --mcp-config: --strict-mcp-config then loads no server.
        argv = [
            "claude", "-p", "--output-format", "stream-json", "--verbose", "--model", config.JUDGE_MODEL,
            "--tools", "", "--strict-mcp-config", "--setting-sources", "",
            "--no-session-persistence", "--disable-slash-commands",
        ]
        if max_budget_usd is not None:
            argv += ["--max-budget-usd", f"{max_budget_usd:.4f}"]
        return argv

    def _headroom(self) -> float | None:
        return None if self.ledger is None else self.ledger.headroom()

    def __call__(self, prompt: str) -> JudgeReply:
        env = run.build_runner_env("claude", tilth_bin=None)
        with (tempfile.TemporaryDirectory(prefix="tilth-judge-cwd-") as cwd,
              run.isolated_claude_config(env, "tilth-judge-config-")):
            try:
                completed = self.spawn(self.argv(self._headroom()), input=prompt, capture_output=True, text=True,
                                       cwd=cwd, env=env, timeout=self.timeout_s)
            except subprocess.TimeoutExpired as error:
                output = error.stdout.decode(errors="replace") if isinstance(error.stdout, bytes) else error.stdout
                return _reply(output or "", None, f"judge call timed out after {self.timeout_s}s")
            except OSError as error:
                # No process ran (for example, no claude binary on PATH), so nothing is charged.
                raise JudgeCallFailed(f"judge call could not start claude: {error}") from error
        return _reply(completed.stdout, completed.returncode, completed.stderr)


def _auth_error(stdout: str) -> str | None:
    try:
        auth.check_captured(stdout)
    except auth.ClaudeAuthError as error:
        return str(error)
    return None


def _reply(stdout: str, returncode: int | None, stderr: str) -> JudgeReply:
    native = stream_native_cost(stdout)
    cost, source = native, "native"
    if native is None:
        source = "pricing"
        try:
            cost = parse_stream_json(stdout, config.JUDGE_MODEL).total_cost_usd
        except ValueError:
            cost = None
    results = [event for event in tolerant_jsonl(stdout) if event.get("type") == "result"]
    final = results[-1] if results else {}
    text = final.get("result") if isinstance(final.get("result"), str) else ""
    error = None
    if returncode != 0 or not results or final.get("is_error"):
        error = (text if final.get("is_error") else "") or stderr.strip()[-500:] or f"claude exited {returncode}"
    auth_error = _auth_error(stdout)
    return JudgeReply(text="" if auth_error else text, cost=cost, cost_source=source,
                      quota=detect_quota_rejection(stdout), error=error, auth_error=auth_error)


class Judge:
    """The run's judge: every uncached call is estimated, ceiling-checked, and charged to ``ledger``."""

    def __init__(
        self, ledger: SpendLedger, client: JudgeClient | None = None,
        resolve_task: Callable[[str], object | None] = default_resolve_task, *,
        cell_estimate_usd: float,
    ) -> None:
        self.ledger = ledger
        self.client = client if client is not None else ClaudeJudgeClient(ledger=ledger)
        self.resolve_task = resolve_task
        self.cell_estimate_usd = cell_estimate_usd
        self._largest_cost: float | None = None

    def applicability(self, task: object) -> Label:
        digest = run._cell_task_digest(task)
        cached = store.cached_label(digest)
        if cached is not None:
            return cached
        answer, cost = self._call("applicability", label_prompt(task))
        label = answer.strip().lower()
        if label not in config.LABELS:
            raise JudgeAnswerInvalid(f"{getattr(task, 'name', task)}: judge answered {answer!r}; "
                                     f"expected one of {', '.join(config.LABELS)}")
        store.put_label(task=getattr(task, "name", ""), task_digest=digest, label=label, cost=cost)
        return label

    def critique(self, row: Mapping[str, object], trajectory: str | None) -> str:
        if row.get("contaminated") is not False:
            raise CritiqueWithheld("contaminated")
        if row.get("trajectory_path") is None or trajectory is None:
            raise CritiqueWithheld("no-trajectory")
        agreement = store.current_agreement()
        if agreement is None or not agreement.calibrated:
            raise CritiqueWithheld("uncalibrated")
        return self._critique(row, trajectory)

    def calibrate(self, labels: CalibrationSet) -> Agreement:
        """Measure agreement with the hand labels and record it, calibrated or not."""
        stamp = {**store.judge_stamp(), "calibration_digest": labels.digest}
        counts = {"label_count": len(labels.tasks), "verdict_count": len(labels.trajectories),
                  "tasks": tuple(sorted(labels.tasks))}
        shortfall = calibration_shortfall(labels)
        if shortfall is not None:
            agreement = Agreement(calibrated=False, reason=shortfall, **counts, **stamp)
            store.write_agreement(agreement)
            return agreement
        label_kappa = cohen_kappa([(hand, self._judged_label(name)) for name, hand in labels.tasks.items()])
        verdict_kappa = cohen_kappa([(entry.verdict, self._judged_verdict(entry)) for entry in labels.trajectories])
        reasons = []
        for dimension, kappa in (("label", label_kappa), ("verdict", verdict_kappa)):
            if kappa is None:
                reasons.append(f"{dimension} kappa undefined (expected agreement is 1)")
            elif kappa < config.KAPPA_THRESHOLD:
                reasons.append(f"{dimension} kappa {kappa:.2f} below {config.KAPPA_THRESHOLD}")
        agreement = Agreement(calibrated=not reasons, reason="; ".join(reasons), label_kappa=label_kappa,
                              verdict_kappa=verdict_kappa, **counts, **stamp)
        store.write_agreement(agreement)
        return agreement

    def _judged_label(self, name: str) -> str | None:
        try:
            return self.applicability(self.resolve_task(name))
        except JudgeAnswerInvalid:
            return None

    def _judged_verdict(self, entry: CalibrationTrajectory) -> str | None:
        try:
            return parse_verdict(self._critique(entry.row, entry.trajectory))
        except CritiqueRejected:
            return None

    def _critique(self, row: Mapping[str, object], trajectory: str) -> str:
        run_key = row.get("run_key")
        cached = store.cached_critique(run_key) if isinstance(run_key, str) else None
        if cached is not None:
            return cached
        record = stripped_record(row, trajectory, self.resolve_task)
        answer, cost = self._call("critique", critique_prompt(record))
        if parse_verdict(answer) is None:
            raise CritiqueRejected(f"critique of {run_key} lacks a verdict line: {answer[:200]!r}")
        if isinstance(run_key, str):
            store.put_critique(run_key=run_key, critique=answer, cost=cost)
        return answer

    def _estimate(self, kind: Kind) -> float:
        """Mean cached cost of this kind, else the largest judge call cost this run, else the floor."""
        costs = [entry["cost"] for entry in store.entries(kind)
                 if entry.get("model") == config.JUDGE_MODEL and isinstance(entry.get("cost"), (int, float))]
        if costs:
            return mean(costs)
        return self._largest_cost if self._largest_cost is not None else self.cell_estimate_usd

    def _charge(self, cost: float, source: str) -> None:
        self.ledger.charge(cost, source=source)
        self._largest_cost = max(cost, self._largest_cost or 0.0)

    def _call(self, kind: Kind, prompt: str) -> tuple[str, float]:
        global _quota_reason
        if _quota_reason is not None:
            raise JudgeQuota(f"judge calls stopped after a usage-limit rejection: {_quota_reason}")
        estimate = self._estimate(kind)
        try:
            refused = self.ledger.exhausted() or self.ledger.would_cross(estimate)
        except ValueError as error:  # an unreadable budget record: its spend is unknown, so refuse
            raise JudgeSpendCeiling(f"{kind} judge call not started: {error}") from error
        if refused:
            raise JudgeSpendCeiling(f"{kind} judge call estimated at ${estimate:.4f} would cross the "
                                    f"${self.ledger.max_usd} ceiling (spent ${self.ledger.spent:.4f} + "
                                    f"${self.ledger.reserve:.4f} reserved)")
        reply = self.client(prompt)
        failed = reply.quota is not None or reply.error is not None or reply.auth_error is not None
        # A failed call charges only a native cost; a pricing-derived cost is not trusted.
        trusted = reply.cost is not None and (reply.cost_source == "native" or not failed)
        cost, source = (reply.cost, reply.cost_source) if trusted else (estimate, "estimate")
        self._charge(cost, source)
        if reply.auth_error is not None:
            raise auth.ClaudeAuthError(reply.auth_error)
        if reply.quota is not None:
            _quota_reason = reply.quota
            raise JudgeQuota(reply.quota)
        if reply.error is not None:
            raise JudgeCallFailed(reply.error)
        return reply.text, cost
