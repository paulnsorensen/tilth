"""ExternalTask: one pinned external benchmark instance, prepared and graded natively (no containers).

A prepared workdir is the instance's ``base_commit`` tree, exported from the bare
upstream mirror and transformed by the dataset (FeatureBench applies its mask),
committed as the only commit of a fresh repository. Grading copies the agent's
changes onto a freshly exported prepared tree, restores the held-out tests and
(for Python) the pytest configuration from the mirror, and runs them. Python
tests run in a grading venv cached under the harness data directory, never in
the agent-writable ``<workdir>/.venv``.
"""

import fcntl
import filecmp
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
from abc import abstractmethod
from collections.abc import Iterable, Mapping
from functools import cached_property
from pathlib import Path, PurePosixPath

from tasks.base import GroundTruth, Task, TaskSource

from . import data, proc

# The external package; every .py file in it keys an external task's run.
PACKAGE_DIR = Path(__file__).resolve().parent
# Paths the prepared workdir excludes from git; grading never copies them.
EXCLUDED_PATHS = (".venv/", "target/")
_SKIPPED_TOP = {".git", ".venv", "target"}
_SKIPPED_ANYWHERE = {"__pycache__", ".pytest_cache"}
_PREPARED_COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "tilth-bench",
    "GIT_AUTHOR_EMAIL": "tilth-bench@localhost",
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+0000",
    "GIT_COMMITTER_NAME": "tilth-bench",
    "GIT_COMMITTER_EMAIL": "tilth-bench@localhost",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+0000",
}
_ENV_STEP_TIMEOUT_S = 1800
_CONTAINER_TOOLS = {"docker", "podman"}
_SHELL_SEGMENTS = re.compile(r"\n|;|&&|\|\||\||\(|\)|`|\$\(")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_COMMAND_PREFIXES = {"sudo", "exec", "command", "env", "nohup", "time"}
_PYTHON_TIMEOUT_S = 1200
_COMPILED_TIMEOUT_S = 1800
# pytest reads these; grading resets every one to base_commit so the agent cannot change outcomes.
_PYTEST_CONFIG_NAMES = frozenset({
    "conftest.py", "pytest.ini", ".pytest.ini", "pytest.toml", ".pytest.toml", "tox.ini", "setup.cfg",
})


class PrepareError(RuntimeError):
    """The prepared tree could not be built from the mirror and the row's patches."""


class EnvBuildError(RuntimeError):
    """The native environment (venv, Go modules, or crates) could not be built."""


def _git_env(cwd: Path | None) -> dict[str, str]:
    env = {**os.environ, **_PREPARED_COMMIT_ENV}
    if cwd is not None:
        # Keep git from discovering an enclosing repository above an export directory.
        env["GIT_CEILING_DIRECTORIES"] = str(Path(cwd).resolve().parent)
    return env


def _git(cwd: Path, *args: str) -> None:
    result = proc.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
                      cwd=cwd, env=_git_env(cwd))
    if result.returncode:
        raise PrepareError(f"git {' '.join(args)} failed: {result.stderr.strip()[-500:]}")


def apply_patch(cwd: Path, patch: str) -> bool:
    """Apply ``patch`` forward in ``cwd``; False when it does not apply."""
    result = proc.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=cwd, env=_git_env(cwd), input=patch)
    return result.returncode == 0


def _tree_files(root: Path) -> set[str]:
    """Relative paths of the files (and symlinks) under ``root`` that grading considers."""
    files = set()
    for current, directories, names in os.walk(root):
        relative = Path(current).relative_to(root)
        for directory in list(directories):
            skipped = directory in _SKIPPED_ANYWHERE or (relative == Path(".") and directory in _SKIPPED_TOP)
            if skipped or (Path(current) / directory).is_symlink():
                directories.remove(directory)
                if not skipped:
                    files.add((relative / directory).as_posix())
        files.update((relative / name).as_posix() for name in names)
    return files


def _copy_changes(workdir: Path, checkout: Path) -> None:
    """Make ``checkout`` hold the agent's tree: copy changed and new files, drop deleted ones."""
    agent, clean = _tree_files(workdir), _tree_files(checkout)
    for relative in sorted(agent):
        source, target = workdir / relative, checkout / relative
        unchanged = (relative in clean and not source.is_symlink() and not target.is_symlink()
                     and filecmp.cmp(source, target, shallow=False))
        if unchanged:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or target.exists():
            target.unlink()
        shutil.copy2(source, target, follow_symlinks=False)
    for relative in clean - agent:
        (checkout / relative).unlink()


def checkout_path(root: Path, relative: str) -> Path:
    """``root / relative`` for a row-supplied path, never outside ``root``.

    Raises PrepareError for an absolute path or a ``..`` component. A symlink at
    a directory component (one the agent left) is removed, so a write through
    the returned path cannot follow it out of ``root``.
    """
    parts = PurePosixPath(relative).parts
    if not parts or relative.startswith("/") or ".." in parts:
        raise PrepareError(f"unsafe path in the row: {relative!r}")
    current = root
    for part in parts[:-1]:
        current = current / part
        if current.is_symlink():
            current.unlink()
    return root.joinpath(*parts)


def write_or_remove(root: Path, relative: str, content: bytes | None) -> None:
    """Set ``root / relative`` to ``content``, or remove it when ``content`` is None."""
    target = checkout_path(root, relative)
    if target.is_symlink() or target.is_file():
        target.unlink()
    if content is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def package_digest() -> str:
    """SHA-256 over every .py file in the external package, keyed by sorted relative path."""
    digest = hashlib.sha256()
    for relative in sorted(path.relative_to(PACKAGE_DIR).as_posix() for path in PACKAGE_DIR.rglob("*.py")):
        digest.update(f"{relative}\0{hashlib.sha256((PACKAGE_DIR / relative).read_bytes()).hexdigest()}\n".encode())
    return digest.hexdigest()


def read_row(dataset: str, data_rev: str, instance_id: str) -> dict:
    """The cached row of ``instance_id`` at the dataset's pinned revision."""
    pinned = data.REVISIONS[dataset]
    if data_rev != pinned:
        raise ValueError(f"{dataset} is pinned at revision {pinned}, not {data_rev}")
    path = data.row_path(dataset, data_rev, instance_id)
    if not path.is_file():
        raise LookupError(f"{instance_id} has no cached {dataset} row; run benchmark/external/preflight.py")
    return json.loads(path.read_text())


class ExternalTask(Task):
    """An external benchmark instance; subclasses supply the mask, held-out restore, and matcher."""

    dataset: str
    transformation = "none"

    def __init__(self, row: Mapping, data_rev: str) -> None:
        self.row = dict(row)
        self.data_rev = data_rev
        self.language = data.language_of(row)
        if self.language not in data.LANGUAGES:
            raise ValueError(f"{self.name}: unsupported language {self.language!r}")
        self._details = self._count({})

    # --- Task surface ---

    @property
    def name(self) -> str:
        return self.row["instance_id"]

    @property
    def prompt(self) -> str:
        return self.problem_statement

    @property
    def problem_statement(self) -> str:
        return self.row["problem_statement"]

    @property
    def ground_truth(self) -> GroundTruth:
        return GroundTruth(required_strings=[], forbidden_strings=[])

    @property
    def task_type(self) -> str:
        return "edit"

    @property
    def capability(self) -> str:
        return "fix"

    @property
    def repo(self) -> str:
        return self.dataset

    @property
    def source(self) -> TaskSource:
        return TaskSource(origin=data.upstream_url(self.upstream_repo), license="upstream",
                          commit_or_tag=self.base_commit, transformation=self.transformation)

    @property
    def timeout_s(self) -> int:
        return _PYTHON_TIMEOUT_S if self.language == "python" else _COMPILED_TIMEOUT_S

    # --- instance data ---

    @property
    def upstream_repo(self) -> str:
        return self.row["repo"]

    @property
    def base_commit(self) -> str:
        return self.row["base_commit"]

    @property
    def fail_to_pass(self) -> tuple[str, ...]:
        return tuple(self.row["FAIL_TO_PASS"])

    @property
    def pass_to_pass(self) -> tuple[str, ...]:
        return tuple(self.row["PASS_TO_PASS"])

    @property
    @abstractmethod
    def gold_patch(self) -> str:
        """The diff that adds the fix to the prepared tree."""

    @property
    def python_version(self) -> str | None:
        """The interpreter version the venv requests, when the row names one."""
        return None

    def identity_inputs(self) -> dict[str, object]:
        return {
            "gold_patch": self.gold_patch,
            "test_patch": self.row["test_patch"],
            "FAIL_TO_PASS": list(self.fail_to_pass),
            "PASS_TO_PASS": list(self.pass_to_pass),
            "base_commit": self.base_commit,
            "data_revision": self.data_rev,
            # Grading helpers outside the task's class files (patches, preflight, proc, ...).
            "external_package": package_digest(),
        }

    # --- the mirror ---

    @property
    def mirror(self) -> Path:
        return data.mirror_path(self.upstream_repo)

    @cached_property
    def _mirror_holds_base(self) -> bool:
        check = proc.run(["git", f"--git-dir={self.mirror}", "cat-file", "-e", f"{self.base_commit}^{{commit}}"])
        return check.returncode == 0

    def base_file(self, relative: str) -> bytes | None:
        """A file's content at ``base_commit``, or None when the base tree lacks it."""
        if not self._mirror_holds_base:
            raise PrepareError(f"mirror {self.mirror} lacks {self.base_commit}; run benchmark/external/preflight.py")
        shown = proc.run(["git", f"--git-dir={self.mirror}", "show", f"{self.base_commit}:{relative}"], text=False)
        return shown.stdout if shown.returncode == 0 else None

    @cached_property
    def base_paths(self) -> frozenset[str]:
        """Every file path in the ``base_commit`` tree."""
        if not self._mirror_holds_base:
            raise PrepareError(f"mirror {self.mirror} lacks {self.base_commit}; run benchmark/external/preflight.py")
        listed = proc.run(["git", f"--git-dir={self.mirror}", "ls-tree", "-r", "-z", "--name-only", self.base_commit])
        if listed.returncode:
            raise PrepareError(f"git ls-tree {self.base_commit} failed: {listed.stderr.strip()[-500:]}")
        return frozenset(path for path in listed.stdout.split("\0") if path)

    def base_file_hashes(self, names: Iterable[str]) -> dict[str, str]:
        """SHA-256 of each named file present in the ``base_commit`` tree."""
        hashes = {}
        for name in names:
            content = self.base_file(name)
            if content is not None:
                hashes[name] = hashlib.sha256(content).hexdigest()
        return hashes

    def declared_package(self) -> str | None:
        """A package name the row itself declares; datasets override."""
        return None

    @cached_property
    def package_name(self) -> str:
        """The package, module path, or crate the upstream publishes."""
        if declared := self.declared_package():
            return declared
        manifests = {"python": ("pyproject.toml", ("project", "name")),
                     "rust": ("Cargo.toml", ("package", "name"))}
        try:
            if self.language == "go":
                go_mod = (self.base_file("go.mod") or b"").decode()
                for line in go_mod.splitlines():
                    if line.startswith("module "):
                        return line.split()[1]
            elif manifest := self.base_file(manifests[self.language][0]):
                value = tomllib.loads(manifest.decode())
                for key in manifests[self.language][1]:
                    value = value[key]
                return str(value)
        except (PrepareError, tomllib.TOMLDecodeError, KeyError, TypeError, UnicodeDecodeError):
            pass
        return self.upstream_repo.rsplit("/", 1)[-1]

    # --- prepare ---

    def apply_mask(self, tree: Path) -> None:
        """Transform the exported base tree into what the agent sees; datasets override."""

    def export_prepared(self, dest: Path) -> None:
        """Write the prepared tree (base tree plus mask) to ``dest`` without git metadata."""
        archive = proc.run(["git", f"--git-dir={self.mirror}", "archive", "--format=tar", self.base_commit],
                           text=False)
        if archive.returncode:
            raise PrepareError(f"git archive {self.base_commit} failed: {archive.stderr.decode()[-500:]}")
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tree:
            try:
                tree.extractall(dest, filter="data")
            except tarfile.TarError as error:
                raise PrepareError(f"git archive {self.base_commit} does not extract safely: {error}") from error
        self.apply_mask(dest)

    def prepare_tree(self, workdir: Path) -> None:
        """Export the prepared tree into ``workdir`` as the single commit of a fresh repository."""
        self.export_prepared(workdir)
        _git(workdir, "init", "-q")
        (workdir / ".git" / "info").mkdir(parents=True, exist_ok=True)
        (workdir / ".git" / "info" / "exclude").write_text("".join(f"{path}\n" for path in EXCLUDED_PATHS))
        _git(workdir, "add", "-A")
        _git(workdir, "commit", "-q", "--no-verify", "-m", "Prepared workspace")

    def install_steps(self) -> list[str]:
        """Python install commands, run in order inside the workdir venv."""
        return []

    def build_env(self, workdir: Path, venv: Path | None = None) -> None:
        """Build the native environment: a uv venv (``<workdir>/.venv`` by default), or the host Go or Rust
        toolchain's dependencies. Steps run under ``proc.toolchain_env`` with a throwaway HOME."""
        venv = venv or workdir / ".venv"
        if self.language == "go":
            steps = [["go", "mod", "download"]]
        elif self.language == "rust":
            steps = [["cargo", "fetch"]]
        else:
            version = ["--python", self.python_version] if self.python_version else []
            steps = [["uv", "venv", *version, str(venv)]]
            steps += [_install_argv(step, venv) for step in self.install_steps()]
        with tempfile.TemporaryDirectory(prefix="tilth-external-home-") as home:
            env = proc.toolchain_env(Path(home))
            env.update(VIRTUAL_ENV=str(venv), PATH=f"{venv / 'bin'}{os.pathsep}{env.get('PATH', '')}")
            for argv in steps:
                if _command_names(argv) & _CONTAINER_TOOLS:
                    raise EnvBuildError(f"{shlex.join(argv)}: container steps are not run (native environments only)")
                try:
                    result = proc.run(argv, cwd=workdir, env=env, timeout=_ENV_STEP_TIMEOUT_S)
                except (OSError, subprocess.TimeoutExpired) as error:
                    raise EnvBuildError(f"{shlex.join(argv)}: {error}") from error
                if result.returncode:
                    raise EnvBuildError(f"{shlex.join(argv)} exited {result.returncode}: "
                                        f"{(result.stderr or result.stdout).strip()[-800:]}")

    def prepare(self, workdir: Path) -> None:
        """The run.py hook: build the agent's workdir and its native environment."""
        self.prepare_tree(workdir)
        self.build_env(workdir)

    def grade_venv(self) -> Path:
        """The Python grading venv, built once per (instance, env fingerprint) under the harness data directory.

        It is built from the prepared tree with the row's install steps, outside
        every agent workdir, and grading only reads it.
        """
        from .preflight import env_fingerprint  # preflight imports this module

        root = data.data_dir() / "envs" / self.data_rev / self.name / env_fingerprint(self)
        venv = root / ".venv"
        root.mkdir(parents=True, exist_ok=True)
        with open(root / ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not (root / "ready").is_file():
                for stale in (root / "tree", venv):
                    shutil.rmtree(stale, ignore_errors=True)
                self.export_prepared(root / "tree")
                self.build_env(root / "tree", venv)
                (root / "ready").touch()
        return venv

    # --- grading ---

    @abstractmethod
    def restore_heldout(self, checkout: Path) -> None:
        """Put the held-out tests into the grading checkout, overriding any agent edit."""

    def restore_test_config(self, checkout: Path) -> None:
        """Reset every pytest configuration file to ``base_commit``, removing ones the agent added.

        That is each ``conftest.py`` and pytest-reading ini file, and each
        ``pyproject.toml`` with a ``[tool.pytest`` table in either version.
        """
        for relative in sorted(_tree_files(checkout) | self.base_paths):
            name = PurePosixPath(relative).name
            if name not in _PYTEST_CONFIG_NAMES and name != "pyproject.toml":
                continue
            base = self.base_file(relative)
            if name == "pyproject.toml":
                target = checkout_path(checkout, relative)
                current = target.read_bytes() if target.is_file() else b""
                if b"[tool.pytest" not in current + (base or b""):
                    continue
            write_or_remove(checkout, relative, base)

    @abstractmethod
    def test_output(self, checkout: Path) -> str:
        """Run the held-out tests natively in ``checkout`` and return their output."""

    @abstractmethod
    def entry_results(self, output: str) -> dict[str, bool]:
        """Map every FAIL_TO_PASS and PASS_TO_PASS entry to whether it passed."""

    def grade_env(self, checkout: Path) -> dict[str, str]:
        """``proc.toolchain_env`` with a fresh HOME beside ``checkout``, for running the held-out tests."""
        home = checkout.with_name(f"{checkout.name}-home")
        home.mkdir(exist_ok=True)
        return proc.toolchain_env(home)

    def grading_env(self, checkout: Path) -> dict[str, str]:
        """The grading venv's environment, importing the project from ``checkout`` before the venv's install."""
        venv = self.grade_venv()
        env = self.grade_env(checkout)
        sources = [str(path) for path in (checkout, checkout / "src") if path.is_dir()]
        env.update(VIRTUAL_ENV=str(venv), PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=os.pathsep.join(sources),
                   PATH=f"{venv / 'bin'}{os.pathsep}{env.get('PATH', '')}")
        return env

    def _count(self, results: Mapping[str, bool]) -> dict[str, float | int]:
        f2p = sum(bool(results.get(entry)) for entry in self.fail_to_pass)
        p2p = sum(bool(results.get(entry)) for entry in self.pass_to_pass)
        total = len(self.fail_to_pass) + len(self.pass_to_pass)
        return {"pass_rate": (f2p + p2p) / total if total else 0.0,
                "f2p_passed": f2p, "f2p_total": len(self.fail_to_pass),
                "p2p_passed": p2p, "p2p_total": len(self.pass_to_pass)}

    def check_correctness(self, result_text: str, repo_path: str) -> tuple[bool, str]:
        """Grade the agent's workdir against the held-out FAIL_TO_PASS and PASS_TO_PASS entries."""
        workdir = Path(repo_path)
        self._details = self._count({})
        with tempfile.TemporaryDirectory(prefix="tilth-external-grade-") as temp:
            checkout = Path(temp) / "checkout"
            try:
                self.export_prepared(checkout)
                _copy_changes(workdir, checkout)
                if self.language == "python":
                    self.restore_test_config(checkout)
                self.restore_heldout(checkout)
            except (PrepareError, OSError) as error:
                return False, f"Grading checkout failed: {error}"
            try:
                output = self.test_output(checkout)
            except subprocess.TimeoutExpired:
                return False, f"Held-out tests timed out after {self.timeout_s}s"
            except (PrepareError, EnvBuildError, OSError) as error:
                return False, f"Held-out tests could not run: {error}"
        results = self.entry_results(output)
        self._details = self._count(results)
        details = self._details
        summary = (f"F2P {details['f2p_passed']}/{details['f2p_total']}, "
                   f"P2P {details['p2p_passed']}/{details['p2p_total']}")
        failed = [entry for entry in (*self.fail_to_pass, *self.pass_to_pass) if not results.get(entry)]
        if not failed and details["f2p_total"] + details["p2p_total"]:
            return True, f"Held-out tests pass ({summary})"
        return False, f"Held-out tests failed ({summary}): {', '.join(failed[:10])}\n{output[-1500:]}"

    def grade_details(self) -> dict[str, float | int]:
        """Entry counts from the last ``check_correctness``; no test source or output."""
        return dict(self._details)


def _command_names(argv: list[str]) -> set[str]:
    """The programs ``argv`` starts: its first word, or each command of a ``bash -c`` script."""
    segments = _SHELL_SEGMENTS.split(argv[2]) if argv[:2] == ["bash", "-c"] else [shlex.join(argv)]
    names = set()
    for segment in segments:
        words = [word.strip("'\"") for word in segment.split()]
        while words and _ASSIGNMENT.match(words[0]):
            words.pop(0)
        if words and words[0] in _COMMAND_PREFIXES:
            # A prefix (env, sudo, ...) runs one of its arguments, after options that may take values.
            names.update(Path(word).name for word in words)
        elif words:
            names.add(Path(words[0]).name)
    return names


def _needs_shell(step: str) -> bool:
    """True when ``step`` uses an unquoted shell operator, an expansion, or several lines."""
    if any(character in step for character in "$`\n"):
        return True
    lexer = shlex.shlex(step, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return any(set(token) <= set("();<>|&") for token in lexer)
    except ValueError:
        return True


def _install_argv(step: str, venv: Path) -> list[str]:
    """Run a row's install step against the workdir venv, with pip through uv.

    A line with shell operators runs as one script line under ``set -e``, as the
    dataset's own setup script runs it: a command that fails inside an ``&&``
    chain stops the chain without failing the build.
    """
    if _needs_shell(step):
        script = "\n".join(["set -e", 'pip() { uv pip "$@"; }', 'pip3() { uv pip "$@"; }', step, ":"])
        return ["bash", "-c", script]
    tokens = shlex.split(step)
    if tokens[:1] in (["pip"], ["pip3"]):
        return ["uv", "pip", *tokens[1:]]
    if tokens[:3] in (["python", "-m", "pip"], ["python3", "-m", "pip"]):
        return ["uv", "pip", *tokens[3:]]
    if tokens[:1] in (["python"], ["python3"]):
        return [str(venv / "bin" / "python"), *tokens[1:]]
    return ["bash", "-c", step]
