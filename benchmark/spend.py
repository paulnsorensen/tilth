"""Run-wide spend ledger shared by every paid call in a benchmark run.

Agent cells, judge calls, and the evolve loop's reflection and proposer calls all
charge the same ledger, so one ``--max-usd`` ceiling bounds the whole run. Callers
check ``would_cross`` with their own estimate before a call and ``charge`` its
cost afterwards. ``reserve`` holds back part of the ceiling: the evolve loop sets
it during its search so every check, the judge's included, leaves the finish
phase's budget unspent, then sets it to 0 for finish.

A ledger built with ``SpendLedger.persistent`` also keeps its charges in an append-only file under
``$TILTH_BENCH_DATA/budgets/``, so a rerun of a stopped command starts from the spend already recorded.
``hold_run_lock`` lets one live run own a budget, so two concurrent runs never share its spend or output.
"""

import fcntl
import hashlib
import json
import math
import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Literal

from jsonl import tolerant_jsonl
from parse import parse_codex_json, parse_stream_json, stream_native_cost

# Unreserved headroom below this floor counts as exhausted, so float leftovers never become a budget cap.
MIN_HEADROOM_USD = 0.01

_BUDGET_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def plan_budget_id(run_keys: Iterable[str]) -> str:
    """A budget id derived from the sorted set of planned run keys, so one plan keeps one budget."""
    digest = hashlib.sha256("\n".join(sorted(set(run_keys))).encode()).hexdigest()
    return f"plan-{digest[:16]}"


def _budget_root() -> Path:
    from external.data import data_dir  # lazy: the data directory helper lives in the external package

    return data_dir() / "budgets"


def _check_budget_id(budget_id: str) -> None:
    if not _BUDGET_ID.fullmatch(budget_id) or ".." in budget_id:
        raise ValueError(f"budget id {budget_id!r} must be a plain file name (letters, digits, '.', '_', '-')")


class BudgetBusyError(RuntimeError):
    """Another live run holds the run lock of this budget."""


def hold_run_lock(budget_id: str, *, root: Path | None = None) -> IO[str]:
    """Take the run lock of ``budget_id`` without waiting and return its open file; closing the file releases it.

    The lock lives under the budget root, so runs from every worktree that share the data directory see it.
    The kernel releases it when the holding process exits, so a crashed run never leaves a stale lock.
    """
    _check_budget_id(budget_id)
    directory = (Path(root) if root is not None else _budget_root()) / "runs"
    directory.mkdir(parents=True, exist_ok=True)
    lock = open(directory / f"{budget_id}.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise BudgetBusyError(
            f"budget {budget_id} is in use by another run; wait for it to finish or pass a different --budget-id"
        ) from None
    return lock


@contextmanager
def _locked(path: Path, *, exclusive: bool) -> Iterator[None]:
    """Hold an ``fcntl`` lock on a sidecar lock file, so readers never see a half-written record."""
    with open(path.with_suffix(".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _parse_line(line: str) -> dict | None:
    """The record on a budget line, or None when the line is torn or not an object."""
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


def _record_charge(record: dict) -> float | None:
    """A record's charge, or None when its amount is unreadable.

    An object with no ``charged_usd`` is not a charge, and ``charge`` never writes a negative amount, so both
    count 0.0: skipping them cannot undercount. A non-numeric or non-finite amount is unknown.
    """
    if "charged_usd" not in record:
        return 0.0
    value = record["charged_usd"]
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return None
    return max(float(value), 0.0)


def _recorded_spend(path: Path) -> float:
    """The sum of the charges after the newest reset marker; the caller holds the lock.

    A line after the newest marker with no readable amount, such as a record torn by a crash, raises
    ``ValueError``: its amount is unknown, and skipping it would undercount the budget.
    """
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return 0.0
    total = 0.0
    unreadable: list[int] = []
    for number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        record = _parse_line(line)
        if record is not None and record.get("reset") is True:
            total, unreadable = 0.0, []
            continue
        charge = None if record is None else _record_charge(record)
        if charge is None:
            unreadable.append(number)
        else:
            total += charge
    if unreadable:
        raise ValueError(f"budget file {path} has no readable charge on line(s) {unreadable}; fix the file, "
                         "pass --fresh-budget to run.py to start the budget over, or use a new --budget-id")
    return total


def _append(path: Path, record: dict) -> None:
    with open(path, "a+b") as out:
        out.seek(0, 2)
        torn_tail = out.tell() > 0 and (out.seek(-1, 2), out.read(1))[1] != b"\n"
        # A torn tail gets its own line, so this record stays whole.
        out.write(("\n" if torn_tail else "").encode() + (json.dumps(record) + "\n").encode())
        out.flush()


@dataclass
class SpendLedger:
    """Cumulative spend against a ceiling; ``max_usd=None`` means unbounded."""

    max_usd: float | None
    spent: float = 0.0
    charges: list[tuple[float, str]] = field(default_factory=list)
    reserve: float = 0.0
    budget_id: str | None = None
    budget_path: Path | None = None

    @classmethod
    def persistent(cls, budget_id: str, max_usd: float | None, *, fresh: bool = False,
                   root: Path | None = None) -> "SpendLedger":
        """A ledger seeded from, and appending to, the budget file of ``budget_id``.

        ``fresh`` appends a reset marker first, so only later charges count.
        """
        _check_budget_id(budget_id)
        directory = Path(root) if root is not None else _budget_root()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{budget_id}.jsonl"
        if fresh:
            with _locked(path, exclusive=True):
                _append(path, {"reset": True, "at": datetime.now(timezone.utc).isoformat()})
        ledger = cls(max_usd=max_usd, budget_id=budget_id, budget_path=path)
        ledger._refresh()
        return ledger

    def _refresh(self) -> None:
        """Re-read the recorded spend under the lock, so a concurrent run's charges count."""
        if self.budget_path is not None:
            with _locked(self.budget_path, exclusive=False):
                self.spent = _recorded_spend(self.budget_path)

    def would_cross(self, estimate: float) -> bool:
        self._refresh()
        ceiling = math.inf if self.max_usd is None else self.max_usd
        return self.spent + estimate + self.reserve > ceiling

    def headroom(self) -> float | None:
        """Unreserved spend left under the ceiling; ``None`` when unbounded."""
        self._refresh()
        return None if self.max_usd is None else self.max_usd - self.spent - self.reserve

    def exhausted(self) -> bool:
        """True when the unreserved headroom is below ``MIN_HEADROOM_USD``."""
        headroom = self.headroom()
        return headroom is not None and headroom < MIN_HEADROOM_USD

    def charge(self, cost: float, *, source: str, run_key: str | None = None) -> None:
        if not math.isfinite(cost) or cost < 0:
            raise ValueError(f"invalid charge {cost} from {source}")
        if self.budget_path is not None:
            record = {"run_key": run_key, "charged_usd": cost, "cost_source": source,
                      "at": datetime.now(timezone.utc).isoformat()}
            with _locked(self.budget_path, exclusive=True):
                _append(self.budget_path, record)
                try:
                    self.spent = _recorded_spend(self.budget_path)
                except ValueError:
                    # The record is paid and stored. The unreadable line stays unknown (never 0.0): the next
                    # pre-cell check re-reads the file and raises, so the caller can store the row first.
                    self.spent += cost
        else:
            self.spent += cost
        self.charges.append((cost, source))


def settle_failed(stdout: str, runner: Literal["claude", "codex"], estimate: float,
                  model: str | None = None) -> tuple[float, str]:
    """The cost of a failed call: the native cost, else the priced stream, else the estimate.

    The second value names the source: ``native``, ``pricing``, or ``estimate``. A killed runner can leave a
    torn last line, which is dropped before pricing.
    """
    if runner == "claude":
        native = stream_native_cost(stdout)
        if native is not None:
            return native, "native"
    events = list(tolerant_jsonl(stdout))
    if runner == "codex":
        # A killed or failed turn leaves trailing events the strict parser rejects; price the completed turns.
        done = [i for i, event in enumerate(events) if isinstance(event, dict) and event.get("type") == "turn.completed"]
        events = events[:done[-1] + 1] if done else []
    clean = "\n".join(json.dumps(event) for event in events)
    try:
        priced = (parse_stream_json(clean, model) if runner == "claude"
                  else parse_codex_json(clean, model or "")).total_cost_usd
    except (ValueError, TypeError, AttributeError, KeyError):
        priced = 0.0
    return (priced, "pricing") if priced else (estimate, "estimate")
