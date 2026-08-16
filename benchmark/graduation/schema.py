"""Frozen-manifest schema for benchmark graduation gates.

A manifest.json has two top-level keys:

    {
      "thresholds": {"<metric_name>": <number | "[BLOCKED]">, ...},
      "floors": {"<harness_name>": <number | "[BLOCKED]">, ...}
    }

- "thresholds" gate aggregate metrics computed from a benchmark results
  JSONL (see benchmark/run.py). Supported metric names: "accuracy"
  (mean of the `correct` field) and "cost_per_correct" (total
  `total_cost_usd` divided by the number of correct rows).
- "floors" gate per-harness pass rates computed from telemetry JSONL
  files (one row per harness run: {"harness": str, "passed": bool}).

A floor or threshold value of the literal string "[BLOCKED]" marks that
gate as not yet graduated: the evaluator must report `blocked`, never
`pass`, whenever any gate is `[BLOCKED]` or absent from the manifest.
See manifest.example.json for a fully-populated sample.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Union

BLOCKED = "[BLOCKED]"

GateValue = Union[int, float, str]  # numeric floor, or the BLOCKED marker

_MANIFEST_KEYS = {"thresholds", "floors"}


@dataclass(frozen=True)
class Manifest:
    thresholds: dict[str, GateValue]
    floors: dict[str, GateValue]


def _validate_gates(data: dict, context: str) -> dict[str, GateValue]:
    if not isinstance(data, dict):
        raise ValueError(f"{context} must be a JSON object")
    for name, value in data.items():
        if not isinstance(name, str) or not name:
            raise ValueError(f"{context} has an invalid gate name: {name!r}")
        if value == BLOCKED:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{context}[{name!r}] must be a number or {BLOCKED!r}")
    return data


def load_manifest(path: Path) -> Manifest:
    """Load and validate a frozen graduation manifest."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read manifest {path}: {error}") from error
    if not isinstance(data, dict):
        raise ValueError("manifest must be a JSON object")
    unknown = sorted(set(data) - _MANIFEST_KEYS)
    if unknown:
        raise ValueError(f"manifest has unknown keys: {', '.join(unknown)}")
    thresholds = _validate_gates(data.get("thresholds", {}), "thresholds")
    floors = _validate_gates(data.get("floors", {}), "floors")
    return Manifest(thresholds=thresholds, floors=floors)


@dataclass(frozen=True)
class Verdict:
    status: str  # "pass" or "blocked"
    reasons: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.status == "pass"

    def __str__(self) -> str:
        if self.passed:
            return "pass"
        return "blocked: " + "; ".join(self.reasons)
