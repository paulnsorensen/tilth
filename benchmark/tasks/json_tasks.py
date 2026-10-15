"""Load benchmark tasks from a JSON file instead of the static `tasks/` package.

Two shapes are supported, detected by whether the JSON has a top-level
`repo` key:

- Mutation shape (top-level `repo`): a local fixture directory with a fixed
  file mutation and a `test_command`, e.g. the `#722` mined-candidate fixture
  shape. The fixture is copied into `REPOS_DIR` and git-bootstrapped there
  (never in the source tree), so `check_task`/`fixtures.reset` (which expect
  a real `.git`) can manage the copy.
- Mined shape (no top-level `repo`): each task self-describes `repo_url`,
  `base_sha`, `head_sha`, and a `test_patch` (the PR's test-file diff), as
  written by `agent_lab_mine.py`. The repository clone is deferred to first
  access via `on_demand_clone`; loading the JSON never touches the network.
  The agent workspace hides `.git` (`hide_git=True`), so the agent cannot
  read `head_sha` or any other post-base history. `test_patch` is applied
  only at grading time (`JsonMinedTask.check_correctness`), never before the
  agent runs, so the agent cannot see or weaken the regression test. Grading
  reads a JUnit XML report that a pytest command writes outside the workdir.
  A mined clone lives under the harness data directory (`$TILTH_BENCH_DATA`,
  see `external.data.data_dir`), never in the checkout or an agent workdir.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import baselines
from config import REPOS, REPOS_DIR, RepoConfig
from external.data import data_dir
from external.featurebench import JUNIT_OPTIONS, junit_results
from external.patches import touched_paths
from external.task import PrepareError, apply_patch, checkout_path, read_report, write_or_remove
from tasks.base import GroundTruth, Mutation, Task, dev_git_env

GRADER_TIMEOUT_SECONDS = 300
DEFAULT_LANGUAGE = "python"
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_URL_RE = re.compile(r"(https://|ssh://|file://|git@[\w.-]+:)[^\s]+")
OUTPUT_TAIL_CHARS = 600


@dataclass
class _JsonTask(Task):
    """Fields and properties shared by both JSON task shapes."""

    id: str
    capability_: str
    prompt_: str
    repo_name: str
    test_command_: list[str]

    @property
    def name(self) -> str:
        return self.id

    @property
    def prompt(self) -> str:
        return self.prompt_

    @property
    def task_type(self) -> str:
        return "edit"

    @property
    def repo(self) -> str:
        return self.repo_name

    @property
    def capability(self) -> str:
        return self.capability_

    @property
    def test_command(self) -> list[str]:
        return self.test_command_


@dataclass
class JsonMutationTask(_JsonTask):
    """A fixed-mutation task loaded from a mutation-shape JSON file."""

    mutations_: list[Mutation]
    ground_truth_: GroundTruth
    fixture_dir: Optional[Path] = None

    @property
    def ground_truth(self) -> GroundTruth:
        return self.ground_truth_

    @property
    def mutations(self) -> list[Mutation]:
        return self.mutations_

    def identity_inputs(self) -> dict[str, object]:
        """The task's own content: prompt, grader, mutations and the fixture repo's file hashes."""
        return {
            "kind": "json-mutation",
            "prompt": self.prompt_,
            "capability": self.capability_,
            "test_command": list(self.test_command_),
            "mutations": [asdict(mutation) for mutation in self.mutations_],
            "forbidden_strings": list(self.ground_truth_.forbidden_strings),
            "fixture_sha256": baselines.tree_sha256(self.fixture_dir) if self.fixture_dir else None,
        }


def _git_blob(repo_dir: Path, revision: str, rel_path: str) -> Optional[bytes]:
    """A path's exact bytes at `revision`, or None if it did not exist there."""
    result = subprocess.run(
        ["git", "cat-file", "blob", f"{revision}:{rel_path}"],
        cwd=repo_dir, capture_output=True,
    )
    return result.stdout if result.returncode == 0 else None


@dataclass
class JsonMinedTask(_JsonTask):
    """A SHA-pinned mined fix task loaded from a mined-shape JSON file."""

    base_sha: str
    head_sha: str
    test_patch: str
    reference_changed_lines_: int
    lint_command_: list[str] = field(default_factory=list)

    @property
    def ground_truth(self) -> GroundTruth:
        return GroundTruth()


    @property
    def reference_changed_lines(self) -> int:
        return self.reference_changed_lines_

    @property
    def lint_command(self) -> list[str]:
        return self.lint_command_

    def identity_inputs(self) -> dict[str, object]:
        """The task's own content: prompt, pins, grader command and the test patch hash."""
        return {
            "kind": "json-mined",
            "prompt": self.prompt_,
            "capability": self.capability_,
            "repo_url": REPOS[self.repo_name].url,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "test_patch_sha256": hashlib.sha256(self.test_patch.encode()).hexdigest(),
            "test_command": list(self.test_command_),
            "lint_command": list(self.lint_command_),
            "reference_changed_lines": self.reference_changed_lines_,
        }

    @property
    def hide_git(self) -> bool:
        # The agent must never see `head_sha`, the reference-fix commit, or
        # any other post-base history: the workspace gets a `.git`-free copy.
        return True

    def check_correctness(self, result_text: str, repo_path: str) -> tuple[bool, str]:
        """Reset `test_patch`'s paths to `base_sha`, apply it, then grade from JUnit.

        Resetting first means an agent that edited or deleted the new test
        cannot win: grading always runs the PR's own regression test against
        whatever fix code the agent left in place, taken from `base_sha` in
        the source clone's git history (not from the `.git`-free workspace).
        """
        workspace = Path(repo_path)
        try:
            self._restore_heldout(workspace)
        except PrepareError as error:
            return False, f"Test patch paths unusable: {error}"
        if not apply_patch(workspace, self.test_patch):
            return False, "Test patch did not apply"

        if not self.test_command_:
            return False, "mined task requires a nonempty test_command"
        command = shlex.join(self.test_command_)
        with tempfile.TemporaryDirectory(prefix="tilth-mined-report-") as report_dir:
            report = Path(report_dir) / "report.xml"
            env = {**os.environ, "PYTEST_ADDOPTS": shlex.join([*JUNIT_OPTIONS, f"--junitxml={report}"])}
            try:
                result = subprocess.run(
                    self.test_command_, cwd=str(workspace), capture_output=True, text=True,
                    timeout=GRADER_TIMEOUT_SECONDS, env=env,
                )
            except subprocess.TimeoutExpired:
                return False, f"Grader timed out after {GRADER_TIMEOUT_SECONDS}s: {command}"
            if result.returncode != 0:
                return False, f"Test failed: {command}{_output_tail(result)}"
            return _junit_verdict(read_report(report), command)

    def _restore_heldout(self, workspace: Path) -> None:
        """Reset every path `test_patch` names to its `base_sha` bytes (or remove it).

        Raises PrepareError for an unsafe path or a patch naming none.
        """
        paths = sorted(_patch_paths(self.test_patch))
        if not paths:
            raise PrepareError("the test patch names no path (nothing is held out)")
        root = workspace.resolve()
        for rel_path in paths:
            if not checkout_path(workspace, rel_path).resolve().is_relative_to(root):
                raise PrepareError(f"path escapes the workspace: {rel_path!r}")
        source_dir = REPOS[self.repo_name].resolved_path()
        for rel_path in paths:
            write_or_remove(workspace, rel_path, _git_blob(source_dir, self.base_sha, rel_path))


_RENAME_HEADER = re.compile(r"^(?:rename|copy) (?:from|to) (.+)$", re.MULTILINE)
_DIFF_GIT_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.MULTILINE)


def _patch_paths(patch: str) -> set[str]:
    """Every path a patch names, including those of a hunk-less pure rename or copy."""
    paths = set(touched_paths(patch))
    paths.update(_RENAME_HEADER.findall(patch))
    for old, new in _DIFF_GIT_HEADER.findall(patch):
        paths.update((old, new))
    return paths


def _output_tail(result: subprocess.CompletedProcess) -> str:
    """A bounded tail of a failed grader's stdout and stderr, ready to append to a reason."""
    tail = f"{result.stdout or ''}{result.stderr or ''}".strip()[-OUTPUT_TAIL_CHARS:]
    return f"\n{tail}" if tail else ""


def _junit_verdict(report: str, command: str) -> tuple[bool, str]:
    """Pass when the report records a PASSED test and no FAILED or ERROR one; skips never count."""
    if not report:
        return False, f"Test report missing (the command wrote no JUnit XML): {command}"
    try:
        ET.fromstring(report)
    except ET.ParseError:
        return False, f"Test report malformed: {command}"
    statuses = [status for status, _ in junit_results(report)]
    broken = sum(status in {"FAILED", "ERROR"} for status in statuses)
    if broken:
        return False, f"Test report shows {broken} failed or errored test(s): {command}"
    if "PASSED" not in statuses:
        return False, f"Test report shows zero executed passing tests (skips do not count): {command}"
    return True, "Test passed"


def _fixture_git(command: list[str], cwd: Path, fixture: Path) -> str:
    """Run a bootstrap git command; a failure names the fixture instead of tracing back."""
    try:
        done = subprocess.run(
            command, cwd=cwd, check=True, capture_output=True, text=True, env=dev_git_env(),
        )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()[-OUTPUT_TAIL_CHARS:]
        raise ValueError(
            f"cannot bootstrap fixture {fixture}: `{' '.join(command[:2])}` failed: {detail}"
        ) from error
    return done.stdout


def _register_mutation_repo(json_path: Path, repo_rel: str, language: str = DEFAULT_LANGUAGE) -> str:
    """Copy the fixture directory named by a mutation-shape `repo` key into
    `REPOS_DIR` and git-bootstrap the copy. Never writes a `.git` into the
    source tree."""
    source_dir = (json_path.parent / repo_rel).resolve()
    digest = hashlib.sha256(
        f"{source_dir}\0{baselines.tree_sha256(source_dir)}".encode()
    ).hexdigest()[:12]
    repo_name = f"json-mutation:{json_path.stem}:{repo_rel}:{digest}"
    repo_dir = REPOS_DIR / repo_name.replace(":", "_").replace(os.sep, "_")
    if not repo_dir.exists():
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(repo_dir.parent)) as staging:
            stage = Path(staging) / "copy"
            shutil.copytree(source_dir, stage, ignore=shutil.ignore_patterns(".git"))
            for command in (
                ["git", "init"],
                ["git", "add", "-A"],
                ["git", "commit", "-m", "chore: bootstrap fixture repo"],
            ):
                _fixture_git(command, stage, source_dir)
            os.rename(str(stage), str(repo_dir))
    head = _fixture_git(["git", "rev-list", "--max-parents=0", "HEAD"], repo_dir, source_dir).split()[0]
    REPOS[repo_name] = RepoConfig(
        name=repo_name,
        url=str(repo_dir),
        commit_sha=head,
        language=language,
        description="json fixture repo",
        path_override=repo_dir,
    )
    return repo_name


def _repo_slug(repo_url: str) -> str:
    """Return a filesystem-friendly repo name from a clone URL."""
    name = repo_url.rstrip("/").rsplit("/", 1)[-1]
    return name[: -len(".git")] if name.endswith(".git") else name


def _register_mined_repo(path: Path, index: int, raw: dict) -> str:
    """Register (without cloning) the on-demand repo for a `(repo_url,
    base_sha)` pair. Naming by repo+base (not task id) means mined tasks that
    share a repo and base commit share one clone."""
    repo_url, base_sha = raw["repo_url"], raw["base_sha"]
    language = raw.get("language", DEFAULT_LANGUAGE)
    repo_name = f"{_repo_slug(repo_url)}-{base_sha[:12]}"
    existing = REPOS.get(repo_name)
    if existing is not None and existing.language != language:
        raise _fail(
            path, index, raw, "language",
            f"mined repo {repo_url}@{base_sha[:12]} is declared as both "
            f"{existing.language!r} and {language!r}; one clone has one language",
        )
    REPOS[repo_name] = RepoConfig(
        name=repo_name,
        url=repo_url,
        commit_sha=base_sha,
        language=language,
        description=f"mined fixture repo for {repo_url}@{base_sha[:12]}",
        on_demand_clone=True,
        path_override=data_dir() / "mined" / repo_name,
    )
    return repo_name


def _fail(path: Path, index: int, raw: object, field_name: str, why: str) -> ValueError:
    task_id = raw.get("id") if isinstance(raw, dict) else None
    who = f"entry {index}" if not isinstance(task_id, str) else f"entry {index} (task {task_id!r})"
    return ValueError(f"{path}: {who}: bad field {field_name!r}: {why}")


def _need_str(path: Path, index: int, raw: dict, key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise _fail(path, index, raw, key, "must be a nonempty string")
    return value


def _opt_language(path: Path, index: int, raw: dict) -> str:
    """The optional `language`, checked against the harness's known languages."""
    value = raw.get("language", DEFAULT_LANGUAGE)
    if value not in baselines.LANGUAGE_TOOLCHAINS:
        known = ", ".join(sorted(baselines.LANGUAGE_TOOLCHAINS))
        raise _fail(path, index, raw, "language", f"must be one of: {known}")
    return value


def _check_sha(path: Path, index: int, raw: dict, key: str) -> None:
    if not _SHA_RE.fullmatch(_need_str(path, index, raw, key)):
        raise _fail(path, index, raw, key, "must be a 40-character lowercase hex SHA")


def _check_repo_url(path: Path, index: int, raw: dict) -> None:
    url = _need_str(path, index, raw, "repo_url")
    if url.startswith("-") or not (_URL_RE.fullmatch(url) or (url.startswith("/") and "\0" not in url)):
        raise _fail(
            path, index, raw, "repo_url",
            "must be an https://, ssh://, file:// or git@host: URL, or an absolute path",
        )


def _opt_str_list(path: Path, index: int, raw: dict, key: str, *, nonempty: bool = False) -> Optional[list[str]]:
    """Return the list at `key` (None when absent), requiring a list of strings."""
    if key not in raw or raw[key] is None:
        if nonempty:
            raise _fail(path, index, raw, key, "is required and must be a nonempty list of strings")
        return None
    value = raw[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _fail(path, index, raw, key, "must be a list of strings")
    if nonempty and not value:
        raise _fail(path, index, raw, key, "must be a nonempty list of strings")
    return value


def _validate_common(path: Path, index: int, raw: object) -> dict:
    if not isinstance(raw, dict):
        raise _fail(path, index, raw, "<entry>", "must be a JSON object")
    _need_str(path, index, raw, "id")
    _need_str(path, index, raw, "prompt")
    if "capability" in raw and not isinstance(raw["capability"], str):
        raise _fail(path, index, raw, "capability", "must be a string")
    _opt_str_list(path, index, raw, "test_command", nonempty=True)
    return raw


def _validate_mutation_entry(path: Path, index: int, raw: object) -> None:
    raw = _validate_common(path, index, raw)
    mutations = raw.get("mutations")
    if not isinstance(mutations, list) or not mutations:
        raise _fail(path, index, raw, "mutations", "is required and must be a nonempty list of objects")
    for position, mutation in enumerate(mutations):
        if not isinstance(mutation, dict) or set(mutation) != {"file_path", "original", "mutated"} or not all(
            isinstance(value, str) for value in mutation.values()
        ):
            raise _fail(
                path, index, raw, f"mutations[{position}]",
                "must be an object of string file_path, original and mutated",
            )
    _opt_str_list(path, index, raw, "forbidden_strings")


def _validate_mined_entry(path: Path, index: int, raw: object) -> None:
    raw = _validate_common(path, index, raw)
    _check_repo_url(path, index, raw)
    _check_sha(path, index, raw, "base_sha")
    _check_sha(path, index, raw, "head_sha")
    _opt_language(path, index, raw)
    if not isinstance(raw.get("test_patch"), str) or not raw["test_patch"].strip():
        raise _fail(path, index, raw, "test_patch", "is required and must be a nonempty string")
    lines = raw.get("reference_changed_lines", 0)
    if isinstance(lines, bool) or not isinstance(lines, int):
        raise _fail(path, index, raw, "reference_changed_lines", "must be an integer")
    _opt_str_list(path, index, raw, "lint_command")
    if not any(Path(token).name in {"pytest", "py.test"} for token in raw["test_command"]):
        raise _fail(
            path, index, raw, "test_command",
            "must invoke pytest (mined grading reads a pytest JUnit report)",
        )


def _common_fields(raw: dict, repo_name: str) -> dict:
    """Constructor fields shared by both JSON task shapes (`raw` already validated)."""
    return {
        "id": raw["id"],
        "capability_": raw.get("capability", "fix"),
        "prompt_": raw["prompt"],
        "repo_name": repo_name,
        "test_command_": list(raw["test_command"]),
    }


def _build_mutation_task(raw: dict, repo_name: str, fixture_dir: Path) -> JsonMutationTask:
    ground_truth = GroundTruth(
        forbidden_strings=raw.get("forbidden_strings", GroundTruth().forbidden_strings),
    )
    return JsonMutationTask(
        **_common_fields(raw, repo_name),
        mutations_=[Mutation(**m) for m in raw["mutations"]],
        ground_truth_=ground_truth,
        fixture_dir=fixture_dir,
    )


def _build_mined_task(path: Path, index: int, raw: dict) -> JsonMinedTask:
    repo_name = _register_mined_repo(path, index, raw)
    return JsonMinedTask(
        **_common_fields(raw, repo_name),
        base_sha=raw["base_sha"],
        head_sha=raw["head_sha"],
        test_patch=raw.get("test_patch", ""),
        reference_changed_lines_=raw.get("reference_changed_lines", 0),
        lint_command_=raw.get("lint_command") or [],
    )


def load_json_tasks(path: Path) -> dict[str, Task]:
    """Load a JSON task file, returning `{task_id: Task}`.

    A top-level `repo` key selects the mutation shape; its absence selects
    the mined shape. Every entry is validated first; a bad file or entry
    raises one `ValueError` naming the path, entry index and field.
    """
    path = Path(path)
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a JSON object")
    raw_tasks = data.get("tasks", [])
    if not isinstance(raw_tasks, list):
        raise ValueError(f"{path}: bad field 'tasks': must be a list")
    mutation_shape = "repo" in data
    if mutation_shape and (not isinstance(data["repo"], str) or not data["repo"]):
        raise ValueError(f"{path}: bad field 'repo': must be a nonempty string")
    language = data.get("language", DEFAULT_LANGUAGE)
    if mutation_shape and language not in baselines.LANGUAGE_TOOLCHAINS:
        known = ", ".join(sorted(baselines.LANGUAGE_TOOLCHAINS))
        raise ValueError(f"{path}: bad field 'language': must be one of: {known}")
    validate = _validate_mutation_entry if mutation_shape else _validate_mined_entry
    seen: set[str] = set()
    for index, raw in enumerate(raw_tasks):
        validate(path, index, raw)
        if raw["id"] in seen:
            raise _fail(path, index, raw, "id", "duplicates an earlier entry in this file")
        seen.add(raw["id"])
    if mutation_shape:
        repo_name = _register_mutation_repo(path, data["repo"], language)
        fixture_dir = (path.parent / data["repo"]).resolve()
        return {raw["id"]: _build_mutation_task(raw, repo_name, fixture_dir) for raw in raw_tasks}
    return {raw["id"]: _build_mined_task(path, index, raw) for index, raw in enumerate(raw_tasks)}


def register_json_tasks(path: Path, registry: dict[str, Task]) -> dict[str, Task]:
    """Load a JSON task file and add its tasks to `registry`.

    Refuses an empty file and any id already in `registry`; returns the loaded tasks.
    """
    loaded = load_json_tasks(path)
    if not loaded:
        raise ValueError(f"{path}: holds no tasks")
    clashes = sorted(task_id for task_id in loaded if task_id in registry)
    if clashes:
        raise ValueError(f"{path}: task id collides with an existing task: {', '.join(clashes)}")
    registry.update(loaded)
    return loaded