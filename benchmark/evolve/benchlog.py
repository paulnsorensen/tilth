#!/usr/bin/env python3
"""The append-only evolve benchmark log: one JSON entry per finished run, and its light and dark SVG charts.

    python3 benchmark/evolve/benchlog.py redraw [--log PATH]

A run builds its entry from its own state (``loop.Evolution.benchmark_entry``) and writes it to
``<run_dir>/benchmark-entry.json``. A value the run did not reach is ``null``. The log holds Sonnet runs only.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

if not __package__:
    # Run as a script: import ``evolve`` from benchmark/ without the harness modules.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evolve import chart  # noqa: E402

SCHEMA = 2
# The committed log; the README embeds the two charts next to it.
COMMITTED_LOG = Path(__file__).resolve().parent.parent / "log" / "evolve.jsonl"
# The log a run appends to when --benchmark-log is absent; the test suite points it at a temp path.
DEFAULT_LOG = COMMITTED_LOG
# The file in a run directory that holds the run's entry.
ENTRY_FILE = "benchmark-entry.json"


def sonnet_models() -> list[str]:
    """The Claude-runner names in ``run.MODELS`` whose model is a Sonnet: the only evolve agent models."""
    import run  # The harness loads only here, so ``redraw`` runs without it.

    return sorted(name for name, model in run.MODELS.items() if run.RUNNERS[name] == "claude" and "sonnet" in model)


def chart_paths(log: Path) -> tuple[Path, Path]:
    """The light and dark chart paths next to ``log``: ``<stem>.svg`` and ``<stem>-dark.svg``."""
    log = Path(log)
    return log.with_suffix(".svg"), log.with_name(f"{log.stem}-dark.svg")


def utc(moment: datetime) -> str:
    """``moment`` as the entry's ``recorded_at`` text: UTC, to the second, with a ``Z``."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_entry(run_dir: Path, entry: dict) -> Path:
    """Write ``entry`` to ``run_dir``'s entry file and return its path; a resumed run replaces it."""
    path = Path(run_dir) / ENTRY_FILE
    path.write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n")
    return path


def load(log: Path) -> list[dict]:
    """Every entry in ``log``; an absent log has none. A malformed line raises ``ValueError`` with its number."""
    log = Path(log)
    if not log.is_file():
        return []
    entries = []
    for number, line in enumerate(log.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise ValueError(f"{log}:{number}: {error}") from None
    return entries


def append(log: Path, entry: dict) -> None:
    """Append ``entry`` to ``log`` as one line; earlier lines stay as they are."""
    log = Path(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


def redraw(log: Path) -> tuple[Path, Path]:
    """Draw both charts from the whole of ``log`` and write them next to it; an absent log becomes an empty file."""
    log = Path(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.touch()
    entries = load(log)
    light, dark = chart_paths(log)
    light.write_text(chart.render(entries, "light"))
    dark.write_text(chart.render(entries, "dark"))
    return light, dark


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    redraw_parser = commands.add_parser("redraw", help="draw both charts from the log")
    redraw_parser.add_argument("--log", type=Path, default=None, help=f"the log file (default {COMMITTED_LOG})")
    args = parser.parse_args(argv)
    for path in redraw(args.log or DEFAULT_LOG):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
