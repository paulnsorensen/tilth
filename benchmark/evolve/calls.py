"""The shared stop state and the paid ``claude -p`` calls the evolve loop makes itself (reflection, proposer)."""

import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean

import run
from jsonl import tolerant_jsonl
from parse import detect_quota_rejection, stream_native_cost
from spend import SpendLedger

STOP_REASONS = ("ceiling", "quota", "plateau", "cli-version")


@dataclass
class StopState:
    """Why the search stopped, if it has: ``ceiling``, ``quota``, ``plateau``, or ``cli-version``.

    gepa swallows proposer exceptions and retries, so evolve never stops by
    raising: the engine's stop callback reads this state instead. ``quota`` is
    the strictest reason and replaces any other; no other reason replaces one.
    """

    reason: str | None = None
    listeners: list[Callable[[str], None]] = field(default_factory=list)

    @property
    def is_set(self) -> bool:
        return self.reason is not None

    def set(self, reason: str) -> None:
        if reason not in STOP_REASONS:
            raise ValueError(f"unknown stop reason {reason!r}")
        if self.reason is None or (reason == "quota" and self.reason != "quota"):
            self.reason = reason
            for listener in self.listeners:
                listener(reason)


@dataclass(frozen=True)
class CallOutcome:
    text: str
    stream: str


class PaidCalls:
    """Reflection and proposer calls: auth guard, same-kind estimate, run-wide ledger, quota stop.

    A call's estimate is the mean cost of earlier calls of its kind in this run,
    else the ``--cell-estimate-usd`` floor. A call that would cross the ledger's
    ceiling (reserve included) does not start and sets the stop state to
    ``ceiling``; a usage-limit rejection sets it to ``quota``. No call starts
    while the stop state is set.
    """

    def __init__(self, ledger: SpendLedger, stop: StopState, floor: float, *,
                 spawn: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                 log: Callable[[str], None] = print, timeout_s: int = 1800) -> None:
        self.ledger = ledger
        self.stop = stop
        self.floor = floor
        self.spawn = spawn
        self.log = log
        self.timeout_s = timeout_s
        self.costs: dict[str, list[float]] = {}

    def estimate(self, kind: str) -> float:
        costs = self.costs.get(kind)
        return mean(costs) if costs else self.floor

    def total(self, kind: str) -> float:
        return sum(self.costs.get(kind, ()))

    def call(self, kind: str, argv: list[str], prompt: str, cwd: Path) -> CallOutcome | None:
        """Run one isolated ``claude -p`` with ``prompt`` on stdin; None when it did not start or failed."""
        run.guard_claude_auth(os.environ)
        if self.stop.is_set:
            return None
        estimate = self.estimate(kind)
        if self.ledger.would_cross(estimate):
            self.log(f"{kind}: not started; ${self.ledger.spent:.4f} spent + ${estimate:.4f} estimated + "
                     f"${self.ledger.reserve:.4f} reserved exceeds ${self.ledger.max_usd}")
            self.stop.set("ceiling")
            return None
        env = run.build_runner_env("claude", tilth_bin=None)
        with tempfile.TemporaryDirectory(prefix=f"tilth-{kind}-config-") as config_dir:
            env["CLAUDE_CONFIG_DIR"] = config_dir
            try:
                completed = self.spawn(argv, input=prompt, capture_output=True, text=True, cwd=str(cwd), env=env,
                                       timeout=self.timeout_s)
                stdout, returncode = completed.stdout or "", completed.returncode
            except subprocess.TimeoutExpired as error:
                output = error.stdout
                stdout = output.decode(errors="replace") if isinstance(output, bytes) else (output or "")
                returncode = None
        native = stream_native_cost(stdout)
        cost = native if native is not None else estimate
        self.ledger.charge(cost, source=f"{kind}:{'native' if native is not None else 'estimate'}")
        self.costs.setdefault(kind, []).append(cost)
        quota = detect_quota_rejection(stdout)
        if quota:
            self.log(f"infra:quota: {kind} call rejected: {quota}")
            self.stop.set("quota")
            return None
        results = [event for event in tolerant_jsonl(stdout) if event.get("type") == "result"]
        if returncode != 0 or not results or results[-1].get("is_error"):
            self.log(f"{kind}: call failed (exit {returncode}); keeping the parent's component")
            return None
        text = results[-1].get("result")
        return CallOutcome(text=text if isinstance(text, str) else "", stream=stdout)
