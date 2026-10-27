"""Deep diagnosis: one Codex run per parent candidate that compares its dev rollouts with the baseline's.

The run reads a scratch directory of neutrally named dev-split trajectory copies and a ``summary.json``, and
answers with a turn-by-turn comparison and a ranked JSON list of causes. The parsed list, scrubbed of forbidden
terms and absolute paths, reaches reflection and the proposer as a ``deep_diagnosis`` record. It never scores or
rejects a candidate. A failure only costs the record. Codex spend is not on the Anthropic ledger.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Collection, Iterable, Mapping
from pathlib import Path

DEFAULT_MODEL = "gpt-6-astra"
DEFAULT_EFFORT = "xhigh"
DEFAULT_MAX_RUNS = 10
DEFAULT_TIMEOUT_S = 1800
TOP_CAUSES = 5
CAUSE_KEYS = ("cause", "evidence", "component_hint", "suggested_change", "expected_calls_saved")
ABSOLUTE_PATH = re.compile(r"(?<![\w.~])(?:/[\w.\-@+]+){2,}/?|(?<![\w])[A-Za-z]:\\[^\s\"']+")
PROMPT = (
    "The current directory holds agent rollouts on coding tasks: task-<n>-tilth-rep<k>.jsonl are runs with the "
    "tilth MCP server, task-<n>-baseline-rep<k>.jsonl are runs with the host's built-in tools, and summary.json "
    "lists each run's task index, arm, repetition, correctness, cost, and API calls. Tilth runs need more API "
    "calls (dependent model turns) than baseline runs. Compare the two arms task by task, turn by turn, and find "
    "what makes the tilth runs need extra turns. Then end your answer with one JSON array in a ```json fence, "
    "the top 5 causes ranked by expected API calls saved. Each element is an object with the keys cause, evidence, "
    "component_hint (src_patch, or the prompts file that should change), suggested_change, and "
    "expected_calls_saved (a number per run). Name no absolute path."
)

Runner = Callable[[list[str], float], int]


def codex_runner(argv: list[str], timeout: float) -> int:
    """Run ``argv`` in its own session; kill the whole group on a timeout. Returns the exit code."""
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise


def parse_causes(text: str) -> list[dict] | None:
    """The last JSON array of objects in ``text`` (a fenced block first), or None when there is none."""
    candidates = [m.group(1) for m in re.finditer(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S)][::-1]
    decoder = json.JSONDecoder()
    for start in reversed([m.start() for m in re.finditer(r"\[", text)]):
        candidates.append(text[start:])
    for chunk in candidates:
        try:
            value, _end = decoder.raw_decode(chunk.lstrip())
        except ValueError:
            continue
        if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            return value
    return None


def scrub(causes: list[dict], forbidden: Iterable[str]) -> list[dict]:
    """At most five entries with only the known keys. An entry naming a forbidden term is dropped.

    An absolute path inside a string is replaced by ``<path>``.
    """
    terms = [term for term in forbidden if term]
    kept = []
    for entry in causes:
        clean = {}
        for key in CAUSE_KEYS:
            value = entry.get(key)
            if isinstance(value, str):
                clean[key] = value
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                clean[key] = value
        if not clean.get("cause") or any(term in value for value in clean.values() if isinstance(value, str)
                                         for term in terms):
            continue
        kept.append({key: ABSOLUTE_PATH.sub("<path>", value) if isinstance(value, str) else value
                     for key, value in clean.items()})
        if len(kept) == TOP_CAUSES:
            break
    return kept


class DeepDiagnosis:
    """``records(cid, tilth_rows, baseline_rows)``: the memoized ``deep_diagnosis`` record for one parent."""

    def __init__(self, scratch_root: Path, *, dev_tasks: Collection[str], forbidden: Iterable[str],
                 log: Callable[[str], None], model: str = DEFAULT_MODEL, effort: str = DEFAULT_EFFORT,
                 max_runs: int = DEFAULT_MAX_RUNS, timeout: float = DEFAULT_TIMEOUT_S,
                 runner: Runner | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.scratch_root = Path(scratch_root)
        self.dev_tasks = frozenset(dev_tasks)
        self.forbidden = [term for term in dict.fromkeys(forbidden) if term]
        self.log = log
        self.model = model
        self.effort = effort
        self.max_runs = max_runs
        self.timeout = timeout
        self.runner = runner or codex_runner
        self.clock = clock
        self.runs = 0
        self.memo: dict[str, list[dict]] = {}

    def records(self, cid: str, tilth_rows: Iterable[Mapping], baseline_rows: Iterable[Mapping]) -> list[dict]:
        if cid not in self.memo:
            self.memo[cid] = self._run(cid, list(tilth_rows), list(baseline_rows))
        return [{"deep_diagnosis": self.memo[cid]}] if self.memo[cid] else []

    def argv(self, scratch: Path) -> list[str]:
        return ["codex", "exec", "-m", self.model, "-c", f"model_reasoning_effort={self.effort}", "-s", "read-only",
                "--skip-git-repo-check", "-C", str(scratch), "-o", str(scratch / "answer.txt"), PROMPT]

    def _stage(self, scratch: Path, tilth_rows: list[Mapping], baseline_rows: list[Mapping]) -> list[dict]:
        """Copy dev-split trajectories under neutral names and return the summary rows."""
        arms = (("tilth", tilth_rows), ("baseline", baseline_rows))
        tasks = sorted({row["task"] for _arm, rows in arms for row in rows if row.get("task") in self.dev_tasks})
        summary = []
        for arm, rows in arms:
            for row in rows:
                if row.get("task") not in self.dev_tasks:
                    continue
                index, rep = tasks.index(row["task"]), row.get("repetition", 0)
                source = row.get("trajectory_path")
                if source and Path(source).is_file():
                    shutil.copyfile(source, scratch / f"task-{index}-{arm}-rep{rep}.jsonl")
                calls = row.get("api_calls")
                if calls is None:
                    calls = len(row.get("per_turn_context_tokens") or [])
                summary.append({"task": index, "arm": arm, "rep": rep, "correct": row.get("correct"),
                                "cost": row.get("total_cost_usd"), "api_calls": calls})
        summary.sort(key=lambda item: (item["task"], item["arm"], item["rep"]))
        return summary

    def _run(self, cid: str, tilth_rows: list[Mapping], baseline_rows: list[Mapping]) -> list[dict]:
        tag = cid[:12]
        if self.runs >= self.max_runs:
            self.log(f"deep diagnosis {tag}: skipped, the {self.max_runs}-run maximum is spent")
            return []
        self.runs += 1
        scratch = self.scratch_root / tag
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True)
        started = self.clock()
        try:
            summary = self._stage(scratch, tilth_rows, baseline_rows)
            (scratch / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
            code = self.runner(self.argv(scratch), self.timeout)
            if code != 0:
                raise RuntimeError(f"codex exited {code}")
            causes = parse_causes((scratch / "answer.txt").read_text())
            if causes is None:
                raise ValueError("the answer has no JSON array of causes")
        except subprocess.TimeoutExpired:
            self.log(f"deep diagnosis {tag}: timed out after {self.timeout:g}s; continuing without it")
            return []
        except (OSError, RuntimeError, ValueError) as error:
            self.log(f"deep diagnosis {tag}: failed in {self.clock() - started:.1f}s "
                     f"({type(error).__name__}: {error}); continuing without it")
            return []
        kept = scrub(causes, self.forbidden)
        self.log(f"deep diagnosis {tag}: {len(kept)} of {len(causes)} causes kept, wall {self.clock() - started:.1f}s")
        return kept
