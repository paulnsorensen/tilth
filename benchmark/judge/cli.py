#!/usr/bin/env python3
"""Judge CLI: label tasks for structural-tool applicability, or calibrate against the hand labels.

    python3 benchmark/judge/cli.py label --tasks NAMES --max-usd FLOAT --cell-estimate-usd FLOAT
    python3 benchmark/judge/cli.py calibrate --max-usd FLOAT --cell-estimate-usd FLOAT
    python3 benchmark/judge/cli.py sheet [--count N] [--panel PATH] [--out DIR]

Both spend flags are required whenever a requested judge call is uncached.
`sheet` makes no model call: it writes a hand-labeling sheet and a calibration draft.
"""

import argparse
import collections
import json
import math
import sys
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent
if not __package__:
    # Run as a script: resolve bare harness imports such as ``config`` against
    # benchmark/, not against this package's own modules.
    sys.path[:] = [entry for entry in sys.path if Path(entry or ".").resolve() != _PACKAGE_DIR]
    sys.path.insert(0, str(_PACKAGE_DIR.parent))

import auth  # noqa: E402
import baselines  # noqa: E402
import panels  # noqa: E402
import run  # noqa: E402
from judge import core  # noqa: E402
from spend import SpendLedger  # noqa: E402

_SPEND_FLAGS = (("--max-usd", "max_usd"), ("--cell-estimate-usd", "cell_estimate_usd"))


def _judge(args: argparse.Namespace, client: core.JudgeClient | None, pending: list[str],
           auth_spawn: Callable | None = None) -> core.Judge | None:
    """A judge on a per-process or named ledger, or None when an uncached call lacks a spend flag."""
    missing = [flag for flag, attribute in _SPEND_FLAGS if getattr(args, attribute) is None]
    if pending and missing:
        print(f"error: {' and '.join(missing)} required: {len(pending)} judge call(s) are uncached "
              f"({', '.join(pending[:5])}{', ...' if len(pending) > 5 else ''})", file=sys.stderr)
        return None
    try:
        if pending:
            auth.preflight_claude_auth([run.isolated_call_target()], spawn=auth_spawn)
        ledger = SpendLedger.persistent(args.budget_id, args.max_usd) if args.budget_id is not None and pending \
            else SpendLedger(args.max_usd)
    except ValueError as error:
        raise core.JudgeError(str(error)) from error
    return core.Judge(ledger, client, cell_estimate_usd=args.cell_estimate_usd or 0.0)


def _label(args: argparse.Namespace, client: core.JudgeClient | None, auth_spawn: Callable | None) -> int:
    names = [name.strip() for name in args.tasks.split(",") if name.strip()]
    tasks = {name: core.default_resolve_task(name) for name in names}
    unknown = [name for name, task in tasks.items() if task is None]
    if unknown:
        print(f"error: unknown task(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    judge = _judge(args, client, core.uncached_labels(tasks), auth_spawn)
    if judge is None:
        return 2
    for name, task in tasks.items():
        print(f"{name}\t{judge.applicability(task)}")
    return 0


def _calibrate(args: argparse.Namespace, client: core.JudgeClient | None, auth_spawn: Callable | None) -> int:
    labels = core.load_calibration()
    pending = core.pending_calls(labels)
    judge = _judge(args, client, pending, auth_spawn)
    if judge is None:
        return 2
    print(json.dumps(asdict(judge.calibrate(labels)), indent=2))
    return 0


_REPO_ROOT = _PACKAGE_DIR.parents[1]
_SHEET_CALLS = 40


def _summarize(value: object, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + " ..."


def _pick_rollouts(count: int) -> tuple[int, list[dict]]:
    """Rows ``core.load_calibration`` accepts (task resolves, trajectory file exists, contaminated is False).

    Picked round-robin over sorted (task, mode) cells.
    """
    stored = baselines.completed_by_key(baselines.load_rows(core.config.RESULT_STORE))
    eligible = [row for row in stored.values() if row.get("contaminated") is False
                and core.default_resolve_task(row.get("task")) is not None
                and row.get("trajectory_path") and Path(row["trajectory_path"]).is_file()]
    by_cell: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    for row in sorted(eligible, key=lambda item: item["run_key"]):
        by_cell[(row["task"], row["mode"])].append(row)
    picked: list[dict] = []
    cells = sorted(by_cell)
    while len(picked) < count and any(by_cell.values()):
        for cell in cells:
            if by_cell[cell] and len(picked) < count:
                picked.append(by_cell[cell].pop(0))
    return len(eligible), picked


def _sheet(args: argparse.Namespace) -> int:
    draft_path = args.out / "calibration-draft.json"
    if draft_path.exists() and not args.force:
        print(f"error: {draft_path} exists and may hold hand labels; pass --force to overwrite", file=sys.stderr)
        return 1
    names = panels.member_ids(args.panel)
    prompts = {name: core.default_resolve_task(name) for name in names}
    unresolved = [name for name, task in prompts.items() if task is None]
    if unresolved:
        print(f"error: panel task(s) do not resolve (external instances need a cached download): "
              f"{', '.join(unresolved)}", file=sys.stderr)
        return 1
    eligible, picked = _pick_rollouts(args.count)
    lines = ["# Judge calibration labeling sheet", "",
             f"{eligible} eligible rollouts in the store; {len(picked)} picked (round-robin by task and arm).",
             "Fill `calibration-draft.json`, then copy it to `benchmark/judge/calibration.json`.", "",
             "## Part A: task labels (structural-tool applicability)", "",
             "`strong`: structural tools (outlines, symbol search, callers, tagged edits) clearly help. "
             "`weak`: they help a little. `none`: plain reads and grep are as good.", ""]
    for name in names:
        lines += [f"### {name}", "", f"> {_summarize(getattr(prompts[name], 'prompt', ''), 600)}", "", "label: ____", ""]
    lines += ["## Part B: rollout verdicts", "",
              "`apt`: structural tools where they help, plain tools where they do not. "
              "`missed`: structural tools would have saved real work and were not used. "
              "`misapplied`: structural tools used where they cost more than they gave, or used wrongly.", ""]
    for index, row in enumerate(picked, 1):
        calls = [json.loads(line) for line in Path(row["trajectory_path"]).read_text().splitlines() if line.strip()]
        lines += [f"### {index}. {row['task']} / {row['mode']} / correct={row.get('correct')}", "",
                  f"run_key: `{row['run_key']}`  cost: ${row.get('total_cost_usd') or 0:.2f}  calls: {len(calls)}", ""]
        lines += [f"- `{call.get('name')}` {_summarize(call.get('input'))}" for call in calls[:_SHEET_CALLS]]
        if len(calls) > _SHEET_CALLS:
            lines.append(f"- ... {len(calls) - _SHEET_CALLS} more calls in `{row['trajectory_path']}`")
        lines += ["", "verdict: ____", ""]
    draft = {"tasks": {name: "" for name in names},
             "trajectories": [{"run_key": row["run_key"], "verdict": ""} for row in picked]}
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "labeling-sheet.md").write_text("\n".join(lines))
    draft_path.write_text(json.dumps(draft, indent=2) + "\n")
    print(f"eligible={eligible} picked={len(picked)} tasks={len(names)} out={args.out}")
    return 0


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number <= 0:
        raise argparse.ArgumentTypeError(f"{value} is not a positive integer")
    return number


def _positive(value: str) -> float:
    number = float(value)
    if not (math.isfinite(number) and number > 0):
        raise argparse.ArgumentTypeError(f"{value} is not a positive amount")
    return number


def main(argv: list[str] | None = None, *, client: core.JudgeClient | None = None,
         auth_spawn: Callable | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    label = commands.add_parser("label", help="label tasks strong, weak, or none")
    label.add_argument("--tasks", required=True, help="comma-separated task names or external instance IDs")
    calibrate = commands.add_parser("calibrate", help="measure agreement with benchmark/judge/calibration.json")
    sheet = commands.add_parser("sheet", help="write a hand-labeling sheet and calibration draft; no model call")
    sheet.add_argument("--count", type=_positive_int, default=24, help="rollouts to pick (default 24)")
    sheet.add_argument("--force", action="store_true", help="overwrite an existing calibration-draft.json")
    sheet.add_argument("--panel", type=Path, default=_REPO_ROOT / "benchmark/panels/gepa-v1.json")
    sheet.add_argument("--out", type=Path, default=_REPO_ROOT / ".cheese/judge", help="output directory")
    for command in (label, calibrate):
        command.add_argument("--max-usd", type=_positive, help="run spend ceiling shared by every judge call")
        command.add_argument("--budget-id", help="share one persistent spend file across runs under this name")
        command.add_argument("--cell-estimate-usd", type=_positive,
                             help="estimate for a judge call when no cached or earlier call cost exists")
    args = parser.parse_args(argv)
    try:
        if args.command == "sheet":
            return _sheet(args)
        run_command = _label if args.command == "label" else _calibrate
        return run_command(args, client, auth_spawn)
    except (panels.PanelError, core.JudgeError, core.CalibrationInvalid, auth.ClaudeAuthError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
