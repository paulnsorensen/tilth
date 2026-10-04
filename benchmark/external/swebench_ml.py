"""SWE-bench Multilingual instances at the pinned revision.

The row ``patch`` is the gold fix and ``test_patch`` adds the held-out tests.
Prepare applies neither. Grading resets the files ``test_patch`` touches to
``base_commit``, applies it as the row's ``eval_script`` does, runs the script's
test command natively, and matches each FAIL_TO_PASS and PASS_TO_PASS test ID
against exactly one parsed verdict.
"""

import os
import re
from collections.abc import Sequence
from pathlib import Path

from . import data, patches, proc
from .featurebench import pytest_results
from .task import ExternalTask, PrepareError, apply_patch, read_row, write_or_remove

# Per-language cherry picks: primary first, then the fallback tried only when it is refused.
PICKS = {
    "go": ("gin-gonic__gin-3741", "prometheus__prometheus-14861"),
    "rust": ("sharkdp__bat-2650", "tokio-rs__tokio-6724"),
}
_START = ": '>>>>> Start Test Output'"
_END = ": '>>>>> End Test Output'"
_GO_RESULT = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)")
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


def test_results(output: str, language: str) -> list[tuple[str, bool]]:
    """``(test ID, passed)`` for every verdict in ``go test -v``, ``cargo test``, or ``pytest -rA`` output."""
    if language == "go":
        return [(match[2], match[1] == "PASS") for line in output.splitlines() if (match := _GO_RESULT.match(line))]
    if language == "rust":
        return [(match[1], match[2] == "ok") for line in output.splitlines() if (match := _CARGO_RESULT.match(line))]
    return [(node, status in _PYTEST_PASSING) for status, node in pytest_results(output)]


def entry_verdicts(output: str, entries: Sequence[str], language: str) -> dict[str, bool]:
    """An entry passes only when exactly one parsed verdict has its exact ID and that verdict passed."""
    results = test_results(output, language)
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
        # The rows carry no install recipe; grading parses pytest -rA, so the venv needs pytest.
        steps = ["pip install pytest"]
        if any(self.base_file(name) is not None for name in ("pyproject.toml", "setup.py", "setup.cfg")):
            steps.append("pip install -e .")
        return steps

    def restore_heldout(self, checkout: Path) -> None:
        test_patch = self.row["test_patch"]
        for relative in sorted(patches.touched_paths(test_patch)):
            write_or_remove(checkout / relative, self.base_file(relative))
        if not apply_patch(checkout, test_patch):
            raise PrepareError("test_patch does not apply to the restored test files")

    def test_output(self, checkout: Path) -> str:
        env = self.grading_env(checkout) if self.language == "python" else dict(os.environ)
        # Cargo builds in the fresh checkout's own target/: a shared target directory would
        # reuse an earlier grade's build, because exported sources keep the commit's mtime.
        env.pop("CARGO_TARGET_DIR", None)
        result = proc.run(["bash", "-c", test_command(self.row["eval_script"])], cwd=checkout, env=env,
                          timeout=self.timeout_s)
        return result.stdout + result.stderr

    def entry_results(self, output: str) -> dict[str, bool]:
        return entry_verdicts(output, [*self.fail_to_pass, *self.pass_to_pass], self.language)


def load(instance_id: str, data_rev: str) -> SweBenchTask:
    """The SWE-bench Multilingual task for a row cached at the pinned revision; raises for any other revision."""
    return SweBenchTask(read_row(data.SWEBENCH_ML, data_rev, instance_id), data_rev)
