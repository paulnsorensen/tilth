"""Run the frozen hook corpus through a tilth binary's Bash hook, in both hint modes.

The seed preflight and the candidate hook gate share this check. A ``must_deny`` row that the hook allows, or a
``must_allow`` row that it denies, is a failure; the failure lines name the command so reflection can read them.
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
HOOK_TIMEOUT_S = 30
OVERALL_DEADLINE_S = 300
WORKERS = 4
DEADLINE_MESSAGE = "could not run: the overall hook corpus deadline passed"


class CorpusError(ValueError):
    """The hook corpus is missing, malformed, or empty, so it cannot judge a hook."""


def _validate(corpus: object) -> dict:
    if not isinstance(corpus, dict):
        raise CorpusError("hook corpus is not a JSON object")
    for key in ("must_deny", "must_allow"):
        rows = corpus.get(key)
        if not isinstance(rows, list) or not rows:
            raise CorpusError(f"hook corpus needs a non-empty `{key}` list")
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("command"), str):
                raise CorpusError(f"hook corpus `{key}` row without a string `command`: {row!r}")
    return corpus


def load_corpus(path: Path | None = None) -> dict:
    path = path or CORPUS_PATH
    try:
        corpus = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise CorpusError(f"hook corpus {path} is unreadable: {error}") from error
    return _validate(corpus)


def _exit_code(binary: str, hint: str, command: str, timeout: float) -> int | str:
    try:
        return run.run_hook(binary, hint, command, timeout=timeout).returncode
    except (OSError, subprocess.SubprocessError) as error:
        return f"could not run: {error}"


def corpus_failures(binary: str, corpus: dict | None = None, *, deadline_s: float | None = None) -> list[str]:
    """One line per corpus row the hook at ``binary`` gets wrong in either hint mode; empty when faithful.

    A hook that cannot run (timeout, missing binary) or a run past the overall deadline stops the check at once,
    so a hanging hook costs one timeout, not one per row.
    """
    try:
        corpus = load_corpus() if corpus is None else _validate(corpus)
    except CorpusError as error:
        return [str(error)]
    checks = [(hint, row, 2) for hint in HINTS for row in corpus["must_deny"]]
    checks += [(hint, row, 0) for hint in HINTS for row in corpus["must_allow"]]
    deadline = time.monotonic() + (OVERALL_DEADLINE_S if deadline_s is None else deadline_s)
    stop = threading.Event()

    def check(item: tuple[str, dict, int]) -> int | str | None:
        if stop.is_set():
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stop.set()
            return DEADLINE_MESSAGE
        code = _exit_code(binary, item[0], item[1]["command"], min(HOOK_TIMEOUT_S, remaining))
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
        failures.append(f"--hint {hint}: {verdict} but got {_describe(code)}: `{row['command']}`")
    if skipped := codes.count(None):
        failures.append(f"stopped early: {skipped} of {len(checks)} checks not run")
    return failures


def _describe(code: int | str) -> str:
    if isinstance(code, str):
        return code
    return {0: "allow (exit 0)", 2: "deny (exit 2)"}.get(code, f"exit {code}")