"""SWE-bench Multilingual instances at the pinned revision.

The row ``patch`` is the gold fix and ``test_patch`` adds the held-out tests.
Prepare applies neither.
Grading resets the files that ``test_patch`` touches to ``base_commit``.
It applies ``test_patch`` as the row's ``eval_script`` does.
It runs the test command of the script natively.
It matches each FAIL_TO_PASS and PASS_TO_PASS test ID against exactly one parsed verdict.
"""

import fcntl
import json
import os
import re
import shlex
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path

from . import data, javascript, patches, proc
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

# gepa-v2 candidates: Go, Rust, and JavaScript instances whose gold patch changes at least two source files.
# preact-3689 is out: its tests run in Karma, and the harness runs no browser.
GEPA_V2_CANDIDATES = (
    "prometheus__prometheus-9248", "hashicorp__terraform-35543", "sharkdp__bat-1892", "tokio-rs__axum-734",
    "tokio-rs__axum-1119", "tokio-rs__axum-682", "gin-gonic__gin-1957", "tokio-rs__axum-1730",
    "tokio-rs__axum-1934", "caddyserver__caddy-5626", "hashicorp__terraform-34900", "sharkdp__bat-2393",
    "sharkdp__bat-3108", "tokio-rs__axum-2096",
    "astral-sh__ruff-15443", "burntsushi__ripgrep-2576", "caddyserver__caddy-4774", "caddyserver__caddy-6288",
    "prometheus__prometheus-10633", "sharkdp__bat-2201", "sharkdp__bat-2260", "sharkdp__bat-2650",
    "sharkdp__bat-2835", "sharkdp__bat-562", "tokio-rs__axum-691", "tokio-rs__tokio-6551", "tokio-rs__tokio-6838",
    "uutils__coreutils-6377", "uutils__coreutils-6575",
    "prometheus__prometheus-10720", "axios__axios-5316", "mrdoob__three.js-27395", "mrdoob__three.js-26589",
    "vuejs__core-11739", "facebook__docusaurus-9183",
)
PROMPT_INSTRUCTION = (
    "Resolve the following issue by changing the code of the repository in the current checkout. "
    "The issue text follows the line of dashes.\n\n"
    "----------\n"
)


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

    Go verdicts are ``pass``, ``fail``, and ``skip`` events.
    The test2json encoder wraps everything a test prints in an ``output`` event.
    So test output cannot forge a Go verdict.
    Cargo verdicts are text lines.
    A test that writes straight to stdout and then aborts can forge one.
    Libtest has no machine-readable format on stable Rust.
    """
    if language == "go":
        return [(event["Test"], event["Action"] == "pass") for event in _go_events(report)
                if event.get("Action") in _GO_VERDICTS and isinstance(event.get("Test"), str)]
    if language == "javascript":
        return javascript.parse(report)
    if language == "rust":
        return [(match[1], match[2] == "ok") for line in report.splitlines() if (match := _CARGO_RESULT.match(line))]
    return [(node, status in _PYTEST_PASSING) for status, node in junit_results(report)]


def refresh_sources(checkout: Path) -> None:
    """Set the mtime of every file outside ``target/`` to now, so cargo sees each source as changed."""
    for directory, names, files in os.walk(checkout):
        if Path(directory) == checkout:
            names[:] = [name for name in names if name != "target"]
        for name in files:
            try:
                os.utime(Path(directory, name), follow_symlinks=False)
            except OSError:
                pass


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
    def prompt(self) -> str:
        return PROMPT_INSTRUCTION + self.problem_statement

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

    def cargo_seed(self) -> Path | None:
        """A ``target/`` built once from the prepared tree, per (instance, env fingerprint).

        Each Rust grade copies it into its checkout, so dependencies build once.
        Cargo judges freshness by file mtimes against its fingerprints, and those hold paths relative to the workspace.
        So a copied seed would also hide a changed workspace source with an old mtime.
        ``refresh_sources`` makes cargo rebuild the workspace crates after the copy.
        Returns None when the seed build fails. The grade then builds cold.
        A failed build leaves a marker, so later grades skip the retry.
        """
        from .preflight import env_fingerprint  # preflight imports this module

        root = data.data_dir() / "envs" / self.data_rev / self.name / env_fingerprint(self) / "cargo"
        root.mkdir(parents=True, exist_ok=True)
        with open(root / ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if (root / "failed").is_file():
                return None
            if not (root / "ready").is_file():
                with tempfile.TemporaryDirectory(prefix="tilth-external-seed-") as temp:
                    tree = Path(temp) / "tree"
                    self.export_prepared(tree)
                    result = proc.run(["cargo", "test", "--no-run"], cwd=tree, env=self.grade_env(tree),
                                      timeout=self.timeout_s)
                    if result.returncode or not (tree / "target").is_dir():
                        (root / "failed").touch()
                        return None
                    shutil.rmtree(root / "target", ignore_errors=True)
                    shutil.move(tree / "target", root / "target")
                (root / "ready").touch()
        return root / "target"

    def javascript_output(self, checkout: Path, command: str) -> tuple[str, str]:
        """Run the row's test command with its runner pointed at a JSON report; return the log and normalized report."""
        try:
            runner = javascript.runner_of(command)
        except javascript.UnsupportedRunner as error:
            raise PrepareError(str(error)) from error
        self.copy_seed(checkout)
        report = report_path(checkout, "js-report")
        reporter = report_path(checkout, "qunit-reporter.cjs")
        reporter.write_text(javascript.QUNIT_REPORTER)
        command, extra = javascript.rewrite(command, runner, report, reporter)
        env = {**self.grade_env(checkout), **extra}
        result = proc.run(["bash", "-c", command], cwd=checkout, env=env, timeout=self.timeout_s)
        try:
            normalized = javascript.normalize(runner, read_report(report), checkout) if report.is_file() else "[]"
        except (ValueError, KeyError, TypeError, AttributeError):  # a malformed report fails every test
            normalized = "[]"
        return result.stdout + result.stderr, normalized

    def test_output(self, checkout: Path) -> tuple[str, str]:
        if self.language == "javascript":
            return self.javascript_output(checkout, test_command(self.row["eval_script"]))
        env = self.grading_env(checkout) if self.language == "python" else self.grade_env(checkout)
        command = test_command(self.row["eval_script"])
        report = report_path(checkout, "pytest.xml")
        if self.language == "python":
            env["PYTEST_ADDOPTS"] = shlex.join([*JUNIT_OPTIONS, f"--junitxml={report}"])
        elif self.language == "go":
            command = go_json_command(command)
        # The grade env never carries CARGO_TARGET_DIR. Cargo builds in the checkout's own target/, a private
        # copy of the seed. A target directory shared by grades would reuse an earlier build,
        # because exported sources keep the commit's mtime.
        if self.language == "rust" and (seed := self.cargo_seed()):
            shutil.copytree(seed, checkout / "target", symlinks=True)
            refresh_sources(checkout)
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
