"""Run the frozen hook corpus through a tilth binary's Bash hook, in both hint modes.

The seed preflight and the candidate hook gate share this check. A ``must_deny`` row that the hook allows, or a
``must_allow`` row that it denies, is a failure; the failure lines name the command so reflection can read them.

The check runs the hook in the mode that the cells of the run use. Without ``--allow-capture``, the two default
lists hold. With the flag, the ``allow_capture`` section holds, and so does each default row: a default
``must_deny`` row still denies unless ``allow_capture.must_allow`` lists its command. ``just check`` holds
every candidate to both modes before this gate, through the Rust tests of the same corpus.
"""

import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import run

CORPUS_PATH = Path(__file__).resolve().parent.parent / "hook_corpus.json"
HINTS = ("tilth", "native")
LISTS = ("must_deny", "must_allow")
CAPTURE_SECTION = "allow_capture"
HOOK_TIMEOUT_S = 30
OVERALL_DEADLINE_S = 300
WORKERS = 4
DEADLINE_MESSAGE = "could not run: the overall hook corpus deadline passed"


class CorpusError(ValueError):
    """The hook corpus is missing, malformed, or empty, so it cannot judge a hook."""


def _validate_lists(holder: object, where: str) -> None:
    if not isinstance(holder, dict):
        raise CorpusError(f"{where} is not a JSON object")
    for key in LISTS:
        rows = holder.get(key)
        if not isinstance(rows, list) or not rows:
            raise CorpusError(f"{where} needs a non-empty `{key}` list")
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("command"), str):
                raise CorpusError(f"{where} `{key}` row without a string `command`: {row!r}")


def _validate(corpus: object, capture: bool = False) -> dict:
    _validate_lists(corpus, "hook corpus")
    if capture:
        _validate_lists(corpus.get(CAPTURE_SECTION), f"hook corpus `{CAPTURE_SECTION}` section")
    return corpus


def load_corpus(path: Path | None = None, capture: bool = False) -> dict:
    path = path or CORPUS_PATH
    try:
        corpus = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise CorpusError(f"hook corpus {path} is unreadable: {error}") from error
    return _validate(corpus, capture)


def _checks(corpus: dict, capture: bool) -> list[tuple[str, dict, int]]:
    """Every (hint, row, wanted exit code) the hook must answer, with ``--allow-capture`` when ``capture``."""
    deny, allow = corpus["must_deny"], corpus["must_allow"]
    if not capture:
        rows = [(row, 2) for row in deny] + [(row, 0) for row in allow]
    else:
        section = corpus[CAPTURE_SECTION]
        lifted = {row["command"] for row in section["must_allow"]}
        both = ([(row, 2) for row in deny if row["command"] not in lifted]
                + [(row, 0) for row in allow]
                + [(row, 2) for row in section["must_deny"]]
                + [(row, 0) for row in section["must_allow"]])
        # A command in both a default list and the section is one check. Two wanted verdicts for one
        # command stay two checks, so the corpus conflict fails the gate.
        rows = list({(row["command"], want): (row, want) for row, want in both}.values())
    return [(hint, row, want) for hint in HINTS for row, want in rows]


def _exit_code(binary: str, hint: str, command: str, timeout: float, capture: bool) -> int | str:
    try:
        return run.run_hook(binary, hint, command, timeout=timeout, capture=capture).returncode
    except (OSError, subprocess.SubprocessError) as error:
        return f"could not run: {error}"


def corpus_failures(binary: str, corpus: dict | None = None, *, deadline_s: float | None = None,
                    capture: bool | None = None) -> list[str]:
    """One line per corpus row the hook at ``binary`` gets wrong in either hint mode; empty when faithful.

    ``capture`` runs the hook with ``--allow-capture`` and checks the rows of that mode; the default is the
    process switch (``run.hook_capture``), so the gate checks the mode that the cells of this run use.

    A hook that cannot run (timeout, missing binary) or a run past the overall deadline stops the check at once,
    so a hanging hook costs one timeout, not one per row.
    """
    capture = run.hook_capture() if capture is None else capture
    try:
        corpus = load_corpus(capture=capture) if corpus is None else _validate(corpus, capture)
    except CorpusError as error:
        return [str(error)]
    checks = _checks(corpus, capture)
    deadline = time.monotonic() + (OVERALL_DEADLINE_S if deadline_s is None else deadline_s)
    stop = threading.Event()

    def check(item: tuple[str, dict, int]) -> int | str | None:
        if stop.is_set():
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stop.set()
            return DEADLINE_MESSAGE
        hint, row, _want = item
        code = _exit_code(binary, hint, row["command"], min(HOOK_TIMEOUT_S, remaining), capture)
        if isinstance(code, str):
            stop.set()
            if time.monotonic() >= deadline:
                return DEADLINE_MESSAGE
        return code

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        codes = list(pool.map(check, checks))
    failures = []
    for (hint, row, want), code in zip(checks, codes, strict=True):
        if code is None or code == want:
            continue
        verdict = f"must deny ({row.get('rule', '?')})" if want == 2 else "must allow"
        mode = f"--hint {hint}" + (f" {run.HOOK_CAPTURE_FLAG}" if capture else "")
        failures.append(f"{mode}: {verdict} but got {_describe(code)}: `{row['command']}`")
    if skipped := codes.count(None):
        failures.append(f"stopped early: {skipped} of {len(checks)} checks not run")
    return failures


def _describe(code: int | str) -> str:
    if isinstance(code, str):
        return code
    return {0: "allow (exit 0)", 2: "deny (exit 2)"}.get(code, f"exit {code}")