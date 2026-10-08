#!/usr/bin/env python3
"""Judge CLI: label tasks for structural-tool applicability, or calibrate against the hand labels.

    python3 benchmark/judge/cli.py label --tasks NAMES --max-usd FLOAT --cell-estimate-usd FLOAT
    python3 benchmark/judge/cli.py calibrate --max-usd FLOAT --cell-estimate-usd FLOAT

Both spend flags are required whenever a requested judge call is uncached.
"""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent
if not __package__:
    # Run as a script: resolve bare harness imports such as ``config`` against
    # benchmark/, not against this package's own modules.
    sys.path[:] = [entry for entry in sys.path if Path(entry or ".").resolve() != _PACKAGE_DIR]
    sys.path.insert(0, str(_PACKAGE_DIR.parent))

import run  # noqa: E402
from judge import core, store  # noqa: E402
from spend import SpendLedger  # noqa: E402

_SPEND_FLAGS = (("--max-usd", "max_usd"), ("--cell-estimate-usd", "cell_estimate_usd"))


def _judge(args: argparse.Namespace, client: core.JudgeClient | None, pending: list[str]) -> core.Judge | None:
    """A judge on a fresh ledger, or None when an uncached call lacks a spend flag."""
    missing = [flag for flag, attribute in _SPEND_FLAGS if getattr(args, attribute) is None]
    if pending and missing:
        print(f"error: {' and '.join(missing)} required: {len(pending)} judge call(s) are uncached "
              f"({', '.join(pending[:5])}{', ...' if len(pending) > 5 else ''})", file=sys.stderr)
        return None
    return core.Judge(SpendLedger(args.max_usd), client, cell_estimate_usd=args.cell_estimate_usd or 0.0)


def _label(args: argparse.Namespace, client: core.JudgeClient | None) -> int:
    names = [name.strip() for name in args.tasks.split(",") if name.strip()]
    tasks = {name: core.default_resolve_task(name) for name in names}
    unknown = [name for name, task in tasks.items() if task is None]
    if unknown:
        print(f"error: unknown task(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    pending = [name for name, task in tasks.items() if store.cached_label(run._cell_task_digest(task)) is None]
    judge = _judge(args, client, pending)
    if judge is None:
        return 2
    for name, task in tasks.items():
        print(f"{name}\t{judge.applicability(task)}")
    return 0


def _calibrate(args: argparse.Namespace, client: core.JudgeClient | None) -> int:
    labels = core.load_calibration()
    pending = core.pending_calls(labels)
    judge = _judge(args, client, pending)
    if judge is None:
        return 2
    print(json.dumps(asdict(judge.calibrate(labels)), indent=2))
    return 0


def _positive(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError(f"{value} is not a positive amount")
    return number


def main(argv: list[str] | None = None, *, client: core.JudgeClient | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    label = commands.add_parser("label", help="label tasks strong, weak, or none")
    label.add_argument("--tasks", required=True, help="comma-separated task names or external instance IDs")
    calibrate = commands.add_parser("calibrate", help="measure agreement with benchmark/judge/calibration.json")
    for command in (label, calibrate):
        command.add_argument("--max-usd", type=_positive, help="run spend ceiling shared by every judge call")
        command.add_argument("--cell-estimate-usd", type=_positive,
                             help="estimate for a judge call when no cached or earlier call cost exists")
    args = parser.parse_args(argv)
    try:
        return _label(args, client) if args.command == "label" else _calibrate(args, client)
    except (core.JudgeError, core.CalibrationInvalid, run.ClaudeAuthError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
