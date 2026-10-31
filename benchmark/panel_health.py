#!/usr/bin/env python3
"""Per-task health table for a benchmark panel, built from stored result rows.

A chain row is timed by its slowest turn (the API ``duration_ms`` of its sub-task), and its budget is the
member budget times the chain size.

The table shows, for each task, the pass rate per arm, the failure-reason classes,
the wall-time percentiles against the cell timeout, the cost against the cell
budget, and the budget-cap and timeout hits. It flags a task that fails in both
arms with the same failure class, and a task whose slowest cell or costliest cell
leaves too little headroom for a loaded host.

Only paid rows count: a row with ``reused: true`` is a copy of a stored cell. A row
that two files share (same ``run_key`` and ``trajectory_path``) counts once.

    python3 benchmark/panel_health.py results/evolve/*/rows.jsonl --panel benchmark/panels/gepa-v3-long-2.json
"""

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

BASELINE = "baseline"
# A failure class is "shared" when at least this fraction of each arm's cells fail with it.
SHARED_FAIL_RATE = 0.25
# A cell that uses more than this fraction of its timeout or budget leaves too little headroom.
HEADROOM_LIMIT = 0.5
BUDGET_LIMIT = 0.8


@dataclass
class ArmStats:
    cells: int = 0
    passed: int = 0
    failures: Counter = field(default_factory=Counter)

    @property
    def fail_rate(self) -> float:
        return 1 - self.passed / self.cells if self.cells else 0.0


@dataclass
class TaskHealth:
    task: str
    split: str | None = None
    arms: dict[str, ArmStats] = field(default_factory=lambda: defaultdict(ArmStats))
    wall_s: list[float] = field(default_factory=list)
    timeouts_s: set[int] = field(default_factory=set)
    cost_ratio: list[float] = field(default_factory=list)
    costs: list[float] = field(default_factory=list)
    budgets: set[float] = field(default_factory=set)
    budget_caps: int = 0
    timed_out: int = 0

    def flags(self) -> list[str]:
        flags = []
        if shared := self.shared_failure():
            flags.append(f"both-arms: {shared}")
        if self.timed_out:
            flags.append("timeout")
        if self.wall_s and self.timeouts_s and max(self.wall_s) > HEADROOM_LIMIT * min(self.timeouts_s):
            flags.append("slow")
        if self.budget_caps:
            flags.append("budget-cap")
        elif self.cost_ratio and max(self.cost_ratio) > BUDGET_LIMIT:
            flags.append("near-budget")
        return flags

    def shared_failure(self) -> str | None:
        """The failure class that reaches ``SHARED_FAIL_RATE`` of the cells in every arm, if any."""
        arms = [arm for arm in self.arms.values() if arm.cells]
        if len(arms) < 2:
            return None
        common = set.intersection(*(set(arm.failures) for arm in arms))
        shared = [name for name in common if all(arm.failures[name] / arm.cells >= SHARED_FAIL_RATE for arm in arms)]
        return max(shared, key=lambda name: sum(arm.failures[name] for arm in arms)) if shared else None


def failure_class(row: Mapping) -> str:
    """A short class for why one row failed; chain rows name each wrong sub-task."""
    error = str(row.get("error") or "")
    reason = str(row.get("correctness_reason") or "")
    if "error_max_budget_usd" in error or "error_max_budget_usd" in reason:
        return "budget-cap"
    if row.get("timed_out") or error == "timeout" or "TimeoutExpired" in error:
        return "timeout"
    if row.get("sub_tasks"):
        wrong = sorted(sub["task"] for sub in row["sub_tasks"] if not sub.get("correct"))
        return "sub:" + "+".join(wrong) if wrong else "chain"
    if error:
        return "error"
    if row.get("contaminated") is True:
        return "contaminated"
    if row.get("f2p_total"):
        if (row.get("f2p_passed") or 0) < row["f2p_total"]:
            return "f2p"
        if (row.get("p2p_passed") or 0) < (row.get("p2p_total") or 0):
            return "p2p"
    if reason.startswith("Missing:"):
        return "missing:" + reason.removeprefix("Missing:").strip()[:40]
    return "other:" + reason[:40] if reason else "other"


def load_rows(paths: Iterable[Path]) -> list[dict]:
    """Paid rows from JSONL files; a torn line is skipped and a shared row counts once."""
    rows, seen = [], set()
    for path in paths:
        for line in Path(path).read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict) or row.get("reused") is True or "task" not in row:
                continue
            key = (row.get("run_key"), row.get("trajectory_path"), row.get("mode"), row.get("repetition"))
            if key[0] is not None and key in seen:
                continue
            seen.add(key)
            rows.append(row)
    return rows


def _cell_budget(row: Mapping) -> float | None:
    """The cell's ``--max-budget-usd``; a chain may spend the member budget once per member."""
    budget = row.get("max_budget_usd")
    if not isinstance(budget, (int, float)) or budget <= 0:
        return None
    return float(budget) * (row.get("chain_size") or 1)


def _cost(row: Mapping) -> float | None:
    for name in ("charged_usd", "total_cost_usd"):
        value = row.get(name)
        if isinstance(value, (int, float)) and math.isfinite(value):
            return float(value)
    return None


def _wall_s(row: Mapping) -> float | None:
    """Seconds the cell ran; for a chain, its slowest turn, because the timeout applies to each turn."""
    if row.get("sub_tasks"):
        turns = [sub.get("duration_ms") or 0 for sub in row["sub_tasks"]]
        return max(turns) / 1000 if max(turns) > 0 else None
    for name in ("wall_duration_ms", "duration_ms"):
        value = row.get(name)
        if isinstance(value, (int, float)) and value > 0:
            return value / 1000
    return None


def summarize(rows: Iterable[Mapping], splits: Mapping[str, str] | None = None) -> dict[str, TaskHealth]:
    """Fold rows into one ``TaskHealth`` per task; ``splits`` limits the tasks to a panel and names their split."""
    tasks: dict[str, TaskHealth] = {}
    for row in rows:
        name = row["task"]
        if splits is not None and name not in splits:
            continue
        health = tasks.setdefault(name, TaskHealth(name, (splits or {}).get(name) or row.get("panel_split")))
        arm = health.arms[row.get("mode") or "?"]
        arm.cells += 1
        if row.get("correct") is True:
            arm.passed += 1
        else:
            arm.failures[failure_class(row)] += 1
        cls = failure_class(row) if row.get("correct") is not True else None
        health.budget_caps += cls == "budget-cap"
        health.timed_out += cls == "timeout"
        if isinstance(row.get("timeout_s"), int):
            health.timeouts_s.add(row["timeout_s"])
        budget, cost = _cell_budget(row), _cost(row)
        if budget is not None:
            health.budgets.add(budget)
        if cost is not None:
            health.costs.append(cost)
            if budget is not None:
                health.cost_ratio.append(cost / budget)
        if (wall := _wall_s(row)) is not None:
            health.wall_s.append(wall)
    return tasks


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile; ``q`` is in [0, 100]."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q / 100 * len(ordered)) - 1)]


def suggested_timeout(health: TaskHealth, factor: float = 2.0, step: int = 300) -> int | None:
    """``factor`` times the slowest cell, rounded up to ``step`` seconds, never below the current timeout."""
    if not health.wall_s:
        return None
    suggestion = math.ceil(factor * max(health.wall_s) / step) * step
    return max(suggestion, *health.timeouts_s) if health.timeouts_s else suggestion


def _arm_cell(arm: ArmStats | None) -> str:
    if arm is None or not arm.cells:
        return "-"
    return f"{arm.passed}/{arm.cells} ({arm.passed / arm.cells:.0%})"


def _failures(arms: Mapping[str, ArmStats]) -> str:
    parts = []
    for mode in sorted(arms, key=lambda mode: (mode != BASELINE, mode)):
        if arms[mode].failures:
            listed = ", ".join(f"{name} x{count}" for name, count in arms[mode].failures.most_common())
            parts.append(f"{'b' if mode == BASELINE else mode[0]}: {listed}")
    return "; ".join(parts) or "-"


def render(tasks: Mapping[str, TaskHealth], order: Sequence[str] | None = None) -> str:
    """A markdown table, one row per task, in ``order`` when given."""
    names = [name for name in order if name in tasks] if order else sorted(tasks)
    modes = sorted({mode for health in tasks.values() for mode in health.arms}, key=lambda m: (m != BASELINE, m))
    header = ["task", "split", *(f"{mode} pass" for mode in modes), "failure classes",
              "wall p50/p90/max s", "timeout s", "max/timeout", "suggest s",
              "cost p90/max $", "budget $", "max/budget", "cap hits", "timeouts", "flags"]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for name in names:
        health = tasks[name]
        wall = health.wall_s
        timeout = min(health.timeouts_s) if health.timeouts_s else None
        cells = [
            name, health.split or "-", *(_arm_cell(health.arms.get(mode)) for mode in modes), _failures(health.arms),
            f"{percentile(wall, 50):.0f}/{percentile(wall, 90):.0f}/{max(wall):.0f}" if wall else "-",
            "/".join(str(value) for value in sorted(health.timeouts_s)) or "-",
            f"{max(wall) / timeout:.2f}" if wall and timeout else "-",
            str(suggested_timeout(health) or "-"),
            f"{percentile(health.costs, 90):.2f}/{max(health.costs):.2f}" if health.costs else "-",
            "/".join(f"{value:g}" for value in sorted(health.budgets)) or "-",
            f"{max(health.cost_ratio):.2f}" if health.cost_ratio else "-",
            str(health.budget_caps), str(health.timed_out), ", ".join(health.flags()) or "-",
        ]
        lines.append("| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |")
    return "\n".join(lines)


def panel_splits(path: Path) -> dict[str, str]:
    """Member id to split, read from a panel file without the loader's host checks."""
    return {member["id"]: member["split"] for member in json.loads(Path(path).read_text())["members"]}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("rows", nargs="+", type=Path, help="result JSONL files (rows.jsonl or benchmark_*.jsonl)")
    parser.add_argument("--panel", type=Path, help="limit the table to this panel's members, in panel order")
    args = parser.parse_args(argv)
    splits = panel_splits(args.panel) if args.panel else None
    tasks = summarize(load_rows(args.rows), splits)
    print(render(tasks, list(splits) if splits else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
