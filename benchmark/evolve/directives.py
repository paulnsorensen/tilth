"""Seeded proposals (directives): a queue of named changes that evolve tries before it picks a component itself.

A directive file is a JSON list of ``{id, component, brief, hypothesis?}``. While directives remain, the
selector picks the head directive's component for the next proposal, and the dispatcher hands the brief to that
component's proposer or reflection call as a ``directive`` record. A directive is consumed when it is picked, once
per proposal, even if the proposal then fails. Directives never change scoring, rejection, or the content id.
"""

import json
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from pathlib import Path

from .diagnosis import parse_hypothesis

KEYS = frozenset({"id", "component", "brief", "hypothesis"})


class DirectiveError(ValueError):
    """The directive file is unusable."""


@dataclass(frozen=True)
class Directive:
    id: str
    component: str
    brief: str
    hypothesis: dict | None = None

    def record(self) -> dict:
        return {"directive": {"id": self.id, "brief": self.brief}}


def _parse(index: int, entry: object, components: Collection[str], forbidden: Iterable[str]) -> Directive:
    where = f"directive {index}"
    if not isinstance(entry, dict):
        raise DirectiveError(f"{where} is not an object")
    if unknown := sorted(set(entry) - KEYS):
        raise DirectiveError(f"{where} has unknown keys: {', '.join(unknown)}")
    for key in ("id", "component", "brief"):
        if not isinstance(entry.get(key), str) or not entry[key].strip():
            raise DirectiveError(f"{where} needs a non-empty string {key}")
    where = f"directive {entry['id']!r}"
    if entry["component"] not in components:
        raise DirectiveError(f"{where} names component {entry['component']!r}; "
                             f"expected one of {', '.join(sorted(components))}")
    if term := next((term for term in forbidden if term and term in entry["brief"]), None):
        raise DirectiveError(f"{where} brief names {term!r}, which the proposer must not see")
    hypothesis = None
    if entry.get("hypothesis") is not None:
        try:
            hypothesis = parse_hypothesis(f"<hypothesis>{json.dumps(entry['hypothesis'])}</hypothesis>")
        except ValueError as error:
            raise DirectiveError(f"{where}: {error}") from error
    return Directive(entry["id"], entry["component"], entry["brief"], hypothesis)


def load(path: Path, components: Collection[str], forbidden: Iterable[str] = ()) -> list[Directive]:
    """The directives in ``path``, in file order; DirectiveError when the file is unreadable or any entry is bad."""
    forbidden = list(forbidden)
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError) as error:
        raise DirectiveError(f"cannot read directives {path}: {error}") from error
    if not isinstance(raw, list):
        raise DirectiveError(f"directives {path} must be a JSON list")
    directives = [_parse(index, entry, components, forbidden) for index, entry in enumerate(raw)]
    ids = [directive.id for directive in directives]
    if duplicate := next((i for i in ids if ids.count(i) > 1), None):
        raise DirectiveError(f"directive id {duplicate!r} appears more than once")
    return directives


class DirectiveQueue:
    """The directives not yet picked, plus the one picked for the proposal now in flight."""

    def __init__(self, directives: Iterable[Directive], log: Callable[[str], None]) -> None:
        self.pending = list(directives)
        self.log = log
        self.active: Directive | None = None

    def take(self, proposal: int) -> Directive:
        """Consume the head directive for ``proposal``."""
        self.active = self.pending.pop(0)
        self.log(f"directive: {self.active.id} -> proposal {proposal} ({self.active.component})")
        return self.active

    def claim(self) -> Directive | None:
        """Hand the in-flight directive to the dispatcher, once."""
        active, self.active = self.active, None
        return active
