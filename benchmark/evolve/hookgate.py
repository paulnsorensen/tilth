"""Run the frozen hook corpus through a tilth binary's Bash hook, in both hint modes.

The seed preflight and the candidate hook gate share this check. A ``must_deny`` row that the hook allows, or a
``must_allow`` row that it denies, is a failure; the failure lines name the command so reflection can read them.
"""

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CORPUS_PATH = Path(__file__).resolve().parent.parent / "hook_corpus.json"
HINTS = ("tilth", "native")
HOOK_TIMEOUT_S = 30
WORKERS = 4


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
        return _validate(json.loads(path.read_text()))
    except (OSError, ValueError) as error:
        if isinstance(error, CorpusError):
            raise
        raise CorpusError(f"hook corpus {path} is unreadable: {error}") from error


def _exit_code(binary: str, hint: str, command: str) -> int | str:
    event = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    try:
        result = subprocess.run([binary, "hook", "--hint", hint], input=event, capture_output=True, text=True,
                                timeout=HOOK_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as error:
        return f"could not run: {error}"
    return result.returncode


def corpus_failures(binary: str, corpus: dict | None = None) -> list[str]:
    """One line per corpus row the hook at ``binary`` gets wrong in either hint mode; empty when faithful."""
    try:
        corpus = load_corpus() if corpus is None else _validate(corpus)
    except CorpusError as error:
        return [str(error)]
    checks = [(hint, row, 2) for hint in HINTS for row in corpus["must_deny"]]
    checks += [(hint, row, 0) for hint in HINTS for row in corpus["must_allow"]]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        codes = list(pool.map(lambda check: _exit_code(binary, check[0], check[1]["command"]), checks))
    failures = []
    for (hint, row, want), code in zip(checks, codes, strict=True):
        if code == want:
            continue
        verdict = f"must deny ({row.get('rule', '?')})" if want == 2 else "must allow"
        failures.append(f"--hint {hint}: {verdict} but got {_describe(code)}: `{row['command']}`")
    return failures


def _describe(code: int | str) -> str:
    if isinstance(code, str):
        return code
    return {0: "allow (exit 0)", 2: "deny (exit 2)"}.get(code, f"exit {code}")
