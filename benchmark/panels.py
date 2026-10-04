"""Pre-registered benchmark panels: a fixed task set split into a cheap tier and stratified dev and test.

A panel file under ``benchmark/panels/`` names its members by source family.
``load_panel`` is the only way to obtain a ``Panel``: it enforces the schema,
the required members, each member's language, the fallback rules, the tiers,
the stratified split, external admission on this host, and the split lock. The
lock is the first completed stored row stamped with the panel's name: after it,
a changed split must be pre-registered under a new panel name.
"""

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import baselines
import config
import external
import external.data
import external.featurebench
import external.preflight
import external.swebench_ml
import tasks

FAMILIES = ("local", "featurebench", "swebench_ml")
SPLITS = ("cheap", "dev", "test")
CHEAP = ("rg_search_dispatch", "rg_trait_implementors", "gin_servehttp_flow")
LOCAL = (*CHEAP, "gin_edit_render_context")
# Per-language SWE-bench Multilingual slots: (primary, fallback).
SLOTS: Mapping[str, tuple[str, str]] = external.swebench_ml.PICKS
_SLOT_LANGUAGE = {instance_id: language for language, pair in SLOTS.items() for instance_id in pair}

_PANEL_KEYS = {"name", "split_seed", "members"}
_MEMBER_KEYS = {"id", "family", "language", "split"}
_FALLBACK_KEYS = {"fallback_for", "fallback_reason"}


class PanelError(ValueError):
    """A panel file is malformed or incomplete, a member is not admitted, or its split changed after results."""


@dataclass(frozen=True)
class Member:
    id: str
    family: str
    language: str
    split: str
    fallback_for: str | None = None
    fallback_reason: str | None = None


@dataclass(frozen=True)
class Panel:
    name: str
    split_seed: int
    cheap: tuple[str, ...]
    dev: tuple[str, ...]
    test: tuple[str, ...]
    split_digest: str
    members: tuple[Member, ...]

    def member(self, task_name: str) -> Member:
        for member in self.members:
            if member.id == task_name:
                return member
        raise PanelError(f"{task_name} is not a member of panel {self.name}")

    def select(self, split: str) -> list[str]:
        """Member ids in manifest order for one split, or every member for ``all``."""
        return [member.id for member in self.members if split == "all" or member.split == split]

    def stamp(self, task_name: str) -> dict[str, str]:
        """The row fields that tie a stored row to this panel and the member's tier."""
        return {"panel_name": self.name, "panel_split_digest": self.split_digest,
                "panel_split": self.member(task_name).split}

    def register(self, tasks_by_name: dict) -> None:
        """Insert each external member into ``tasks_by_name`` through the c2 loaders.

        Re-registering a member whose task digest is unchanged keeps the entry
        already there. A different task under an existing name, including a
        local task, is refused.
        """
        from run import _cell_task_digest  # run imports this module

        for member in self.members:
            if member.family == "local":
                continue
            loader = external.featurebench.load if member.family == "featurebench" else external.swebench_ml.load
            dataset = external.data.FEATUREBENCH if member.family == "featurebench" else external.data.SWEBENCH_ML
            try:
                task = loader(member.id, external.data.REVISIONS[dataset])
            except (LookupError, ValueError, OSError) as error:
                raise PanelError(f"{member.id}: cannot load the external task: {error}") from error
            existing = tasks_by_name.get(task.name)
            if existing is not None:
                if not isinstance(existing, external.ExternalTask):
                    raise PanelError(f"{task.name}: panel member {member.id} collides with a local task")
                if _cell_task_digest(existing) != _cell_task_digest(task):
                    raise PanelError(f"{task.name}: already registered with a different task digest")
                continue
            if task.name != member.id:
                raise PanelError(f"{member.id}: the loader returned task {task.name}")
            tasks_by_name[task.name] = task


def _digest(payload: object) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def stratify(members: Iterable[Mapping[str, Any]], split_seed: int) -> dict[str, str]:
    """Assign every non-cheap member to ``dev`` or ``test`` by family-stratified alternation.

    Within a source family, members sort by ``(language, sha256("<seed>:<id>"))``
    and alternate dev, test, dev, so dev takes the extra member of an odd family.
    """
    assignment: dict[str, str] = {}
    pending = [member for member in members if member["id"] not in CHEAP]
    for family in FAMILIES:
        stratum = sorted((member for member in pending if member["family"] == family),
                         key=lambda member: (member["language"],
                                             hashlib.sha256(f"{split_seed}:{member['id']}".encode()).hexdigest()))
        for index, member in enumerate(stratum):
            assignment[member["id"]] = "dev" if index % 2 == 0 else "test"
    return assignment


def split_digest(members: Iterable[Member]) -> str:
    """Hash the sorted id lists of each split; nothing else about a member changes it."""
    lists: dict[str, list[str]] = {split: [] for split in SPLITS}
    for member in members:
        lists[member.split].append(member.id)
    return _digest({split: sorted(ids) for split, ids in lists.items()})


def _cached_row(instance_id: str) -> Mapping[str, Any] | None:
    return external.data.cached_row(instance_id)


def _parse(path: Path) -> tuple[str, int, list[Member]]:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise PanelError(f"{path}: cannot read panel: {error}") from error
    if not isinstance(data, dict):
        raise PanelError(f"{path}: a panel is a JSON object")
    if unknown := sorted(set(data) - _PANEL_KEYS):
        raise PanelError(f"{path}: unknown key(s) {', '.join(unknown)}")
    if missing := sorted(_PANEL_KEYS - set(data)):
        raise PanelError(f"{path}: missing key(s) {', '.join(missing)}")
    name, seed, entries = data["name"], data["split_seed"], data["members"]
    if not isinstance(name, str) or not name.strip():
        raise PanelError(f"{path}: name must be a non-empty string")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise PanelError(f"{path}: split_seed must be an integer")
    if not isinstance(entries, list):
        raise PanelError(f"{path}: members must be a list")
    members, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise PanelError(f"{path}: each member is a JSON object")
        member_id = entry.get("id")
        allowed = _MEMBER_KEYS | (_FALLBACK_KEYS if entry.get("family") == "swebench_ml" else set())
        if unknown := sorted(set(entry) - allowed):
            raise PanelError(f"{member_id}: unknown member key(s) {', '.join(unknown)}")
        if missing := sorted(_MEMBER_KEYS - set(entry)):
            raise PanelError(f"{member_id}: missing member key(s) {', '.join(missing)}")
        if not all(isinstance(entry[key], str) for key in _MEMBER_KEYS):
            raise PanelError(f"{member_id}: id, family, language, and split must be strings")
        if member_id in seen:
            raise PanelError(f"{member_id}: duplicate member id")
        seen.add(member_id)
        if entry["family"] not in FAMILIES:
            raise PanelError(f"{member_id}: unknown family {entry['family']!r} (expected one of {', '.join(FAMILIES)})")
        if entry["split"] not in SPLITS:
            raise PanelError(f"{member_id}: unknown split {entry['split']!r} (expected one of {', '.join(SPLITS)})")
        members.append(Member(**entry))
    return name, seed, members


def _check_members(members: list[Member]) -> None:
    """Refuse members outside the required families and ids, and malformed fallbacks."""
    for member in members:
        if member.family == "local":
            task = tasks.TASKS.get(member.id)
            if task is None or isinstance(task, external.ExternalTask):
                raise PanelError(f"{member.id}: not a local task in TASKS")
            if member.id not in LOCAL:
                raise PanelError(f"{member.id}: not a panel local member (allowed: {', '.join(LOCAL)})")
        elif member.family == "featurebench":
            if external.featurebench.level_of(member.id) != "lv1" or not external.data.is_safe_id(member.id):
                raise PanelError(f"{member.id}: not a FeatureBench Lite Level 1 instance id")
        elif member.id not in _SLOT_LANGUAGE:
            raise PanelError(f"{member.id}: not a SWE-bench Multilingual slot id "
                             f"({', '.join(sorted(_SLOT_LANGUAGE))})")
        else:
            primary, fallback = SLOTS[_SLOT_LANGUAGE[member.id]]
            if member.id == primary and (member.fallback_for is not None or member.fallback_reason is not None):
                raise PanelError(f"{member.id}: a slot primary carries no fallback_for or fallback_reason")
            if member.id == fallback:
                if member.fallback_for != primary:
                    raise PanelError(f"{member.id}: a fallback needs fallback_for {primary!r}, "
                                     f"not {member.fallback_for!r}")
                if not isinstance(member.fallback_reason, str) or not member.fallback_reason.strip():
                    raise PanelError(f"{member.id}: a fallback needs a non-empty fallback_reason")
    ids = {member.id for member in members}
    for language, pair in SLOTS.items():
        if set(pair) <= ids:
            raise PanelError(f"the {language} slot holds both {pair[0]} and its fallback {pair[1]}")


def _check_complete(members: list[Member]) -> None:
    ids = {member.id for member in members}
    missing = [name for name in LOCAL if name not in ids]
    if not any(member.family == "featurebench" for member in members):
        missing.append("a featurebench member")
    missing += [f"the {language} slot ({' or '.join(pair)})" for language, pair in SLOTS.items()
                if not set(pair) & ids]
    if missing:
        raise PanelError(f"panel is incomplete; missing: {'; '.join(missing)}")


def _check_languages(members: list[Member], row_source: Callable[[str], Mapping[str, Any] | None]) -> None:
    for member in members:
        if member.family == "local":
            expected = config.REPOS[tasks.TASKS[member.id].repo].language
            source = "its repo language"
        else:
            required = "python" if member.family == "featurebench" else _SLOT_LANGUAGE[member.id]
            if member.language != required:
                raise PanelError(f"{member.id}: a {member.family} member here declares {required!r}, "
                                 f"not {member.language!r}")
            row = row_source(member.id)
            if row is None:
                raise PanelError(f"{member.id}: no dataset row to check its language; "
                                 "run benchmark/external/preflight.py --panel")
            try:
                expected = external.language_of(row)
            except ValueError as error:
                raise PanelError(f"{member.id}: {error}") from error
            source = "external.language_of on its dataset row"
        if member.language != expected:
            raise PanelError(f"{member.id}: declared language {member.language!r} differs from "
                             f"{source} {expected!r}")


def _check_split(members: list[Member], split_seed: int) -> None:
    for member in members:
        if (member.id in CHEAP) != (member.split == "cheap"):
            expected = "cheap" if member.id in CHEAP else "dev or test"
            raise PanelError(f"{member.id}: split {member.split!r}, but the cheap tier is exactly "
                             f"{', '.join(CHEAP)} (expected {expected})")
    assignment = stratify([{"id": m.id, "family": m.family, "language": m.language} for m in members], split_seed)
    differing = [f"{member.id} recorded {member.split}, expected {assignment[member.id]}"
                 for member in members if member.id in assignment and member.split != assignment[member.id]]
    if differing:
        raise PanelError(f"recorded split differs from panels.stratify(seed={split_seed}): {'; '.join(differing)}")


def _check_admitted(members: list[Member], admit: Callable[[str], Any]) -> None:
    refused = []
    for member in members:
        if member.family == "local":
            continue
        try:
            verdict = admit(member.id)
        except LookupError as error:
            refused.append(f"{member.id} ({error})")
            continue
        if not verdict.admitted:
            refused.append(f"{member.id} ({verdict.reason})")
    if refused:
        raise PanelError(f"external member(s) not admitted on this host: {'; '.join(refused)}")


def _check_lock(name: str, digest: str, store_path: Path) -> None:
    for row in baselines.rows(store_path, panel_name=name):
        if baselines.is_completed(row) and row.get("panel_split_digest") != digest:
            raise PanelError(
                f"panel {name}'s split changed after results were stored (stored digest "
                f"{row.get('panel_split_digest')}, current {digest}); register the new split under a new panel name"
            )


def load_panel(
    path: Path,
    *,
    store_path: Path,
    admit: Callable[[str], Any] | None = None,
    row_source: Callable[[str], Mapping[str, Any] | None] | None = None,
) -> Panel:
    """Load and validate a panel; raise ``PanelError`` on any fault.

    ``store_path`` is the result store the caller writes (``run.py`` passes
    ``RESULTS_DIR / baselines.STORE_FILENAME``). ``admit`` defaults to
    ``external.preflight.admit`` and ``row_source`` to the cached dataset rows in
    ``$TILTH_BENCH_DATA``; both are looked up when called.
    """
    name, split_seed, members = _parse(path)
    _check_members(members)
    _check_complete(members)
    _check_languages(members, row_source or _cached_row)
    _check_split(members, split_seed)
    _check_admitted(members, admit or external.preflight.admit)
    digest = split_digest(members)
    _check_lock(name, digest, store_path)
    by_split = {split: tuple(member.id for member in members if member.split == split) for split in SPLITS}
    return Panel(name=name, split_seed=split_seed, cheap=by_split["cheap"], dev=by_split["dev"],
                 test=by_split["test"], split_digest=digest, members=tuple(members))
