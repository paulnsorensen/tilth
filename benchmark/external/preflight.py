"""Admission of external instances: a native gold, empty, and tampered round trip on this host.

``python3 benchmark/external/preflight.py`` admits the FeatureBench candidates and
the per-language SWE-bench Multilingual picks (falling back only when a primary
is refused). ``--panel PATH`` admits exactly a panel's external members. Both
forms fetch uncached rows first and exit nonzero when a required family has no
admitted instance.

The tampered check grades the gold patch minus one hunk at a time, largest hunk
first, up to ``$TILTH_BENCH_TAMPER_MAX_HUNKS`` hunks (default 8); an instance
passes it when any one removal fails the held-out tests.
"""

import sys
from pathlib import Path

if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from external.preflight import main as _main

    raise SystemExit(_main())

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from typing import Literal

from . import data, download, featurebench, patches, proc, registry, swebench_ml
from .task import EnvBuildError, ExternalTask, PrepareError, apply_patch

Reason = Literal[
    "admitted", "admitted:mask_patch_forward", "level2", "unclassified", "env_build_failed",
    "prepare_failed", "gold_unresolved", "empty_resolved", "tampered_resolved",
]
_PROBES = {
    "python": (("uv", "--version"), ("python3", "--version")),
    "go": (("go", "version"),),
    "rust": (("rustc", "--version"), ("cargo", "--version")),
}
_DEPENDENCY_FILES = (
    "Cargo.toml", "Cargo.lock", "go.mod", "go.sum", "pyproject.toml", "setup.py", "setup.cfg",
    "requirements.txt", "uv.lock", "poetry.lock", "Pipfile.lock",
)
_EXTERNAL_FAMILIES = (data.FEATUREBENCH, data.SWEBENCH_ML)
# Bump when admission or grading changes, so cached verdicts are recomputed.
VERDICT_VERSION = 2
DEFAULT_TAMPER_MAX_HUNKS = 8


def tamper_max_hunks() -> int:
    """How many single-hunk removals the tampered check tries: ``$TILTH_BENCH_TAMPER_MAX_HUNKS``, else 8."""
    return int(os.environ.get("TILTH_BENCH_TAMPER_MAX_HUNKS") or DEFAULT_TAMPER_MAX_HUNKS)


@dataclass(frozen=True)
class PreflightVerdict:
    """Whether an instance discriminates on this host, and the pass counts that decided it."""

    instance_id: str
    dataset: str
    data_rev: str
    env_fingerprint: str
    admitted: bool
    reason: Reason
    gold: tuple[int, int] | None = None
    empty: tuple[int, int] | None = None
    tampered: tuple[int, int] | None = None
    detail: str = ""
    tampered_hunk: int | None = None
    tamper_max_hunks: int | None = None
    version: int = VERDICT_VERSION

    def counts(self) -> str:
        return " ".join(f"{name}={pair[0]}/{pair[1]}" if pair else f"{name}=-"
                        for name, pair in (("gold", self.gold), ("empty", self.empty), ("tampered", self.tampered)))


def verdict_path(instance_id: str, data_rev: str, fingerprint: str) -> Path:
    return data.data_dir() / "verdicts" / data_rev / instance_id / f"{fingerprint}.json"


def _read_verdict(path: Path) -> PreflightVerdict | None:
    try:
        stored = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(stored, dict) or stored.get("version") != VERDICT_VERSION:
        return None
    if stored.get("tamper_max_hunks") != tamper_max_hunks():
        return None
    for name in ("gold", "empty", "tampered"):
        if stored.get(name) is not None:
            stored[name] = tuple(stored[name])
    try:
        return PreflightVerdict(**stored)
    except TypeError:
        # Written by another verdict schema: recompute rather than trust it.
        return None


def _probe(argv: tuple[str, ...]) -> str | None:
    try:
        result = proc.run(argv, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = (result.stdout or result.stderr).strip().splitlines()
    return lines[0] if result.returncode == 0 and lines else None


def env_fingerprint(task: ExternalTask) -> str:
    """Hash this host's toolchain versions for the task's language and its base dependency files."""
    payload = {
        "toolchains": {" ".join(argv): _probe(argv) for argv in _PROBES[task.language]},
        "python": task.python_version,
        "dependency_files": task.base_file_hashes(_DEPENDENCY_FILES),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _reset(workdir: Path) -> None:
    for argv in (["git", "reset", "-q", "--hard"], ["git", "clean", "-q", "-f", "-d"]):
        if proc.run(argv, cwd=workdir).returncode:
            raise PrepareError(f"{' '.join(argv)} failed in {workdir}")


def _grade(task: ExternalTask, workdir: Path, patch: str) -> tuple[bool, tuple[int, int]]:
    """Resolve ``patch`` on the prepared tree; a patch that does not apply does not resolve."""
    _reset(workdir)
    total = len(task.fail_to_pass) + len(task.pass_to_pass)
    if patch and not apply_patch(workdir, patch):
        return False, (0, total)
    resolved, _ = task.check_correctness("", str(workdir))
    details = task.grade_details()
    return resolved, (details["f2p_passed"] + details["p2p_passed"], total)


def _tamper(task: ExternalTask, workdir: Path, max_hunks: int) -> tuple[bool, dict]:
    """Grade single-hunk removals of the gold patch until one fails the held-out tests.

    Returns whether every applying removal still resolved, and the verdict fields
    that record the counts, the failing hunk index, and the hunks tried.
    """
    tried: list[str] = []
    counts = None
    for index, path, patch in patches.hunk_removals(task.gold_patch, max_hunks):
        _reset(workdir)
        if patch and not apply_patch(workdir, patch):
            tried.append(f"{index} (does not apply)")
            continue
        resolved, counts = _grade(task, workdir, patch)
        if not resolved:
            return False, {"tampered": counts, "tampered_hunk": index,
                           "detail": f"tampered: removing gold hunk {index} ({path}) fails the held-out tests"}
        tried.append(str(index))
    return True, {"tampered": counts,
                  "detail": f"tampered: every single-hunk removal tried still resolves (hunks {', '.join(tried)})"}


def round_trip(task: ExternalTask, fingerprint: str) -> PreflightVerdict:
    """Prepare the task on this host and grade its gold, empty, and single-hunk tampered patches."""
    max_hunks = tamper_max_hunks()

    def verdict(reason: Reason, **fields) -> PreflightVerdict:
        return PreflightVerdict(instance_id=task.name, dataset=task.dataset, data_rev=task.data_rev,
                                env_fingerprint=fingerprint, admitted=reason.startswith("admitted"),
                                reason=reason, tamper_max_hunks=max_hunks, **fields)

    with tempfile.TemporaryDirectory(prefix="tilth-preflight-") as temp:
        workdir = Path(temp) / "repo"
        try:
            task.prepare_tree(workdir)
        except PrepareError as error:
            return verdict("prepare_failed", detail=str(error))
        try:
            task.build_env(workdir)
            if task.language == "python":
                task.grade_venv()
        except EnvBuildError as error:
            return verdict("env_build_failed", detail=str(error))
        except PrepareError as error:
            return verdict("prepare_failed", detail=str(error))
        try:
            gold_ok, gold = _grade(task, workdir, task.gold_patch)
            empty_ok, empty = _grade(task, workdir, "")
            if not gold_ok:
                return verdict("gold_unresolved", gold=gold, empty=empty)
            if empty_ok:
                return verdict("empty_resolved", gold=gold, empty=empty)
            tampered_ok, tampered = _tamper(task, workdir, max_hunks)
        except PrepareError as error:
            return verdict("prepare_failed", detail=str(error))
    if tampered_ok:
        return verdict("tampered_resolved", gold=gold, empty=empty, **tampered)
    admitted: Reason = "admitted" if task.transformation == "none" else f"admitted:{task.transformation}"
    return verdict(admitted, gold=gold, empty=empty, **tampered)


def admit(instance_id: str) -> PreflightVerdict:
    """Admit a cached instance, reusing a verdict cached under this host's env fingerprint.

    Never downloads and makes no model call; raises LookupError for an uncached
    instance. Every round-trip verdict is cached; delete its file under
    ``verdicts/`` to retry one that failed for a transient reason.
    """
    dataset = data.cached_dataset(instance_id)
    if dataset is None:
        raise LookupError(f"{instance_id} has no cached row; run benchmark/external/preflight.py")
    data_rev = data.REVISIONS[dataset]
    if dataset == data.FEATUREBENCH and featurebench.level_of(instance_id) != "lv1":
        reason: Reason = "level2" if featurebench.level_of(instance_id) == "lv2" else "unclassified"
        return PreflightVerdict(instance_id=instance_id, dataset=dataset, data_rev=data_rev, env_fingerprint="",
                                admitted=False, reason=reason)
    task = registry.load(instance_id)
    try:
        fingerprint = env_fingerprint(task)
    except PrepareError as error:
        return PreflightVerdict(instance_id=instance_id, dataset=dataset, data_rev=data_rev, env_fingerprint="",
                                admitted=False, reason="prepare_failed", detail=str(error))
    path = verdict_path(instance_id, data_rev, fingerprint)
    cached = _read_verdict(path)
    if cached is not None:
        return cached
    verdict = round_trip(task, fingerprint)
    path.parent.mkdir(parents=True, exist_ok=True)
    download.write_atomically(path, json.dumps(asdict(verdict)))
    return verdict


# --- CLI ---


def _ensure_rows(dataset: str, instance_ids: list[str]) -> None:
    if any(data.cached_row(instance_id) is None for instance_id in instance_ids):
        download.fetch(dataset, extra_ids=instance_ids)


def _admit_and_print(instance_id: str) -> PreflightVerdict | None:
    try:
        verdict = admit(instance_id)
    except LookupError:
        print(f"{instance_id}: not_found")
        return None
    print(f"{instance_id}: {verdict.reason} {verdict.counts()}")
    return verdict


def _candidates() -> int:
    picks = swebench_ml.PICKS
    fb_candidates = featurebench.candidates()
    _ensure_rows(data.FEATUREBENCH, fb_candidates)
    _ensure_rows(data.SWEBENCH_ML, [instance_id for pair in picks.values() for instance_id in pair])
    admitted = {"featurebench": 0}
    for instance_id in fb_candidates:
        verdict = _admit_and_print(instance_id)
        admitted["featurebench"] += verdict is not None and verdict.admitted
    for language in ("go", "rust"):
        admitted[language] = 0
        for instance_id in picks.get(language, ()):
            verdict = _admit_and_print(instance_id)
            if verdict is not None and verdict.admitted:
                admitted[language] = 1
                break
    for family, count in admitted.items():
        print(f"{family}_admitted: {count}")
    empty = [family for family, count in admitted.items() if count == 0]
    for family in empty:
        print(f"ERROR: no {family} instance was admitted on this host", file=sys.stderr)
    return 1 if empty else 0


def _panel(path: Path) -> int:
    members = json.loads(path.read_text())["members"]
    external = [(member["id"], member["family"]) for member in members if member.get("family") in _EXTERNAL_FAMILIES]
    for family in _EXTERNAL_FAMILIES:
        _ensure_rows(family, [instance_id for instance_id, member_family in external if member_family == family])
    refused = []
    featurebench_admitted = 0
    for instance_id, family in external:
        verdict = _admit_and_print(instance_id)
        if verdict is None or not verdict.admitted:
            refused.append(f"{instance_id} ({verdict.reason if verdict else 'not_found'})")
        elif family == data.FEATUREBENCH:
            featurebench_admitted += 1
    for member in refused:
        print(f"ERROR: panel member refused: {member}", file=sys.stderr)
    if not featurebench_admitted:
        print("ERROR: no featurebench panel member was admitted on this host", file=sys.stderr)
    return 1 if refused or not featurebench_admitted else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Admit external benchmark instances on this host")
    parser.add_argument("--panel", type=Path, help="Admit exactly this panel's external members")
    args = parser.parse_args(argv)
    return _panel(args.panel) if args.panel else _candidates()
