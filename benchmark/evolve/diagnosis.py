"""Diagnosis wiring for the evolve loop: what reflection and the proposer see about where tokens go.

Three parts. None of them scores or rejects a candidate.

- ``compact`` cuts an ``attribution.summarize`` result to a size that a prompt record can carry.
- ``WeightedSelector`` is gepa's ``ReflectionComponentSelector``. It favours the components that the parent's
  attribution names. It also guarantees that ``src_patch`` and every other component gets a turn.
- ``HypothesisBook`` stores the soft predictions that reflection and the proposer attach to a change. It keys
  them by parent and child content id outside the candidate, and settles them against attribution.
"""

import json
import random
import re
from collections.abc import Callable
from pathlib import Path

from .candidate import SRC_PATCH, short_id

ATTRIBUTION_CAP = 4000
DOMINANT_SHOWN = 3
EVIDENCE_CAP = 120
FEEDBACK_SHOWN = 5
SRC_PATCH_BY_PROPOSAL = 2
MAX_UNSELECTED = 10
HINT_WEIGHT = 4.0
DIRECTIONS = ("decrease", "increase")
TOOL_METRICS = ("result_chars_per_cell", "calls_per_cell", "error_rate")
METRICS = (*TOOL_METRICS, "excess_chars_per_cell", "exposure")
EXPOSURE_KEY = "result_chars_times_remaining_api_turns_per_cell"

DIAGNOSIS_NOTE = (
    "An `attribution` record and a `source_map` record, when present, name where the extra tokens come from "
    "and which source renders them. A `deep_diagnosis` record, when present, lists ranked causes from an "
    "independent turn-by-turn comparison of rollouts with and without tilth."
)
DIRECTIVE_NOTE = (
    "A `directive` record, when present, names the change to make: implement exactly that change."
)
HYPOTHESIS_BODY = (
    "add one <hypothesis>{json}</hypothesis> block that predicts the effect of your change. "
    f"Its fields are metric (one of {', '.join(METRICS)}), scope (a tool name such as tilth_write, "
    "a cause name for excess_chars_per_cell, or all), direction (decrease or increase), "
    "tolerance (the smallest relative change that counts, such as 0.1), and invariants (a list of strings). "
    "The hypothesis is a prediction only: nothing rejects a candidate because of it. "
    "A `hypothesis_feedback` record, when present, shows how earlier predictions turned out."
)
REFLECTION_HYPOTHESIS = "After </new_text>, " + HYPOTHESIS_BODY
PROPOSER_HYPOTHESIS = "After your edits, " + HYPOTHESIS_BODY

_BLOCK = re.compile(r"<hypothesis>(.*?)</hypothesis>", re.DOTALL)


def _size(value) -> int:
    return len(json.dumps(value, sort_keys=True))


def causes(summary: dict) -> list[str]:
    """The leading dominant causes, in rank order."""
    return [d["cause"] for d in summary.get("dominant", ())[:DOMINANT_SHOWN]]


def _shrink(view: dict) -> bool:
    """Drop one item from the largest of errors and call_shapes; False when neither has any."""
    sized = sorted(((_size(view[k]), k) for k in ("errors", "call_shapes") if view.get(k)), reverse=True)
    if not sized:
        return False
    part = view[sized[0][1]]
    if isinstance(part, dict):
        part.pop(next(reversed(part)))
    else:
        part.pop()
    return True


def compact(summary: dict) -> dict:
    """The prompt view of a summary: coverage, leading causes with evidence, call shapes and errors."""
    view = {
        "coverage": summary.get("coverage"),
        "dominant": [dict(d) for d in summary.get("dominant", ())[:DOMINANT_SHOWN]],
        "call_shapes": json.loads(json.dumps(summary.get("call_shapes") or [])),
        "errors": json.loads(json.dumps(summary.get("errors") or [])),
    }
    while _size(view) > ATTRIBUTION_CAP and _shrink(view):
        pass
    while _size(view) > ATTRIBUTION_CAP and len(view["dominant"]) > 1:
        view["dominant"].pop()
    if _size(view) > ATTRIBUTION_CAP:
        for entry in view["dominant"]:
            entry["evidence"] = str(entry["evidence"])[:EVIDENCE_CAP]
    return view


def parse_hypothesis(text: str) -> dict | None:
    """The one hypothesis block in ``text``, or None when there is none; ValueError when it is malformed."""
    match = _BLOCK.search(text)
    if match is None:
        return None
    try:
        raw = json.loads(match.group(1))
    except ValueError as error:
        raise ValueError(f"hypothesis is not JSON: {error}") from error
    if not isinstance(raw, dict):
        raise ValueError("hypothesis is not an object")
    tolerance = raw.get("tolerance")
    invariants = raw.get("invariants")
    if raw.get("metric") not in METRICS:
        raise ValueError(f"hypothesis metric must be one of {', '.join(METRICS)}")
    if not isinstance(raw.get("scope"), str) or not raw["scope"]:
        raise ValueError("hypothesis scope must be a non-empty string")
    if raw.get("direction") not in DIRECTIONS:
        raise ValueError("hypothesis direction must be decrease or increase")
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or tolerance < 0:
        raise ValueError("hypothesis tolerance must be a number of at least 0")
    if not isinstance(invariants, list) or not all(isinstance(i, str) for i in invariants):
        raise ValueError("hypothesis invariants must be a list of strings")
    return {key: raw[key] for key in ("metric", "scope", "direction", "tolerance", "invariants")}


def measure(summary: dict | None, metric: str, scope: str) -> float | None:
    """One candidate-arm figure from a summary; None when the summary cannot give it."""
    if not summary:
        return None
    if metric == "exposure":
        value = summary.get("exposure", {}).get("candidate", {}).get(EXPOSURE_KEY)
        return None if value is None else float(value)
    if metric == "excess_chars_per_cell":
        if "dominant" not in summary:
            return None
        found = [d["excess_chars_per_cell"] for d in summary["dominant"] if scope in ("all", d["cause"])]
        return float(sum(found))
    arm = [e for e in summary.get("per_tool", ()) if e.get("arm") == "candidate"]
    if not arm:
        return None
    rows = [e for e in arm if scope in ("all", e["tool"])]
    if metric == "error_rate":
        calls = sum(e["calls_per_cell"] for e in rows)
        return sum(e["error_rate"] * e["calls_per_cell"] for e in rows) / calls if calls else 0.0
    return float(sum(e[metric] for e in rows))


def judge(hypothesis: dict, parent: dict | None, child: dict | None) -> tuple[str, dict]:
    """Compare a claim with the child-minus-parent change. Returns the status and what was observed."""
    before = measure(parent, hypothesis["metric"], hypothesis["scope"])
    after = measure(child, hypothesis["metric"], hypothesis["scope"])
    observed = {"parent": before, "child": after}
    if before is None or after is None or before == 0:
        return "unmeasurable", observed
    change = (after - before) / abs(before)
    observed["relative_change"] = round(change, 4)
    sign = -1 if hypothesis["direction"] == "decrease" else 1
    return ("met" if sign * change >= hypothesis["tolerance"] else "unmet"), observed


class HypothesisBook:
    """Soft predictions, stored beside the candidate and never part of its content id."""

    def __init__(self, path: Path, log: Callable[[str], None]):
        self.path = path
        self.log = log
        self.pending: dict[str, dict] = {}
        self.awaiting: dict[str, list[dict]] = {}
        self.outcomes: list[dict] = []

    def _write(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    def offer(self, component: str, text: str) -> None:
        """Hold the hypothesis in a model reply until the change it belongs to has a child id."""
        try:
            hypothesis = parse_hypothesis(text)
        except ValueError as error:
            self.log(f"hypothesis: ignored for {component}: {error}")
            return
        if hypothesis is not None:
            self.pending[component] = hypothesis

    def discard(self) -> None:
        self.pending.clear()

    def default(self, component: str, hypothesis: dict) -> None:
        """Hold a seeded hypothesis unless the model's reply already offered one for ``component``."""
        self.pending.setdefault(component, dict(hypothesis))

    def attach(self, parent: str, child: str) -> None:
        """Key the held hypotheses by ``parent`` and ``child`` content id."""
        held, self.pending = self.pending, {}
        if parent == child:
            return
        for component, hypothesis in held.items():
            record = {"kind": "hypothesis", "parent": parent, "child": child, "component": component,
                      "hypothesis": hypothesis}
            self._write(record)
            self.awaiting.setdefault(child, []).append(record)
            self.log(f"hypothesis: {parent[:12]} -> {child[:12]} {component}: "
                     f"{json.dumps(hypothesis, sort_keys=True)}")

    def settle(self, child: str, summary_of: Callable[[str], dict | None]) -> list[str]:
        """Judge the held claims about ``child`` once; later calls return nothing."""
        statuses = []
        for record in self.awaiting.pop(child, []):
            status, observed = judge(record["hypothesis"], summary_of(record["parent"]), summary_of(child))
            outcome = {"kind": "outcome", "parent": record["parent"], "child": child,
                       "component": record["component"], "status": status, "observed": observed,
                       "hypothesis": record["hypothesis"]}
            self._write(outcome)
            self.outcomes.append(outcome)
            self.log(f"hypothesis: {record['parent'][:12]} -> {child[:12]} {record['component']} "
                     f"{status} {json.dumps(observed, sort_keys=True)}")
            statuses.append(status)
        return statuses

    def feedback(self) -> list[dict]:
        """The record for later calls: how the last few predictions turned out."""
        if not self.outcomes:
            return []
        shown = [
            {"parent": o["parent"][:12], "child": o["child"][:12], "component": o["component"],
             "hypothesis": o["hypothesis"], "status": o["status"], "observed": o["observed"]}
            for o in self.outcomes[-FEEDBACK_SHOWN:]
        ]
        return [{"hypothesis_feedback": shown}]


class WeightedSelector:
    """gepa component selector: one component per proposal, weighted by the parent's attribution hints.

    Two bounds hold whatever the weights say. ``src_patch`` is picked within the first
    ``SRC_PATCH_BY_PROPOSAL`` proposals, and no component waits more than ``MAX_UNSELECTED`` proposals in a row.
    Each pick keeps every component's deadline reachable, so the bounds hold even when several components wait.
    """

    def __init__(self, dominant_of: Callable[[dict], list[dict] | None], log: Callable[[str], None],
                 rng: random.Random | None = None, directives=None):
        self.dominant_of = dominant_of
        self.log = log
        self.directives = directives
        self.rng = rng or random.Random(0)
        self.count = 0
        self.deadline: dict[str, int] = {}

    def _weights(self, names: list[str], dominant: list[dict]) -> dict[str, float]:
        total = sum(d["excess_chars_per_cell"] for d in dominant) or 1.0
        weights = {name: 1.0 for name in names}
        for entry in dominant:
            if entry["component_hint"] in weights:
                weights[entry["component_hint"]] += HINT_WEIGHT * entry["excess_chars_per_cell"] / total
        return weights

    def _reachable(self, deadlines: dict[str, int]) -> bool:
        """True when the components can take the slots after this proposal in deadline order."""
        slots = sorted(deadlines.values())
        return all(self.count + 1 + index <= due for index, due in enumerate(slots))

    def __call__(self, state, trajectories, subsample_scores, candidate_idx, candidate) -> list[str]:
        names = list(candidate)
        self.count += 1
        for name in names:
            self.deadline.setdefault(name, MAX_UNSELECTED + 1)
        if SRC_PATCH in names and self.deadline[SRC_PATCH] > SRC_PATCH_BY_PROPOSAL and self.count == 1:
            self.deadline[SRC_PATCH] = SRC_PATCH_BY_PROPOSAL
        if self.directives is not None and self.directives.pending:
            directive = self.directives.take(self.count)
            if directive.component in names:
                self.deadline[directive.component] = self.count + MAX_UNSELECTED + 1
                self.log(f"selector: proposal {self.count} parent {short_id(candidate)} -> {directive.component}: "
                         f"directive override {directive.id}")
                return [directive.component]
            self.log(f"selector: directive {directive.id} dropped: the candidate has no {directive.component}")
        feasible = []
        for name in names:
            after = {**self.deadline, name: self.count + MAX_UNSELECTED + 1}
            if self._reachable({k: after[k] for k in names}):
                feasible.append(name)
        dominant = self.dominant_of(candidate) or []
        weights = self._weights(feasible or names, dominant)
        if not feasible:
            pick = min(names, key=lambda n: (self.deadline[n], names.index(n)))
            reason = "deadline"
        elif len(feasible) < len(names):
            pick = self.rng.choices(feasible, [weights[n] for n in feasible])[0]
            reason = "deadline-bounded weighted choice"
        else:
            pick = self.rng.choices(feasible, [weights[n] for n in feasible])[0]
            reason = "weighted choice" if dominant else "uniform choice, no attribution"
        self.deadline[pick] = self.count + MAX_UNSELECTED + 1
        hints = "; ".join(f"{d['cause']} -> {d['component_hint']} ({d['excess_chars_per_cell']} chars/cell)"
                          for d in dominant) or "none"
        self.log(f"selector: proposal {self.count} parent {short_id(candidate)} -> {pick}: {reason}; "
                 f"evidence {hints}")
        return [pick]
