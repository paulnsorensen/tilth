"""Candidates that failed a deterministic gate, so that a re-proposed patch is not checked or repaired again.

The loop already writes each gate failure to ``log.txt`` (``header``'s line and the indented
tail), so a prior or resumed run seeds this ledger from that log. No separate state file exists.
"""

import re
from pathlib import Path

# Stages whose verdict depends only on the candidate's content. A cheap-tier or paid-tier loss is a score and an
# infrastructure fault has no verdict, so neither is a known failure.
GATE_STAGES = frozenset({"apply", "just check", "integrity", "build", "hook gate"})
ID_LENGTH = 12
MAX_LISTED = 5  # failed patches shown to one proposal
MAX_ERROR_CHARS = 200
_HEADER = re.compile(r"^candidate ([0-9a-f]{%d}): failed at (.+)$" % ID_LENGTH)
_TEST_NAME = re.compile(r"^---- .* ----$")
_ERROR = re.compile(r"^(?:error(?:\[\w+\])?: (?!could not compile|aborting due to|Recipe |test failed)|Diff in )")
_WORKTREE = re.compile(r"\S*/worktrees/[^/\s]+/")
# Before `just check infra` existed, a `just check` timeout was logged at `just check` with this line as its whole tail.
_OLD_JUST_CHECK_TIMEOUT = re.compile(r"just check timed out after \d+ s")


def header(cid: str, stage: str) -> str:
    """The log line that opens one gate failure; ``parse_log`` reads it back."""
    return f"candidate {cid[:ID_LENGTH]}: failed at {stage}"


def first_error(tail: str) -> str:
    """The one line of a failure tail that names the failure: a failed test, else an error, else the first line."""
    lines = [line.strip() for line in tail.splitlines() if line.strip() and line.strip() != "[...]"]
    for pattern in (_TEST_NAME, _ERROR):
        for line in lines:
            if pattern.match(line):
                return _clip(line)
    return _clip(lines[0]) if lines else ""


def _clip(line: str) -> str:
    return _WORKTREE.sub("", line)[:MAX_ERROR_CHARS]


def parse_log(text: str) -> list[tuple[str, str, str]]:
    """``(id12, stage, first error line)`` of every gate failure in a ``log.txt``, oldest first."""
    found: list[tuple[str, str, str]] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = _HEADER.match(line)
        if match is None or match[2] not in GATE_STAGES:
            continue
        tail = []
        for follow in lines[index + 1:]:
            if not follow.startswith("  "):
                break
            tail.append(follow[2:])
        tail_text = "\n".join(tail)
        if match[2] == "just check" and _OLD_JUST_CHECK_TIMEOUT.fullmatch(tail_text.strip()):
            continue  # an older log's timeout: no verdict on the patch
        found.append((match[1], match[2], first_error(tail_text)))
    return found


class FailedPatches:
    """Gate failures by the first 12 characters of the content id, in the order they happened."""

    def __init__(self) -> None:
        self.entries: dict[str, tuple[str, str]] = {}

    def add(self, cid: str, stage: str, tail: str) -> None:
        if stage in GATE_STAGES:
            self._put(cid[:ID_LENGTH], stage, first_error(tail))

    def get(self, cid: str) -> tuple[str, str] | None:
        return self.entries.get(cid[:ID_LENGTH])

    def seed_from(self, log_path: Path) -> int:
        """Add the gate failures that ``log_path`` records; the count read, 0 for a missing log."""
        try:
            found = parse_log(Path(log_path).read_text())
        except (OSError, UnicodeDecodeError):
            return 0
        for cid, stage, error in found:
            self._put(cid, stage, error)
        return len(found)

    def summary(self) -> list[dict]:
        """The latest ``MAX_LISTED`` failures as ``{id, stage, error}`` records."""
        return [{"id": cid, "stage": stage, "error": error}
                for cid, (stage, error) in list(self.entries.items())[-MAX_LISTED:]]

    def _put(self, cid: str, stage: str, error: str) -> None:
        self.entries.pop(cid, None)
        self.entries[cid] = (stage, error)
