"""Evaluator CLI for benchmark graduation gates.

Consumes a frozen manifest (see benchmark/graduation/schema.py), a
benchmark results JSONL, and a directory of telemetry JSONL files, and
prints a verdict: `pass` only when every threshold and floor in the
manifest is present, numeric, and met. Any missing, `[BLOCKED]`, or
unmet gate forces `blocked`.

Usage:
    python3 -m benchmark.graduation.evaluate --manifest <path> \
        [--results <jsonl>] [--telemetry <dir>]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .schema import BLOCKED, Manifest, Verdict, load_manifest


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def _read_telemetry_dir(path: Path) -> list[dict]:
    rows = []
    for jsonl_path in sorted(Path(path).glob("*.jsonl")):
        rows.extend(_read_jsonl(jsonl_path))
    return rows


def _compute_thresholds(results: list[dict]) -> dict[str, float]:
    if not results:
        return {}
    correct = [1.0 if row.get("correct") else 0.0 for row in results]
    accuracy = sum(correct) / len(correct)
    metrics = {"accuracy": accuracy}
    total_cost = sum(float(row.get("total_cost_usd") or 0.0) for row in results)
    num_correct = sum(correct)
    if num_correct > 0:
        metrics["cost_per_correct"] = total_cost / num_correct
    return metrics


def _compute_floors(telemetry: list[dict]) -> dict[str, float]:
    by_harness: dict[str, list[bool]] = {}
    for row in telemetry:
        harness = row.get("harness")
        if not isinstance(harness, str):
            continue
        by_harness.setdefault(harness, []).append(bool(row.get("passed")))
    return {
        harness: sum(1.0 for p in passes if p) / len(passes)
        for harness, passes in by_harness.items()
    }


def evaluate(
    manifest: Manifest,
    *,
    results: list[dict] | None = None,
    telemetry: list[dict] | None = None,
) -> Verdict:
    """Evaluate a graduation manifest against measured results/telemetry.

    Reports `pass` only when every threshold and floor in the manifest
    is present, numeric, and met by the measured value. Any gate that
    is missing, marked `[BLOCKED]`, or unmet forces `blocked`.
    """
    reasons: list[str] = []
    computed_thresholds = _compute_thresholds(results or [])
    computed_floors = _compute_floors(telemetry or [])

    if not manifest.thresholds and not manifest.floors:
        reasons.append("manifest has no thresholds or floors")

    for name, required in manifest.thresholds.items():
        if required == BLOCKED:
            reasons.append(f"threshold {name!r} is {BLOCKED}")
            continue
        actual = computed_thresholds.get(name)
        if actual is None:
            reasons.append(f"threshold {name!r} has no measured value")
            continue
        if name == "cost_per_correct":
            if actual > required:
                reasons.append(f"threshold {name!r} unmet: {actual} > {required}")
        elif actual < required:
            reasons.append(f"threshold {name!r} unmet: {actual} < {required}")

    for name, required in manifest.floors.items():
        if required == BLOCKED:
            reasons.append(f"floor {name!r} is {BLOCKED}")
            continue
        actual = computed_floors.get(name)
        if actual is None:
            reasons.append(f"floor {name!r} has no measured value")
            continue
        if actual < required:
            reasons.append(f"floor {name!r} unmet: {actual} < {required}")

    if reasons:
        return Verdict(status="blocked", reasons=tuple(reasons))
    return Verdict(status="pass")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate a benchmark graduation manifest.")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--results", type=Path, default=None)
    parser.add_argument("--telemetry", type=Path, default=None)
    args = parser.parse_args(argv)

    manifest = load_manifest(args.manifest)
    results = _read_jsonl(args.results) if args.results else []
    telemetry = _read_telemetry_dir(args.telemetry) if args.telemetry else []

    verdict = evaluate(manifest, results=results, telemetry=telemetry)
    print(verdict)
    return 0 if verdict.passed else 1


if __name__ == "__main__":
    sys.exit(main())
