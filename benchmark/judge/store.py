"""Judge caches and agreement records under ``config.JUDGE_DIR``.

Labels are keyed by task digest, judge model, and applicability prompt hash;
critiques by run key, judge model, and critique prompt hash. Each cache entry keeps
the cost of the call that produced it, which feeds the judge-call estimate. Nothing
here touches result rows or the result store.
"""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Literal

from jsonl import tolerant_jsonl

from . import config

Kind = Literal["applicability", "critique"]

_CACHE_FILES: dict[Kind, str] = {"applicability": "labels.jsonl", "critique": "critiques.jsonl"}
_AGREEMENT_FILE = "agreements.jsonl"
NO_RECORD_REASON = "no calibration record for the current judge model, prompts, and calibration file"


@dataclass(frozen=True)
class Agreement:
    """Judge-versus-hand-label agreement from one calibration run."""

    calibrated: bool
    reason: str = ""
    label_kappa: float | None = None
    verdict_kappa: float | None = None
    label_count: int = 0
    verdict_count: int = 0
    threshold: float = config.KAPPA_THRESHOLD
    model: str = ""
    applicability_prompt_hash: str = ""
    critique_prompt_hash: str = ""
    calibration_digest: str = ""
    tasks: tuple[str, ...] = ()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def prompt_template(kind: Kind) -> str:
    return (config.PROMPTS_DIR / f"{kind}.md").read_text()


def prompt_hash(kind: Kind) -> str:
    return _sha256(prompt_template(kind).encode())


def calibration_digest(path: Path | None = None) -> str:
    return _sha256((path or config.CALIBRATION_FILE).read_bytes())


def judge_stamp() -> dict[str, str]:
    """The judge model and prompt hashes an agreement record carries."""
    return {
        "model": config.JUDGE_MODEL,
        "applicability_prompt_hash": prompt_hash("applicability"),
        "critique_prompt_hash": prompt_hash("critique"),
    }


def current_stamp() -> dict[str, str]:
    """The identity a stored agreement must match to count as current."""
    return {**judge_stamp(), "calibration_digest": calibration_digest()}


def _key(kind: Kind, subject: str) -> str:
    return _sha256(json.dumps([subject, config.JUDGE_MODEL, prompt_hash(kind)]).encode())


def label_key(task_digest: str) -> str:
    return _key("applicability", task_digest)


def critique_key(run_key: str) -> str:
    return _key("critique", run_key)


def _read(name: str) -> list[dict]:
    try:
        return tolerant_jsonl((config.JUDGE_DIR / name).read_text())
    except FileNotFoundError:
        return []


def _append(name: str, entry: dict) -> None:
    path = config.JUDGE_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as cache:
        cache.write(json.dumps(entry) + "\n")


def entries(kind: Kind) -> list[dict]:
    return _read(_CACHE_FILES[kind])


def _cached(kind: Kind, key: str) -> dict | None:
    return next((entry for entry in reversed(entries(kind)) if entry.get("key") == key), None)


def cached_label(task_digest: str) -> str | None:
    entry = _cached("applicability", label_key(task_digest))
    return entry["label"] if entry else None


def cached_labels(task_digests: Iterable[str]) -> dict[str, str]:
    """Map each task digest with a cached label to that label, reading the cache once."""
    by_key = {entry.get("key"): entry["label"] for entry in entries("applicability")}
    return {digest: by_key[key] for digest in set(task_digests) if (key := label_key(digest)) in by_key}


def cached_critique(run_key: str) -> str | None:
    entry = _cached("critique", critique_key(run_key))
    return entry["critique"] if entry else None


def put_label(*, task: str, task_digest: str, label: str, cost: float) -> None:
    _append(_CACHE_FILES["applicability"], {
        "key": label_key(task_digest), "task": task, "task_digest": task_digest,
        "model": config.JUDGE_MODEL, "label": label, "cost": cost,
    })


def put_critique(*, run_key: str, critique: str, cost: float) -> None:
    _append(_CACHE_FILES["critique"], {
        "key": critique_key(run_key), "run_key": run_key, "model": config.JUDGE_MODEL,
        "critique": critique, "cost": cost,
    })


def write_agreement(agreement: Agreement) -> None:
    _append(_AGREEMENT_FILE, asdict(agreement))


def current_agreement() -> Agreement | None:
    """The newest stored agreement whose model, prompt hashes, and calibration digest are current."""
    stamp = current_stamp()
    names = {field.name for field in fields(Agreement)}
    for record in reversed(_read(_AGREEMENT_FILE)):
        if all(record.get(key) == value for key, value in stamp.items()):
            known = {key: value for key, value in record.items() if key in names}
            return Agreement(**{**known, "tasks": tuple(known.get("tasks", ()))})
    return None
