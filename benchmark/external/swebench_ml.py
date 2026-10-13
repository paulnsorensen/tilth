"""SWE-bench Multilingual instances at the pinned revision.

The row ``patch`` is the gold fix and ``test_patch`` adds the held-out tests.
Prepare applies neither. Grading resets the files ``test_patch`` touches to
``base_commit``, applies it as the row's ``eval_script`` does, runs the script's
test command natively, and matches each FAIL_TO_PASS and PASS_TO_PASS test ID
against exactly one parsed verdict.
"""

import json
import re
import shlex
from collections.abc import Sequence
from pathlib import Path

from . import data, patches, proc
from .featurebench import JUNIT_OPTIONS, junit_results
from .task import ExternalTask, PrepareError, apply_patch, read_report, read_row, report_path, write_or_remove

# Per-language cherry picks: primary first, then the fallback tried only when it is refused.
PICKS = {
    "go": ("gin-gonic__gin-3741", "prometheus__prometheus-14861"),
    "rust": ("sharkdp__bat-2650", "tokio-rs__tokio-6724"),
}
_START = ": '>>>>> Start Test Output'"
_END = ": '>>>>> End Test Output'"
_GO_VERDICTS = {"pass", "fail", "skip"}
_CARGO_RESULT = re.compile(r"^test (\S+) \.\.\. (ok|FAILED|ignored)")
_PYTEST_PASSING = {"PASSED", "XFAIL", "XPASS"}


def candidates() -> list[str]:
    return [instance_id for picks in PICKS.values() for instance_id in picks]


def test_command(eval_script: str) -> str:
    """The test command between the eval script's start and end markers."""
    lines = eval_script.splitlines()
    try:
        start = lines.index(_START)
        end = lines.index(_END, start)
    except ValueError as error:
        raise PrepareError("eval_script has no test-output markers") from error
    return "\n".join(lines[start + 1:end])


def go_json_command(command: str) -> str:
    """``command`` with every ``go test`` run as ``go test -json``, so verdicts arrive as test2json events."""
    return command if "-json" in command else re.sub(r"\bgo test\b", "go test -json", command)


def _go_events(output: str) -> list[dict]:
    events = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def go_log(output: str) -> str:
    """The text a ``go test -json`` stream carries, for failure messages."""
    return "".join(str(event.get("Output", "")) for event in _go_events(output) if event.get("Action") == "output")


def test_results(report: str, language: str) -> list[tuple[str, bool]]:
    """``(test ID, passed)`` for every verdict in a ``go test -json`` stream, ``cargo test`` output, or JUnit XML.

    Go verdicts are ``pass``, ``fail``, and ``skip`` events. The test2json encoder wraps
    everything a test prints in an ``output`` event, so test output cannot forge one.
    Cargo verdicts are text lines. A test that writes straight to the process's stdout
    and then aborts can forge one: libtest has no machine-readable format on stable Rust.
    """
    if language == "go":
        return [(event["Test"], event["Action"] == "pass") for event in _go_events(report)
                if event.get("Action") in _GO_VERDICTS and isinstance(event.get("Test"), str)]
    if language == "rust":
        return [(match[1], match[2] == "ok") for line in report.splitlines() if (match := _CARGO_RESULT.match(line))]
    return [(node, status in _PYTEST_PASSING) for status, node in junit_results(report)]


def entry_verdicts(report: str, entries: Sequence[str], language: str) -> dict[str, bool]:
    """An entry passes only when exactly one parsed verdict has its exact ID and that verdict passed."""
    results = test_results(report, language)
    verdicts = {}
    for entry in entries:
        matches = [passed for test_id, passed in results if test_id == entry]
        verdicts[entry] = matches == [True]
    return verdicts


class SweBenchTask(ExternalTask):
    dataset = data.SWEBENCH_ML

    @property
    def gold_patch(self) -> str:
        return self.row["patch"]

    def install_steps(self) -> list[str]:
        # The rows carry no install recipe; grading reads a pytest JUnit report, so the venv needs pytest.
        steps = ["pip install pytest"]
        if any(self.base_file(name) is not None for name in ("pyproject.toml", "setup.py", "setup.cfg")):
            steps.append("pip install -e .")
        return steps

    def restore_heldout(self, checkout: Path) -> None:
        test_patch = self.row["test_patch"]
        for relative in sorted(patches.touched_paths(test_patch)):
            write_or_remove(checkout, relative, self.base_file(relative))
        if not apply_patch(checkout, test_patch):
            raise PrepareError("test_patch does not apply to the restored test files")

    def test_output(self, checkout: Path) -> tuple[str, str]:
        env = self.grading_env(checkout) if self.language == "python" else self.grade_env(checkout)
        command = test_command(self.row["eval_script"])
        report = report_path(checkout, "pytest.xml")
        if self.language == "python":
            env["PYTEST_ADDOPTS"] = shlex.join([*JUNIT_OPTIONS, f"--junitxml={report}"])
        elif self.language == "go":
            command = go_json_command(command)
        # The grade env never carries CARGO_TARGET_DIR, so Cargo builds in the fresh checkout's own target/:
        # a shared target directory would reuse an earlier grade's build, because exported sources keep the
        # commit's mtime.
        result = proc.run(["bash", "-c", command], cwd=checkout, env=env, timeout=self.timeout_s)
        if self.language == "python":
            return result.stdout + result.stderr, read_report(report)
        if self.language == "go":
            return go_log(result.stdout) + result.stderr, result.stdout
        return result.stdout + result.stderr, result.stdout + result.stderr

    def entry_results(self, report: str) -> dict[str, bool]:
        return entry_verdicts(report, [*self.fail_to_pass, *self.pass_to_pass], self.language)


def load(instance_id: str, data_rev: str) -> SweBenchTask:
    """The SWE-bench Multilingual task for a row cached at the pinned revision; raises for any other revision."""
    return SweBenchTask(read_row(data.SWEBENCH_ML, data_rev, instance_id), data_rev)
