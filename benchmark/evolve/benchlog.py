#!/usr/bin/env python3
"""The append-only evolve benchmark log: one JSON entry per finished run, and its light and dark SVG charts.

    python3 benchmark/evolve/benchlog.py redraw [--log PATH]
    python3 benchmark/evolve/benchlog.py import RUN_DIR [RUN_DIR ...] [--log PATH]

``entry_from_run_dir`` builds an entry from a run directory's ``log.txt`` and ``rows.jsonl``. A live run and
``import`` use it alike, so an imported entry equals the live one except ``recorded_at``. The builder reads only
values the run logged or stored; a value the run did not reach is ``null``. The log holds Sonnet runs only.
"""

import argparse
import json
import re
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

RUN_PREFIX = "run: "
FRONTIER_PREFIX = "frontier: "
ARMS_PREFIX = "arms: "
ARM_NAMES = ("baseline", "seed", "best")
_SEED = re.compile(r"^run seed: (\S+)$")
_STOP = re.compile(r"^stop: (.+)$")
_SEARCH_END = re.compile(r"^search: ended \((.+)\)$")
_MEANS = re.compile(r"^finish: test-split means (.+)$")
_MEAN_PAIR = re.compile(r"([0-9a-f]+)=(-?\d+(?:\.\d+)?)")
_PAIRED = re.compile(r"paired test delta ([+-]?\d+\.\d+) \(95% CI \[(-?\d+\.\d+), (-?\d+\.\d+)\], (\d+) tasks")
_PASS = re.compile(r"test pass rate delta ([+-]?\d+\.\d+)")
_SPEND = re.compile(r"^spend: (?:(.*) )?total=\$(\d+(?:\.\d+)?) of \$(\d+(?:\.\d+)?)$")
_FINALISTS = re.compile(r"^finish: finalists ([0-9a-f]+) \(dev mean (-?\d+\.\d+)\), ([0-9a-f]+) \(dev mean (-?\d+\.\d+)\)$")
_SPEND_PART = re.compile(r"(\w+)=\$(\d+(?:\.\d+)?)")
_PR = re.compile(r"^finish: draft PR opened from \S+: ")
_URL = re.compile(r"https?://[^\s'\",}]+")
# The first matching prefix names the outcome of a ``finish:`` line; a later finish line wins.
_OUTCOMES = (
    ("finish: draft PR opened from ", "pr_opened"),
    ("finish: no improvement", "no_improvement"),
    ("finish: incomplete", "incomplete"),
    ("finish: refused", "refused"),
    ("finish: run failed", "failed"),
    ("finish: pushed ", "pr_failed"),
)


def run_line(panel: str, model: str, preamble: bool) -> str:
    """The run-log line that records the run's panel, agent model, and preamble flag."""
    return RUN_PREFIX + json.dumps({"model": model, "panel": panel, "preamble": preamble}, sort_keys=True)


def frontier_line(seed_dev_mean: float | None, best_cid: str | None, best_dev_mean: float | None) -> str:
    """The run-log line that records the seed's dev mean and the best accepted non-seed candidate."""
    return FRONTIER_PREFIX + json.dumps({"best_cid": best_cid, "best_dev_mean": best_dev_mean,
                                         "seed_dev_mean": seed_dev_mean}, sort_keys=True)


def arms_line(arms: dict) -> str:
    """The run-log line that records the pass rate and costs of the baseline, seed, and best arms per split."""
    return ARMS_PREFIX + json.dumps(arms, sort_keys=True)


def sonnet_models() -> list[str]:
    """The Claude-runner names in ``run.MODELS`` whose model is a Sonnet: the only evolve agent models."""
    import run  # The harness loads only here, so ``redraw`` runs without it.

    return sorted(name for name, model in run.MODELS.items() if run.RUNNERS[name] == "claude" and "sonnet" in model)


def chart_paths(log: Path) -> tuple[Path, Path]:
    """The light and dark chart paths next to ``log``: ``<stem>.svg`` and ``<stem>-dark.svg``."""
    log = Path(log)
    return log.with_suffix(".svg"), log.with_name(f"{log.stem}-dark.svg")


def _utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _session(lines: list[str]) -> list[str]:
    """The lines of the last session: a resumed run appends to the same log, and each session logs its seed first."""
    starts = [index for index, line in enumerate(lines) if _SEED.match(line)]
    return lines[starts[-1]:] if starts else lines


def _json_after(lines: list[str], prefix: str) -> dict:
    found = [line[len(prefix):] for line in lines if line.startswith(prefix)]
    return json.loads(found[-1]) if found else {}


def _last(lines: list[str], pattern: re.Pattern) -> re.Match | None:
    matches = [match for line in lines if (match := pattern.search(line))]
    return matches[-1] if matches else None


def _unique(rows: list[dict], key: str, mode: str | None = None) -> object:
    """The one non-null value of ``key`` over ``rows`` (of ``mode``); None when the rows hold none or several."""
    values = {row.get(key) for row in rows if mode is None or row.get("mode") == mode} - {None}
    return values.pop() if len(values) == 1 else None


def _outcome(lines: list[str]) -> str | None:
    outcome = None
    for line in lines:
        outcome = next((name for prefix, name in _OUTCOMES if line.startswith(prefix)), outcome)
    return outcome


def _spend(lines: list[str]) -> dict | None:
    match = _last(lines, _SPEND)
    if match is None:
        return None
    parts = {name: float(value) for name, value in _SPEND_PART.findall(match.group(1) or "")}
    return {**parts, "total": float(match.group(2)), "max_usd": float(match.group(3))}


def _frontier(lines: list[str]) -> dict:
    """The seed and best dev means: the ``frontier:`` line, else a ``finish: finalists`` line (winner, then seed).

    A log from before the ``frontier:`` line that has no finalists line records no dev mean.
    """
    if any(line.startswith(FRONTIER_PREFIX) for line in lines):
        return _json_after(lines, FRONTIER_PREFIX)
    finalists = _last(lines, _FINALISTS)
    if finalists is None:
        return {}
    return {"best_cid": finalists.group(1), "best_dev_mean": float(finalists.group(2)),
            "seed_dev_mean": float(finalists.group(4))}


def _stored_arms(lines: list[str], rows: list[dict]) -> dict:
    """The arms of a log from before the ``arms:`` line: the baseline arm per split from the stored rows only.

    The rows do not name the candidate of a tilth row, so the seed and best arms are None. A retried or reused cell
    is in the rows more than once; its last row is the one the run used. The test arms exist only when finish
    compared the finalists.
    """
    from evolve.loop import EvolveError, arm_stats  # The harness loads only here, so ``redraw`` runs without it.

    def baseline(split: str) -> dict | None:
        cells = {(row.get("task"), row.get("repetition")): row for row in rows
                 if row.get("mode") == "baseline" and row.get("panel_split") == split}
        try:
            return arm_stats(cells.values())
        except (EvolveError, KeyError, TypeError):
            return None

    test = dict.fromkeys(ARM_NAMES) | {"baseline": baseline("test")} if _last(lines, _MEANS) else None
    return {"dev": dict.fromkeys(ARM_NAMES) | {"baseline": baseline("dev")}, "test": test}


def entry_from_run_dir(run_dir: Path, *, recorded_at: datetime) -> dict:
    """One log entry from ``run_dir``'s ``log.txt`` and ``rows.jsonl``; ``FileNotFoundError`` without a ``log.txt``.

    Every field comes from a logged line or a stored row. A field the run did not reach is None. A log without a
    ``spend:`` line raises ``ValueError``: that run was refused at preflight or did not reach its end.
    """
    run_dir = Path(run_dir)
    lines = _session((run_dir / "log.txt").read_text().splitlines())
    rows_path = run_dir / "rows.jsonl"
    rows = ([json.loads(line) for line in rows_path.read_text().splitlines() if line.strip()]
            if rows_path.is_file() else [])
    if _last(lines, _SPEND) is None:
        raise ValueError(f"{run_dir / 'log.txt'} has no spend line: the run was refused or did not end")
    header = _json_after(lines, RUN_PREFIX)
    frontier = _frontier(lines)
    seed = _last(lines, _SEED)
    stop = _last(lines, _STOP) or _last(lines, _SEARCH_END)
    means = _last(lines, _MEANS)
    paired = _last(lines, _PAIRED)
    passed = _last(lines, _PASS)
    pr = [line for line in lines if _PR.match(line)]
    url = _URL.search(pr[-1]) if pr else None
    return {
        "schema": SCHEMA,
        "run_id": run_dir.name,
        "recorded_at": _utc(recorded_at),
        "seed_sha": seed.group(1) if seed else None,
        "tilth_version": _unique(rows, "tilth_version", "tilth_forced"),
        "panel": header.get("panel", _unique(rows, "panel_name")),
        "model": header.get("model", _unique(rows, "model_alias")),
        "model_id": _unique(rows, "resolved_model"),
        "preamble": header.get("preamble"),
        "stop_reason": stop.group(1) if stop else None,
        "outcome": _outcome(lines),
        "seed_dev_mean": frontier.get("seed_dev_mean"),
        "best_cid": frontier.get("best_cid"),
        "best_dev_mean": frontier.get("best_dev_mean"),
        "test_means": ({cid: float(value) for cid, value in _MEAN_PAIR.findall(means.group(1))}
                       if means else None),
        "paired_test_delta": ({"point": float(paired.group(1)), "low": float(paired.group(2)),
                               "high": float(paired.group(3)), "tasks": int(paired.group(4))}
                              if paired else None),
        "pass_rate_delta": float(passed.group(1)) if passed else None,
        "arms": (_json_after(lines, ARMS_PREFIX) if any(line.startswith(ARMS_PREFIX) for line in lines)
                 else _stored_arms(lines, rows)),
        "spend": _spend(lines),
        "pr_url": url.group(0) if url else None,
    }


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


def import_runs(run_dirs: list[Path], log: Path) -> list[str]:
    """Append an entry for each run directory, oldest ``log.txt`` first; the imported run ids.

    A directory without a ``log.txt``, one whose log does not parse, a run whose model is not a Sonnet, or a run id
    the log holds already is skipped with one line on stderr. ``recorded_at`` is the ``log.txt`` modification time.
    """
    known = {entry.get("run_id") for entry in load(log)}
    sonnet = sonnet_models()
    found = [Path(run_dir) for run_dir in run_dirs if (Path(run_dir) / "log.txt").is_file()]
    for run_dir in run_dirs:
        if Path(run_dir) not in found:
            print(f"skip {run_dir}: no log.txt", file=sys.stderr)
    imported = []
    for run_dir in sorted(found, key=lambda path: ((path / "log.txt").stat().st_mtime, path.name)):
        if run_dir.name in known:
            print(f"skip {run_dir}: run id {run_dir.name} is in the log", file=sys.stderr)
            continue
        modified = datetime.fromtimestamp((run_dir / "log.txt").stat().st_mtime, tz=timezone.utc)
        try:
            entry = entry_from_run_dir(run_dir, recorded_at=modified)
        except (OSError, ValueError) as error:
            print(f"skip {run_dir}: {error}", file=sys.stderr)
            continue
        if entry["model"] not in sonnet:
            print(f"skip {run_dir}: model {entry['model']} is not a Sonnet model ({', '.join(sonnet)})",
                  file=sys.stderr)
            continue
        append(log, entry)
        known.add(run_dir.name)
        imported.append(run_dir.name)
    return imported


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    redraw_parser = commands.add_parser("redraw", help="draw both charts from the log")
    import_parser = commands.add_parser("import", help="append entries built from existing run directories")
    import_parser.add_argument("run_dirs", type=Path, nargs="+", metavar="RUN_DIR")
    for sub in (redraw_parser, import_parser):
        sub.add_argument("--log", type=Path, default=None, help=f"the log file (default {COMMITTED_LOG})")
    args = parser.parse_args(argv)
    log = args.log or DEFAULT_LOG
    if args.command == "import":
        for run_id in import_runs(args.run_dirs, log):
            print(f"imported {run_id}")
    for path in redraw(log):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
