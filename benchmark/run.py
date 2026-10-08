#!/usr/bin/env python3
"""
Benchmark runner for tilth performance evaluation.

Executes the selected agent CLI for each task, mode, model, and repetition.
Records token usage, cost, correctness, and tool usage to JSONL format.
"""

import argparse
import functools
import hashlib
import inspect
import json
import math
import os
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple, Optional

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

import baselines
import external
import external.contamination
import external.preflight
import panels
from spend import SpendLedger
from claude_bash_guard import allowed_command
from config import (
    BENCHMARK_DIR,
    DEFAULT_MAX_BUDGET_USD,
    DEFAULT_REPS,
    FIXTURES_DIR,
    MODELS,
    MODES,
    OPENCODE_CONFIG_HOME,
    OPENCODE_CONFIGS,
    REPO_ROOT,
    REPOS,
    RESULTS_DIR,
    RUNNERS,
    SYNTHETIC_REPO,
    SYSTEM_PROMPT,
    TILTH_BIN,
    ModeConfig,
)
from fixtures.reset import ensure_repo_clean, reset_repo
from parse import (
    detect_quota_rejection,
    extract_stream_error,
    extract_trajectory,
    parse_codex_json,
    parse_opencode_json,
    parse_stream_json,
    stream_native_cost,
    tool_batch_sizes,
    tool_call_counts,
    tool_op_kinds,
)
from tasks import TASKS
from variants import (
    experiment_modes,
    hydrate_mode_metadata,
    load_experiment,
    randomized_arm_order,
    rustc_version,
)


def _tilth_version(binary_path: Optional[str] = None) -> Optional[str]:
    """Get a tilth binary's reported version."""
    try:
        result = subprocess.run(
            [binary_path or TILTH_BIN, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip().removeprefix("tilth ") if result.returncode == 0 else None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _variant_metadata(
    mode: ModeConfig,
    *,
    reported_version: Optional[str] = None,
) -> dict:
    return {
        "label": mode.name,
        "plugin_dir": mode.plugin_dir,
        "plugin_version": mode.plugin_version,
        "plugin_git_sha": mode.plugin_git_sha,
        "repository": mode.repository,
        "git_ref": mode.git_ref,
        "git_sha": mode.git_sha,
        "binary_path": mode.binary_path,
        "binary_sha256": mode.binary_sha256,
        "tilth_version": reported_version or mode.tilth_version,
        "rustc_version": mode.rustc_version,
    }


def _reported_tilth_version(mode: ModeConfig) -> Optional[str]:
    return mode.tilth_version or (_tilth_version(mode.binary_path) if mode.binary_path else None)


def wozcode_mode(plugin_dir: Path) -> ModeConfig:
    plugin_dir = plugin_dir.expanduser().resolve()
    manifest_path = plugin_dir / ".claude-plugin" / "plugin.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read Woz plugin manifest {manifest_path}: {error}") from error
    version = manifest.get("version") if isinstance(manifest, dict) else None
    if not isinstance(version, str) or not version:
        raise ValueError(f"Woz plugin manifest has no version: {manifest_path}")
    server = plugin_dir / "servers" / "code-server.cjs"
    if not server.is_file():
        raise ValueError(f"Woz MCP server not found: {server}")
    try:
        revision = subprocess.run(
            ["git", "-C", str(plugin_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        plugin_git_sha = None
    else:
        plugin_git_sha = revision.stdout.strip() if revision.returncode == 0 else None
    return ModeConfig(
        name="wozcode", tools=list(MODES["baseline"].tools),
        mcp_config_path=str(manifest_path), description="Built-ins + Woz Code plugin",
        plugin_dir=str(plugin_dir), plugin_version=version,
        plugin_git_sha=plugin_git_sha,
    )


def claude_mcp_config(mode: ModeConfig) -> dict:
    if mode.plugin_dir:
        plugin_dir = Path(mode.plugin_dir)
        return {"mcpServers": {"plugin_woz_code": {
            "command": "node",
            "args": ["--no-warnings=ExperimentalWarning", str(plugin_dir / "servers" / "code-server.cjs")],
            "env": {
                "WOZCODE_MCP_CWD_HOOK_INJECTED": "1",
                "WOZCODE_BUNDLE_VERSION": mode.plugin_version,
                "CLAUDE_PLUGIN_ROOT": str(plugin_dir),
            },
        }}}
    if not mode.mcp_config_path:
        return {"mcpServers": {}}
    with open(mode.mcp_config_path) as config_file:
        config = json.load(config_file)
    if mode.binary_path and "tilth" in config.get("mcpServers", {}):
        config["mcpServers"]["tilth"]["command"] = mode.binary_path
    return config


def get_repo_path(repo_name: str) -> Path:
    """Resolve working directory for a task's repo."""
    if repo_name == "synthetic":
        return SYNTHETIC_REPO
    return REPOS[repo_name].path


_RUNTIME_ENV_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TERM",
        "LANG",
        "LANGUAGE",
        "XDG_DATA_HOME",
        "GOCACHE",
    }
)
_PROVIDER_AUTH_PREFIXES = ("ANTHROPIC_", "OPENAI_", "OPENROUTER_")
_PROVIDER_AUTH_KEYS = frozenset({"CODEX_API_KEY"})


_DEFAULT_TILTH_BIN = object()


class ClaudeAuthError(RuntimeError):
    """A Claude subprocess would bill an API key instead of the subscription."""


# API-billing credentials: under ``claude -p`` either overrides
# ``CLAUDE_CODE_OAUTH_TOKEN`` and bills the API instead of the subscription.
_API_BILLING_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def guard_claude_auth(env: Mapping[str, str]) -> None:
    """Refuse a Claude call whose environment carries an API-billing credential.

    Judge and proposer callers reuse this.
    """
    for key in _API_BILLING_KEYS:
        if env.get(key):
            raise ClaudeAuthError(
                f"{key} is set; it overrides CLAUDE_CODE_OAUTH_TOKEN and bills "
                "the API instead of the subscription. Unset it before running Claude cells."
            )


def build_runner_env(
    runner: str,
    *,
    opencode_config: Optional[str] = None,
    bare: bool = False,
    ambient: Optional[Mapping[str, str]] = None,
    tilth_bin: object = _DEFAULT_TILTH_BIN,
) -> dict[str, str]:
    """Build a minimal environment for one runner subprocess."""
    source = os.environ if ambient is None else ambient
    if runner == "claude":
        guard_claude_auth(source)
    env = {
        key: value
        for key, value in source.items()
        if key in _RUNTIME_ENV_KEYS
        or key in _PROVIDER_AUTH_KEYS
        or (runner == "claude" and key in {"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN"})
        or key.startswith(_PROVIDER_AUTH_PREFIXES)
        or key.startswith("LC_")
    }
    # No runner bills Anthropic through the API, and an empty ANTHROPIC_* value
    # can still switch a client's auth path, so neither is forwarded.
    for key in [key for key, value in env.items()
                if key in _API_BILLING_KEYS or (key.startswith("ANTHROPIC_") and not value)]:
        del env[key]
    # Pin the agent CLI for the whole run: a mid-run update changes the run key.
    env["DISABLE_AUTOUPDATER"] = "1"

    selected_tilth_bin = TILTH_BIN if tilth_bin is _DEFAULT_TILTH_BIN else tilth_bin
    if isinstance(selected_tilth_bin, str):
        tilth_dir = os.path.dirname(selected_tilth_bin)
        if tilth_dir:
            env["PATH"] = tilth_dir + os.pathsep + env.get("PATH", "")

    if runner == "codex" and "CODEX_HOME" in source:
        env["CODEX_HOME"] = source["CODEX_HOME"]

    if runner == "opencode" and opencode_config is not None:
        OPENCODE_CONFIG_HOME.mkdir(parents=True, exist_ok=True)
        env["OPENCODE_CONFIG"] = opencode_config
        env["XDG_CONFIG_HOME"] = str(OPENCODE_CONFIG_HOME)
        if bare:
            env["OPENCODE_DISABLE_DEFAULT_PLUGINS"] = "1"
            env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
            env["OPENCODE_DISABLE_CLAUDE_CODE"] = "1"
            env["OPENCODE_DISABLE_EXTERNAL_SKILLS"] = "1"

    return env


def _codex_skill_roots(repo_path: Path, env: Mapping[str, str]) -> list[Path]:
    home = Path(env.get("HOME", str(Path.home())))
    codex_home = Path(env.get("CODEX_HOME", str(home / ".codex")))
    candidates = [home / ".agents" / "skills", codex_home / "skills", Path("/etc/codex/skills")]
    candidates.extend(parent / ".agents" / "skills" for parent in (repo_path, *repo_path.parents))
    roots: set[Path] = set()
    for path in candidates:
        try:
            _ = path.stat()
        except FileNotFoundError:
            continue
        if path.is_dir():
            roots.add(path.resolve())
    return sorted(roots)


def _codex_skill_paths(repo_path: Path, env: Mapping[str, str]) -> list[str]:
    paths: set[str] = set()
    visited: set[Path] = set()

    def fail(error: OSError) -> None:
        raise error

    for root in _codex_skill_roots(repo_path, env):
        for current, dirs, files in os.walk(root, topdown=True, followlinks=True, onerror=fail):
            canonical = Path(current).resolve()
            if canonical in visited:
                dirs.clear()
                continue
            visited.add(canonical)
            if "SKILL.md" in files:
                paths.add(str((canonical / "SKILL.md").resolve()))
    return sorted(paths)


class InvalidCodexCellError(RuntimeError):
    """A Codex cell broke benchmark isolation or did not use required MCP."""


def _validate_codex_stream(
    raw: str, require_tilth: bool, skill_paths: list[str], skill_roots: list[str],
) -> int:
    successful = 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("type") not in {"item.started", "item.completed"}:
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"command_execution", "mcp_tool_call", "file_change", "file_edit", "file_write"}:
            tool_input = str({key: item.get(key) for key in ("command", "arguments", "changes", "file_path")})
            aliases = (".agents/skills/", ".codex/skills/", "$CODEX_HOME/skills/")
            if any(path in tool_input for path in (*skill_paths, *skill_roots, *aliases)):
                raise InvalidCodexCellError("Codex attempted host skill access")
        result = item.get("result")
        if (event.get("type") == "item.completed" and item.get("type") == "mcp_tool_call"
                and item.get("server") == "tilth" and item.get("status") == "completed"
                and item.get("error") is None and isinstance(result, dict)
                and result.get("isError") is not True and result.get("is_error") is not True):
            successful += 1
    if require_tilth and successful == 0:
        raise InvalidCodexCellError("Codex tilth arm made no successful tilth MCP call")
    return successful


def _compact_tool_sequence(result):
    """Extract ordered tool call names + key args from all turns."""
    seq = []
    for turn in result.turns:
        for tc in turn.tool_calls:
            entry = {"name": tc.name}
            # Add compact args summary
            args = {}
            for k, v in tc.input.items():
                if k == "command":
                    args[k] = str(v)[:80]
                elif k == "file_path":
                    args[k] = str(v).split("/")[-1]  # filename only
                elif k in ("pattern", "query", "path", "scope", "kind", "section", "expand"):
                    args[k] = str(v)[:60]
                elif k in ("paths", "sections", "patterns") and isinstance(v, list):
                    # Batch-capable read/glob args — file or segment counts.
                    args[f"{k}_count"] = len(v)
                elif k == "files" and isinstance(v, list):
                    # tilth_edit: count files in the batch AND total hunks across files.
                    args["files_count"] = len(v)
                    args["edits_count"] = sum(
                        len(f.get("edits", [])) for f in v if isinstance(f, dict)
                    )
                # skip other large args
            if args:
                entry["args"] = args
            seq.append(entry)
    return seq


@contextmanager
def _agent_repo(repo_path: Path, hide_git: bool):
    """Yield a disposable workspace for one benchmark cell."""
    with tempfile.TemporaryDirectory(prefix="tilth-benchmark-") as temp_dir:
        workspace = Path(temp_dir) / repo_path.name
        if not hide_git and (repo_path / ".git").exists():
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_path),
                    "worktree",
                    "add",
                    "--detach",
                    str(workspace),
                    "HEAD",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            try:
                yield workspace
            finally:
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repo_path),
                        "worktree",
                        "remove",
                        "--force",
                        str(workspace),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            return

        shutil.copytree(
            repo_path,
            workspace,
            ignore=shutil.ignore_patterns(".git", ".git_hidden")
            if hide_git
            else shutil.ignore_patterns(".git_hidden"),
        )
        yield workspace


@contextmanager
def _prepared_repo(task: object):
    """Yield a fresh workdir that the task's ``prepare`` hook builds."""
    with tempfile.TemporaryDirectory(prefix="tilth-benchmark-") as temp_dir:
        workspace = Path(temp_dir) / "repo"
        task.prepare(workspace)
        yield workspace


class McpUnavailableError(RuntimeError):
    """A mode expected an MCP server that the session did not expose."""


def _copy_private(source: Path, destination: Path) -> None:
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
    )
    temp_path = Path(temp_name)
    try:
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        shutil.copyfile(source, temp_path)
        os.replace(temp_path, destination)
    finally:
        temp_path.unlink(missing_ok=True)


@contextmanager
def _cell_claude_config(model_name: str, mode: ModeConfig):
    if RUNNERS[model_name] != "claude":
        yield None
        return
    seed = os.environ.get("CLAUDE_CONFIG_DIR")
    if not seed:
        if mode.plugin_dir:
            raise ValueError("Woz Code requires an explicit CLAUDE_CONFIG_DIR auth seed")
        yield None
        return
    seed_auth = Path(seed) / "wozcode" / "auth.json"
    if mode.plugin_dir and not seed_auth.is_file():
        raise ValueError(f"Woz Code auth seed not found: {seed_auth}")
    with tempfile.TemporaryDirectory(prefix="tilth-claude-config-") as temp_dir:
        config_dir = Path(temp_dir)
        cell_auth = config_dir / "wozcode" / "auth.json"
        if mode.plugin_dir:
            cell_auth.parent.mkdir()
            _copy_private(seed_auth, cell_auth)
        try:
            yield config_dir
        finally:
            if mode.plugin_dir and cell_auth.is_file():
                _copy_private(cell_auth, seed_auth)

_STRICT_NATIVE_FILE_TOOLS = frozenset({"Read", "Edit", "Write", "MultiEdit", "Grep", "Glob", "NotebookEdit", "LS"})
_STRICT_BASELINE_TOOLS = ("Read", "Edit", "Write", "Grep", "Glob", "Bash")
_STRICT_SYSTEM_GUIDANCE = (
    "Use only the available file tools to read or edit source files. "
    "Use Bash only for go test, go build, go vet, and gofmt -w on local Go files. "
    "Do not use Bash to inspect files or run shell wrappers. Do not delegate tasks."
)


_BASH_GUARD = Path(__file__).with_name("claude_bash_guard.py")


# The hashed command template names the hook by placeholder: the interpreter and
# checkout paths are host details, and the guard's content is keyed by its hash.
_STRICT_HOOK_PLACEHOLDER = "<python> <bash-guard>"


def _strict_bash_settings(hook_command: str | None = None) -> str:
    if hook_command is None:
        hook_command = f"{shlex.quote(str(Path(sys.executable).resolve()))} {shlex.quote(str(_BASH_GUARD.resolve()))}"
    return json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": hook_command},
    ]}]}})


def _audit_strict_claude(raw: str, mode: ModeConfig, available_tools: list[str]) -> int:
    available = set(available_tools)
    if mode.mcp_config_path:
        if "Bash" not in available or available & (_STRICT_NATIVE_FILE_TOOLS | {"Agent", "Task"}):
            raise RuntimeError("strict MCP arm exposed forbidden native tools or no Bash")
    elif not set(_STRICT_BASELINE_TOOLS) <= available or available & {"Agent", "Task"}:
        raise RuntimeError("strict baseline did not expose its file tools or exposed delegation")
    if not mode.mcp_config_path and any(name.startswith("mcp__") for name in available):
        raise RuntimeError("strict baseline exposed an MCP tool")

    calls = {}
    denied = set()
    for line in raw.splitlines():
        event = json.loads(line)
        message = event.get("message", {})
        for block in message.get("content", []):
            if block.get("type") == "tool_use":
                name = block.get("name", "")
                if name in {"Agent", "Task"} or (mode.mcp_config_path and name in _STRICT_NATIVE_FILE_TOOLS):
                    raise RuntimeError(f"strict cell attempted forbidden native tool: {name}")
                if name == "Bash":
                    calls[block.get("id")] = block.get("input", {}).get("command", "")
            elif block.get("type") == "tool_result" and block.get("is_error") is True:
                denied.add(block.get("tool_use_id"))
    for call_id, command in calls.items():
        if not allowed_command(command) and call_id not in denied:
            raise RuntimeError(f"strict cell used forbidden Bash command: {command}")
    return sum(not allowed_command(command) for command in calls.values())


CELL_TIMEOUT_S = 600
_TOOLCHAIN_PROBES = {
    "rustc": ("rustc", "--version"),
    "cargo": ("cargo", "--version"),
    "go": ("go", "version"),
    "node": ("node", "--version"),
    "python3": ("python3", "--version"),
    "uv": ("uv", "--version"),
}
# The toolchains a repo's language builds and tests with; a rustc bump must not
# re-key a Go, JavaScript, or Python task.
_LANGUAGE_TOOLCHAINS = {
    "rust": ("rustc", "cargo"),
    "go": ("go",),
    "javascript": ("node",),
    "python": ("python3", "uv"),
}
# The synthetic fixture repo is a Python project (fixtures/template/pyproject.toml).
_SYNTHETIC_LANGUAGE = "python"
# A repo without a lockfile (express) pins its dependencies through these instead.
_UNLOCKED_DEPENDENCY_FILES = ("package.json", "node_modules/.package-lock.json")
_LOCKFILES = (
    "Cargo.lock", "go.sum", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "poetry.lock", "uv.lock", "Pipfile.lock", "requirements.txt",
)


def _run_probe(argv: tuple[str, ...]) -> str | None:
    """Return a tool's version line, or None when it is unavailable."""
    try:
        probe = subprocess.run(list(argv), capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = (probe.stdout or probe.stderr).strip().splitlines()
    return lines[0] if probe.returncode == 0 and lines else None


# Probed once per run when planning cell identities.
_probe_version = functools.lru_cache(maxsize=None)(_run_probe)


def cli_version(runner: str, *, fresh: bool = False) -> str | None:
    """Return the agent CLI version that a runner subprocess will use.

    ``fresh`` bypasses the per-run cache; the scheduler re-probes before each paid
    cell so a mid-run CLI update cannot mix versions under one planned key.
    """
    return (_run_probe if fresh else _probe_version)((runner, "--version"))


def _file_sha256(path: Path) -> str:
    with open(path, "rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _is_prepared(task: object) -> bool:
    """A task with a ``prepare`` hook builds its own workdir instead of copying a REPOS fixture."""
    return callable(getattr(task, "prepare", None))


def _task_timeout(task: object) -> int:
    return getattr(task, "timeout_s", CELL_TIMEOUT_S)


def env_fingerprint(task_or_repo: object) -> str:
    """Fingerprint the task language's toolchains and its dependency lockfiles.

    A task exposing ``language`` supplies the language; a prepared task supplies
    the lockfiles of its base tree, so neither indexes ``REPOS``. A bare repo
    name resolves both through ``REPOS``.
    """
    task = None if isinstance(task_or_repo, str) else task_or_repo
    repo_name = task_or_repo if task is None else task.repo
    language = getattr(task, "language", None) or (
        _SYNTHETIC_LANGUAGE if repo_name == "synthetic" else REPOS[repo_name].language
    )
    if _is_prepared(task):
        base_file_hashes = getattr(task, "base_file_hashes", lambda _names: {})
        lockfiles = base_file_hashes(_LOCKFILES) or base_file_hashes(_UNLOCKED_DEPENDENCY_FILES)
    else:
        repo_path = get_repo_path(repo_name)
        lockfiles = {
            name: _file_sha256(repo_path / name)
            for name in _LOCKFILES if (repo_path / name).is_file()
        } or {
            name: _file_sha256(repo_path / name)
            for name in _UNLOCKED_DEPENDENCY_FILES if (repo_path / name).is_file()
        }
    return baselines.env_fingerprint(
        toolchains={name: _probe_version(_TOOLCHAIN_PROBES[name])
                    for name in _LANGUAGE_TOOLCHAINS.get(language, ())},
        lockfile_hash=hashlib.sha256(json.dumps(lockfiles, sort_keys=True).encode()).hexdigest(),
    )


def _is_tilth_arm(mode: ModeConfig) -> bool:
    return mode.mcp_config_path is not None and mode.plugin_dir is None


def is_stock_arm(mode: ModeConfig) -> bool:
    """A stock (baseline) arm attaches no MCP server or plugin, whatever its name.

    Legacy runs call it ``baseline``; experiment manifests call it ``no_tilth``.
    """
    return mode.mcp_config_path is None and mode.plugin_dir is None


def _mcp_shape(mode: ModeConfig, runner: str, mode_name: str) -> dict:
    """Describe the MCP servers a runner attaches, minus binary paths the key hashes separately."""
    if runner == "opencode":
        config_path = mode.opencode_config_path or OPENCODE_CONFIGS.get(mode_name)
        if config_path is None:
            return {}
        try:
            return json.loads(Path(config_path).read_text())
        except FileNotFoundError:
            return {"opencode_config": config_path}
    if not mode.mcp_config_path:
        return {}
    if runner == "codex":
        return {"tilth": {"args": ["--mcp", "--edit"], "required": True}}
    servers = claude_mcp_config(mode).get("mcpServers", {})
    return {
        name: {key: value for key, value in server.items() if key != "command"}
        for name, server in servers.items()
    }


def _task_fixture_files(task: object) -> dict[str, str]:
    """Hash the fixture files a task grades or runs against.

    A task module's fixtures sit beside it as `<stem>_fixtures` or, for a
    `*_tasks` module, `<name>_fixtures` (as `gin_render_context_fixtures` does).
    """
    roots = []
    module_file = inspect.getsourcefile(type(task))
    if module_file:
        module_path = Path(module_file)
        stems = {module_path.stem, module_path.stem.removesuffix("_tasks")}
        roots.extend(module_path.with_name(f"{stem}_fixtures") for stem in sorted(stems))
    if getattr(task, "repo", None) == "synthetic":
        roots.append(FIXTURES_DIR / "template")
    return {
        str(path.relative_to(root.parent)): _file_sha256(path)
        for root in roots if root.is_dir()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


_LIBRARY_PATHS = tuple(
    Path(path).resolve() for key in ("stdlib", "platstdlib", "purelib", "platlib")
    if (path := sysconfig.get_paths().get(key))
)


def _task_source_files(task: object) -> dict[str, str]:
    """Hash the source files that define the task class and its bases.

    These hold the grader (`check_correctness`, `required_matches`, module
    helpers), so a grading edit changes the task digest. Keys are relative to the
    benchmark directory, or a bare file name outside it, so they match across hosts.
    """
    sources = {}
    for cls in type(task).__mro__:
        try:
            source_file = inspect.getsourcefile(cls)
        except TypeError:
            continue
        if not source_file:
            continue
        path = Path(source_file).resolve()
        if any(path.is_relative_to(library) for library in _LIBRARY_PATHS):
            continue
        key = str(path.relative_to(BENCHMARK_DIR)) if path.is_relative_to(BENCHMARK_DIR) else path.name
        sources[key] = _file_sha256(path)
    return sources


def _cell_task_digest(task: object) -> str:
    ground_truth = getattr(task, "ground_truth", None)
    mutations = getattr(task, "mutations", ()) or ()
    identity_inputs = getattr(task, "identity_inputs", None)
    return baselines.task_digest(
        prompt=task.prompt,
        ground_truth=asdict(ground_truth) if is_dataclass(ground_truth) else ground_truth,
        test_command=list(getattr(task, "test_command", ()) or ()),
        fixture_files=_task_fixture_files(task),
        repo_commit=REPOS[task.repo].commit_sha if task.repo in REPOS else None,
        mutations=[asdict(mutation) if is_dataclass(mutation) else mutation for mutation in mutations],
        hide_git=bool(getattr(task, "hide_git", False)),
        task_sources=_task_source_files(task),
        identity_inputs=identity_inputs() if callable(identity_inputs) else None,
    )


_REPO_PLACEHOLDER = Path("<repo>")
_TILTH_BIN_PLACEHOLDER = "<tilth-bin>"


def _command_template(
    task: object,
    mode: ModeConfig,
    mode_name: str,
    model_name: str,
    *,
    bare: bool,
    reasoning_effort: str | None,
    max_budget_usd: float,
    strict_file_tools: bool,
) -> list[str]:
    """Return the runner argv a cell will run, with its per-cell paths normalized.

    The disposable workspace path, the host skill paths codex disables, the
    tilth binary path (keyed by its SHA-256), the strict Bash hook's interpreter
    and guard paths (the guard keyed by its SHA-256), and the task prompt (keyed
    by the task digest) become placeholders, so the template changes only when
    the runner configuration does.
    """
    if _is_tilth_arm(mode):
        mode = replace(mode, binary_path=_TILTH_BIN_PLACEHOLDER)
    template_task = SimpleNamespace(prompt="<prompt>", hide_git=bool(getattr(task, "hide_git", False)))
    try:
        cmd, _ = _runner_command(
            template_task, mode, mode_name, model_name, _REPO_PLACEHOLDER,
            bare=bare, reasoning_effort=reasoning_effort, max_budget_usd=max_budget_usd,
            strict_file_tools=strict_file_tools, skill_paths=[],
            strict_hook_command=_STRICT_HOOK_PLACEHOLDER,
        )
    except (ValueError, RuntimeError) as error:
        # The cell fails when it runs; key it by the reason it cannot be built.
        return [f"unbuildable: {error}"]
    return cmd


def cell_identity(
    task_name: str,
    mode_name: str,
    model_name: str,
    repetition: int,
    *,
    bare: bool,
    reasoning_effort: str | None,
    max_budget_usd: float,
    strict_file_tools: bool,
) -> dict:
    """Return the run key, the key-input fields, and the harness variant flags one cell's row records."""
    task = TASKS[task_name]
    mode = MODES[mode_name]
    runner = RUNNERS[model_name]
    if strict_file_tools:
        tools = list(_STRICT_BASELINE_TOOLS) if not mode.mcp_config_path else ["Bash"]
    else:
        tools = list(mode.tools)
    identity = {
        "harness_digest": baselines.harness_digest(
            system_prompt=SYSTEM_PROMPT + ("\n" + _STRICT_SYSTEM_GUIDANCE if strict_file_tools else ""),
            tools=tools,
            strict_file_tools=strict_file_tools,
            bare=bare,
            max_budget_usd=max_budget_usd if runner == "claude" else None,
            mcp_shape=_mcp_shape(mode, runner, mode_name),
            command=_command_template(
                task, mode, mode_name, model_name, bare=bare, reasoning_effort=reasoning_effort,
                max_budget_usd=max_budget_usd, strict_file_tools=strict_file_tools,
            ),
            bash_guard_sha256=_file_sha256(_BASH_GUARD) if strict_file_tools else None,
        ),
        "task_digest": _cell_task_digest(task),
        "env_fingerprint": env_fingerprint(task),
        "cli_version": cli_version(runner),
        "timeout_s": _task_timeout(task),
        **({"git_sha": mode.git_sha, "binary_sha256": mode.binary_sha256} if _is_tilth_arm(mode) else {}),
    }
    key_inputs = {
        **identity, "task": task_name, "model": MODELS[model_name], "mode": mode_name,
        "repetition": repetition, "reasoning_effort": reasoning_effort,
    }
    # The harness digest already keys these; rows record them so a baseline
    # variant is its own frozen slot (baselines.BASELINE_SLOT_FIELDS).
    variant_flags = {
        "bare": bare, "strict_file_tools": strict_file_tools,
        "max_budget_usd": max_budget_usd if runner == "claude" else None,
    }
    return {"run_key": baselines.run_key(key_inputs), **identity, **variant_flags}


def write_trajectory(stream_log_path: Path | None, runner: str) -> str | None:
    """Write the tool-call sidecar beside a teed claude or codex stream."""
    if runner not in {"claude", "codex"} or stream_log_path is None or not stream_log_path.is_file():
        return None
    calls = extract_trajectory(stream_log_path.read_text(), runner)
    sidecar = stream_log_path.with_name(f"{stream_log_path.stem}.trajectory.jsonl")
    sidecar.write_text("".join(json.dumps(call) + "\n" for call in calls))
    return str(sidecar)


def contamination_fields(trajectory_path: str | None, task: object) -> dict:
    """The ``contaminated`` flag and its hits for a row's trajectory sidecar."""
    hits = external.contamination.find_hits(trajectory_path, task)
    return {"contaminated": bool(hits), "contamination_hits": hits}


class QuotaExhaustedError(RuntimeError):
    """A subscription usage limit rejected the cell; later cells would fail too."""


def run_single(
    task_name: str,
    mode_name: str,
    model_name: str,
    repetition: int,
    verbose: bool = False,
    stream_log_path: Optional[Path] = None,
    bare: bool = False,
    reasoning_effort: str | None = None,
    max_budget_usd: float = DEFAULT_MAX_BUDGET_USD,
    strict_file_tools: bool = False,
) -> dict:
    """Run one benchmark cell in the task's configured agent workspace."""
    task = TASKS[task_name]
    workspace = (
        _prepared_repo(task) if _is_prepared(task)
        else _agent_repo(get_repo_path(task.repo), getattr(task, "hide_git", False))
    )
    with workspace as repo_path, _cell_claude_config(model_name, MODES[mode_name]) as config_dir:
        mutations = getattr(task, "mutations", ())
        if mutations and not _is_prepared(task):
            task.apply_mutations(str(repo_path))
        return _run_single_in_repo(
            task_name,
            mode_name,
            model_name,
            repetition,
            repo_path,
            verbose=verbose,
            stream_log_path=stream_log_path,
            bare=bare,
            reasoning_effort=reasoning_effort,
            max_budget_usd=max_budget_usd,
            strict_file_tools=strict_file_tools,
            claude_config_dir=config_dir,
        )


_CODEX_GUIDANCE = (
    "Do not discover, read, or invoke host skills or external agent guidance. "
    "Batch independent source reads in one call when the tool supports it."
)
_CODEX_TILTH_GUIDANCE = (
    " Use tilth MCP first for source discovery, reads, and writes. "
    "Batch independent files into one tilth call. "
    "Use shell only for tests and builds. Native tools remain available."
)


def _runner_command(
    task: object,
    mode: ModeConfig,
    mode_name: str,
    model_name: str,
    repo_path: Path,
    *,
    bare: bool,
    reasoning_effort: str | None,
    max_budget_usd: float,
    strict_file_tools: bool,
    skill_paths: list[str],
    strict_hook_command: str | None = None,
) -> tuple[list[str], Optional[str]]:
    """Build one cell's runner argv and, for opencode, the config it selects."""
    model_id = MODELS[model_name]
    runner = RUNNERS[model_name]
    opencode_config: Optional[str] = None
    if runner == "codex":
        if mode_name == "tilth_forced":
            raise ValueError("Codex cannot enforce tilth_forced tool restrictions")
        cmd = [
            "codex", "exec", "--json", "--ephemeral",
            "--ignore-user-config", "--ignore-rules",
            "--sandbox", "workspace-write",
            "--enable", "skip_host_skill_discovery",
            "--disable", "skill_search", "--disable", "plugins", "--disable", "hooks",
            "--disable", "apps", "--disable", "multi_agent",
            "--disable", "skill_mcp_dependency_install",
            "-m", model_id,
            "-c", "mcp_servers={}",
            "-c", "project_doc_max_bytes=0",
            "-c", f'projects.{json.dumps(str(repo_path))}.trust_level="untrusted"',
        ]
        skills_config = ",".join(
            f'{{path={json.dumps(path)},enabled=false}}' for path in skill_paths
        )
        cmd += ["-c", f"skills.config=[{skills_config}]"]
        instructions = f"{SYSTEM_PROMPT}\nYour current working directory is: {repo_path}\n{_CODEX_GUIDANCE}"
        if mode.mcp_config_path:
            instructions += _CODEX_TILTH_GUIDANCE
        cmd += ["-c", f"developer_instructions={json.dumps(instructions)}"]
        if reasoning_effort is not None:
            cmd += ["-c", f"model_reasoning_effort={json.dumps(reasoning_effort)}"]
        if getattr(task, "hide_git", False):
            cmd.append("--skip-git-repo-check")

        if mode.mcp_config_path:
            tilth_bin = mode.binary_path or TILTH_BIN
            cmd += [
                "-c", f"mcp_servers.tilth.command={json.dumps(str(tilth_bin))}",
                "-c", 'mcp_servers.tilth.args=["--mcp", "--edit"]',
                "-c", "mcp_servers.tilth.required=true",
                "-c", 'mcp_servers.tilth.tools.tilth_write.approval_mode="approve"',
            ]

        cmd += ["--", task.prompt]

    elif runner == "opencode":
        opencode_config = mode.opencode_config_path or OPENCODE_CONFIGS.get(mode_name)
        if opencode_config is None:
            raise RuntimeError(
                f"opencode runner has no config for mode '{mode_name}'. "
                f"Supported modes: {', '.join(sorted(OPENCODE_CONFIGS))}."
            )
        # opencode has no --system-prompt; prepend like codex. OPENCODE_CONFIG
        # (set below) selects the MCP servers; --dangerously-skip-permissions
        # keeps the headless run from blocking on tool-permission prompts.
        full_prompt = f"{SYSTEM_PROMPT}\n\n{task.prompt}"
        cmd = [
            "opencode", "run",
            "--format", "json",
            "--dir", str(repo_path),
            "--model", model_id,
            "--dangerously-skip-permissions",
        ]
        if bare:
            cmd.append("--pure")  # strip external plugins (claude --bare parity)
        cmd.append(full_prompt)

    else:  # claude
        cmd = [
            "claude", "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--model", model_id,
            "--max-budget-usd", str(max_budget_usd),
            "--disable-slash-commands",
            "--disallowedTools", ",".join(("Agent", "Task", *sorted(_STRICT_NATIVE_FILE_TOOLS))) if strict_file_tools and mode.mcp_config_path else "Agent,Task",
            "--no-session-persistence",
            "--dangerously-skip-permissions",
            "--strict-mcp-config",
            "--system-prompt", SYSTEM_PROMPT + f"\nYour current working directory is: {repo_path}" + ("\n" + _STRICT_SYSTEM_GUIDANCE if strict_file_tools else ""),
        ]

        # --setting-sources "" loads no user/project/local settings (hooks, env,
        # user MCP servers) while retaining OAuth/keychain auth and honoring
        # --mcp-config. --safe-mode is documented to disable ALL MCP servers,
        # including --mcp-config ones, which silently ran tilth arms
        # native-only; --bare refuses OAuth and needs ANTHROPIC_API_KEY.
        if bare or mode.plugin_dir:
            cmd += ["--setting-sources", ""]
        if reasoning_effort is not None:
            cmd += ["--effort", reasoning_effort]
        if mode.plugin_dir:
            cmd += ["--plugin-dir", mode.plugin_dir, "--agent", "woz:code"]

        if strict_file_tools:
            cmd += ["--settings", _strict_bash_settings(strict_hook_command)]
        tools_list = (list(_STRICT_BASELINE_TOOLS) if not mode.mcp_config_path else ["Bash"]) if strict_file_tools else list(mode.tools)

        # --tools "" disables all built-ins (tilth_forced); --tools "a,b,c" allowlists; absent = default
        if tools_list:
            cmd += ["--tools", ",".join(tools_list)]
        elif mode.mcp_config_path:
            cmd += ["--tools", ""]

        if mode.mcp_config_path:
            cmd += ["--mcp-config", json.dumps(claude_mcp_config(mode))]

        cmd += ["--", task.prompt]

    return cmd, opencode_config


def _run_single_in_repo(
    task_name: str,
    mode_name: str,
    model_name: str,
    repetition: int,
    repo_path: Path,
    verbose: bool = False,
    stream_log_path: Optional[Path] = None,
    bare: bool = False,
    reasoning_effort: str | None = None,
    max_budget_usd: float = DEFAULT_MAX_BUDGET_USD,
    strict_file_tools: bool = False,
    claude_config_dir: Path | None = None,
) -> dict:
    """Execute and grade one benchmark cell."""
    task = TASKS[task_name]
    mode = MODES[mode_name]
    model_id = MODELS[model_name]
    runner = RUNNERS[model_name]
    timeout_s = _task_timeout(task)
    skill_paths: list[str] = []
    skill_roots: list[str] = []
    identity = cell_identity(
        task_name, mode_name, model_name, repetition,
        bare=bare, reasoning_effort=reasoning_effort,
        max_budget_usd=max_budget_usd, strict_file_tools=strict_file_tools,
    )

    if runner == "codex":
        skill_paths = _codex_skill_paths(repo_path, os.environ)
        skill_roots = [str(path) for path in _codex_skill_roots(repo_path, os.environ)]
    cmd, opencode_config = _runner_command(
        task, mode, mode_name, model_name, repo_path,
        bare=bare, reasoning_effort=reasoning_effort, max_budget_usd=max_budget_usd,
        strict_file_tools=strict_file_tools, skill_paths=skill_paths,
    )

    if verbose:
        print(f"    Running: {' '.join(cmd)}")

    # Build a fresh allowlist for every runner subprocess. Runner-specific
    # config is added only by the lane that consumes it.
    env = build_runner_env(
        runner,
        opencode_config=opencode_config,
        bare=bare,
        tilth_bin=mode.binary_path,
    )
    if claude_config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = str(claude_config_dir)
    start_time = time.time()

    if runner in {"claude", "codex"} and stream_log_path is not None:
        # Tee JSONL stdout to disk while preserving it for parsing.
        stream_log_path.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(
            cmd,
            cwd=str(repo_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,  # line-buffered
            env=env,
        )
        assert proc.stdout is not None and proc.stderr is not None
        stderr_pipe = proc.stderr
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        timed_out = False

        def _drain_stderr() -> None:
            stderr_chunks.append(stderr_pipe.read())

        def _kill_on_timeout() -> None:
            nonlocal timed_out
            timed_out = True
            proc.kill()

        stderr_thread = threading.Thread(target=_drain_stderr)
        stderr_thread.start()
        timer = threading.Timer(timeout_s, _kill_on_timeout)
        timer.start()
        try:
            with open(stream_log_path, "w") as logf:
                for line in proc.stdout:
                    logf.write(line)
                    logf.flush()
                    stdout_chunks.append(line)
            proc.wait()
            stderr_thread.join()
        finally:
            timer.cancel()
            proc.stdout.close()
            stderr_pipe.close()

        stderr_text = "".join(stderr_chunks)
        if runner == "codex":
            stream_log_path.with_suffix(".stderr").write_text(stderr_text)
        if timed_out:
            raise subprocess.TimeoutExpired(cmd, timeout_s)

        result = subprocess.CompletedProcess(
            args=cmd,
            returncode=proc.returncode,
            stdout="".join(stdout_chunks),
            stderr=stderr_text,
        )
    else:
        result = subprocess.run(
            cmd,
            cwd=str(repo_path),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
        )
    elapsed_ms = int((time.time() - start_time) * 1000)
    trajectory_path = write_trajectory(stream_log_path, runner)

    # A usage limit surfaces as a rejected rate_limit_event or a limit result.
    if runner == "claude" and (quota := detect_quota_rejection(result.stdout)):
        raise QuotaExhaustedError(f"claude -p hit a usage limit: {quota}")

    if result.returncode != 0:
        runner_name = {"codex": "codex exec", "opencode": "opencode run"}.get(runner, "claude -p")
        # The real cause (e.g. a provider ContentFilterError) streams as a late
        # {"type":"error",...} event; a head-truncated dump hides it. Surface it.
        detail = extract_stream_error(result.stdout) or f"stdout tail: {result.stdout[-600:]}"
        raise RuntimeError(
            f"{runner_name} failed with code {result.returncode}\n"
            f"stderr: {result.stderr}\n"
            f"{detail}"
        )

    if runner == "claude":
        final_events = [event for line in result.stdout.splitlines()
                        if (event := json.loads(line)).get("type") == "result"]
        final = final_events[-1] if final_events else {}
        if final.get("is_error") is not False or final.get("subtype") != "success":
            raise RuntimeError(f"claude -p did not complete successfully: {final.get('subtype', 'missing result')}")

    # Parse output based on runner
    if runner == "codex":
        run_result = parse_codex_json(result.stdout, model_id)
    elif runner == "opencode":
        run_result = parse_opencode_json(result.stdout)
    else:
        run_result = parse_stream_json(result.stdout, model_id)
    codex_tilth_calls = 0
    if runner == "codex":
        codex_tilth_calls = _validate_codex_stream(
            result.stdout, bool(mode.mcp_config_path), skill_paths, skill_roots,
        )

    run_result.task_name = task_name
    run_result.mode_name = mode_name
    run_result.model_name = model_name
    run_result.repetition = repetition

    # A tilth-armed claude cell without mcp__tilth__ tools silently degrades
    # into a native-only run and poisons the whole comparison (--safe-mode did
    # exactly this). Fail loudly instead; the main loop aborts the run.
    if runner == "claude" and mode.mcp_config_path:
        prefix = "mcp__plugin_woz_code__" if mode.plugin_dir else "mcp__tilth__"
        if not any(t.startswith(prefix) for t in run_result.available_tools):
            raise McpUnavailableError(
                f"mode '{mode_name}' expects {prefix} tools but the "
                f"session exposed none " 
                f"(tools={run_result.available_tools}, "
                f"mcp_servers={run_result.mcp_servers})"
            )

    strict_denied_bash_calls = (
        _audit_strict_claude(result.stdout, mode, run_result.available_tools)
        if strict_file_tools and runner == "claude" else 0
    )

    # Override duration if needed (subprocess timing may be more accurate)
    if run_result.duration_ms == 0:
        run_result.duration_ms = elapsed_ms

    # Check correctness
    correct, reason = task.check_correctness(
        run_result.result_text,
        str(repo_path),
    )
    run_result.correct = correct
    run_result.correctness_reason = reason
    grade_details = getattr(task, "grade_details", None)
    details = grade_details() if callable(grade_details) else {}

    # Build tool call breakdown
    tool_breakdown = tool_call_counts(run_result)

    # Collect per-turn context and output token counts.
    per_turn_context = [turn.context_tokens for turn in run_result.turns]
    per_turn_output = [turn.output_tokens for turn in run_result.turns]
    total_context = sum(per_turn_context)
    task_source = asdict(task.source)
    reported_version = _reported_tilth_version(mode)

    # Return JSON-serializable dict
    return {
        "task": task_name,
        "repo": task.repo,
        "mode": mode_name,
        "model": model_id,
        "model_alias": model_name,
        **({"reasoning_effort": reasoning_effort} if reasoning_effort is not None else {}),
        **({"max_budget_usd": max_budget_usd} if runner == "claude" else {}),
        **({"strict_file_tools": True, "strict_denied_bash_calls": strict_denied_bash_calls} if strict_file_tools else {}),
        **({"successful_tilth_mcp_calls": codex_tilth_calls} if runner == "codex" else {}),
        "capability": task.capability,
        "source": task_source,
        "repetition": repetition,
        "tilth_version": reported_version,
        "variant": _variant_metadata(mode, reported_version=reported_version),
        "num_turns": run_result.num_turns,
        "num_tool_calls": sum(tool_breakdown.values()),
        "tool_calls": tool_breakdown,
        "total_cost_usd": run_result.total_cost_usd,
        "duration_ms": run_result.duration_ms,
        "wall_duration_ms": elapsed_ms,
        "context_tokens": total_context,
        "output_tokens": run_result.total_output_tokens,
        "input_tokens": run_result.total_input_tokens,
        "cache_creation_tokens": run_result.total_cache_creation_tokens,
        "cache_creation_5m_tokens": sum(
            turn.cache_creation_5m_tokens for turn in run_result.turns
        ),
        "cache_creation_1h_tokens": sum(
            turn.cache_creation_1h_tokens for turn in run_result.turns
        ),
        "cache_read_tokens": run_result.total_cache_read_tokens,
        "per_turn_context_tokens": per_turn_context,
        "per_turn_output_tokens": per_turn_output,
        "per_turn_token_usage": [
            {
                "input_tokens": turn.input_tokens,
                "cache_creation_tokens": turn.cache_creation_tokens,
                "cache_creation_5m_tokens": turn.cache_creation_5m_tokens,
                "cache_creation_1h_tokens": turn.cache_creation_1h_tokens,
                "cache_read_tokens": turn.cache_read_tokens,
                "output_tokens": turn.output_tokens,
            }
            for turn in run_result.turns
        ],
        "correct": correct,
        "correctness_reason": reason,
        "result_text": run_result.result_text[:5000],
        "tool_sequence": _compact_tool_sequence(run_result),
        "available_tools": run_result.available_tools,
        "mcp_servers": run_result.mcp_servers,
        "model_usage": run_result.model_usage,
        "batch_sizes": tool_batch_sizes(run_result),
        "op_kinds": tool_op_kinds(run_result),
        "cost_source": run_result.cost_source,
        "trajectory_path": trajectory_path,
        **contamination_fields(trajectory_path, task),
        **details,
        "reused": False,
        **identity,
    }


def select_models(value: str | None, runner: str | None) -> list[str]:
    """Keep a selected runner from scheduling another provider's models."""
    if runner is None:
        return parse_comma_list(value or "sonnet", MODELS, "models")
    aliases = {name: model for name, model in MODELS.items() if RUNNERS[name] == runner}
    requested = value or ("gpt5" if runner == "codex" else "sonnet")
    if requested != "all":
        for name in requested.split(","):
            if name.strip() in MODELS and name.strip() not in aliases:
                raise ValueError(f"{name.strip()} is not a {runner} model")
    return parse_comma_list(requested, aliases, "models")


def select_modes(value: str | None, runner: str | None) -> list[str]:
    """Codex cannot enforce the Claude-only forced tool arm."""
    modes = parse_comma_list(value or ("baseline,tilth" if runner == "codex" else "all"), MODES, "modes")
    if runner == "codex" and "tilth_forced" in modes:
        raise ValueError("tilth_forced is not supported by the codex runner")
    return modes


def parse_comma_list(value: str, valid_options: dict, name: str) -> list[str]:
    """Parse comma-separated list and validate against valid options."""
    if value.lower() == "all":
        return list(valid_options.keys())

    items = [item.strip() for item in value.split(",") if item.strip()]
    invalid = [item for item in items if item not in valid_options]
    if invalid:
        raise ValueError(
            f"Invalid {name}: {', '.join(invalid)}. "
            f"Valid options: {', '.join(valid_options.keys())}"
        )
    return items

def select_tasks(value: str) -> list[str]:
    """Resolve ``--tasks``: ``all`` is the local registry; another name may be an admitted external instance.

    A name outside ``TASKS`` is resolved through ``external.resolve_task`` and
    registered in ``TASKS`` so planning, identity, and the runners find it.
    """
    if value.lower() == "all":
        return [name for name, task in TASKS.items() if not isinstance(task, external.ExternalTask)]
    for name in (item.strip() for item in value.split(",")):
        if not name or name in TASKS:
            continue
        resolved = external.resolve_task(name)
        if resolved is not None:
            TASKS[name] = resolved
        elif external.cached_row(name) is not None:
            raise ValueError(f"external task {name} is not admitted: {external.preflight.admit(name).reason}")
    return parse_comma_list(value, TASKS, "tasks")


def planned_cell_count(
    *,
    task_count: int,
    mode_count: int,
    model_count: int,
    repetitions: int,
) -> int:
    """Return the number of model calls in a benchmark schedule."""
    return task_count * mode_count * model_count * repetitions


def enforce_cell_ceiling(planned: int, *, maximum: int | None) -> None:
    """Reject a benchmark schedule that exceeds its explicit cell ceiling."""
    if maximum is not None and planned > maximum:
        raise ValueError(
            f"planned {planned} benchmark cells exceeds --max-cells {maximum}"
        )



def estimate_cell_cost(
    rows: list[dict],
    *,
    task: str,
    mode: str,
    model: str,
    run_max_cost: float | None,
    fallback: float,
) -> float:
    """Estimate a cell's cost: stored mean for its task and arm, else run maximum, else fallback."""
    costs = [
        row["total_cost_usd"] for row in rows
        if row.get("task") == task and row.get("mode") == mode and row.get("model") == model
        and baselines.is_completed(row) and isinstance(row.get("total_cost_usd"), (int, float))
    ]
    if costs:
        return sum(costs) / len(costs)
    if run_max_cost is not None:
        return run_max_cost
    return fallback


@dataclass(frozen=True)
class PlannedCell:
    """One scheduled benchmark cell and the key it is stored under."""

    task_name: str
    model_name: str
    repetition: int
    mode_name: str
    arm_order: tuple[str, ...]
    arm_index: int
    identity: dict

    @property
    def run_key(self) -> str:
        return self.identity["run_key"]


def _read_stream(stream_log_path: Path) -> str:
    try:
        return stream_log_path.read_text()
    except FileNotFoundError:
        return ""


# --- The evolve loop's cell path (benchmark/evolve) ---


class CellSpec(NamedTuple):
    """One cell ``run_plan`` schedules; ``repetition`` is the row's 0-based repetition number."""

    task: str
    mode: str
    model: str
    repetition: int


@dataclass(frozen=True)
class CandidateBuild:
    """A tilth binary built from one local commit."""

    git_sha: str
    binary_path: str
    binary_sha256: str


class PlanStopped(RuntimeError):
    """``run_plan`` stopped: ``ceiling`` (spend), ``quota`` (usage limit), ``cli-version`` (the agent CLI changed
    or stopped answering ``--version`` since the cell was planned) before a cell, or ``mcp-unavailable`` and
    ``invalid-cell`` (a config-level failure that main aborts on) after the failed cell is stored.

    ``rows`` holds the rows this call produced before it stopped; every paid one is stored.
    """

    def __init__(self, reason: str, detail: str, rows: list[dict]) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.rows = rows


class BaselineDrift(ValueError):
    """A planned stock-arm cell has a completed stored row only under different key inputs."""


class CandidateBuildFailed(RuntimeError):
    """``run_plan`` could not build ``candidate_sha``; the message ends with the failing build output."""


_CANDIDATE_BUILDS: dict[str, CandidateBuild] = {}


_TOOL_ENV_KEYS = frozenset({"PATH", "HOME", "USER", "LOGNAME", "LANG", "TERM", "TMPDIR", "SHELL", "CARGO_HOME",
                            "RUSTUP_HOME", "RUSTUP_TOOLCHAIN", "RUSTC_WRAPPER", "CARGO_INCREMENTAL"})
_TOOL_ENV_PREFIXES = ("LC_", "XDG_", "MISE_", "SCCACHE_")
_SECRET_MARKS = ("TOKEN", "SECRET", "PASSWORD", "KEY", "CREDENTIAL", "AUTH")


def build_tool_env() -> dict[str, str]:
    """The allowlisted env for a candidate's build and checks: no token, key, or credential reaches proposer code.

    The real HOME stays: cargo is a mise shim that resolves toolchains through HOME, and the caches live there.
    Files under HOME stay readable to candidate code; a sandbox is a follow-up.
    """
    return {key: value for key, value in os.environ.items()
            if (key in _TOOL_ENV_KEYS or key.startswith(_TOOL_ENV_PREFIXES))
            and not any(mark in key.upper() for mark in _SECRET_MARKS)}


def candidate_target_dir() -> Path:
    return RESULTS_DIR / "candidates" / "target"


def _run_cargo(argv: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True)


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _build_candidate(sha: str, repo: Path) -> CandidateBuild:
    """Build ``sha`` once per results dir: the binary and its digest are kept beside the sha and reused."""
    directory = RESULTS_DIR / "candidates" / sha
    binary, record = directory / "tilth", directory / "build.json"
    if binary.is_file() and record.is_file():
        built = json.loads(record.read_text())
        if built.get("binary_sha256") == _file_sha256(binary):
            return CandidateBuild(git_sha=sha, binary_path=str(binary), binary_sha256=built["binary_sha256"])
    # A stable checkout path per sha; the target dir is shared so dependencies build once.
    worktree = directory / "src"
    if worktree.exists():
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=repo, capture_output=True)
        shutil.rmtree(worktree, ignore_errors=True)
    _git("worktree", "add", "--detach", str(worktree), sha, cwd=repo)
    try:
        head = _git("rev-parse", "HEAD", cwd=worktree).strip()
        if head != sha:
            raise RuntimeError(f"candidate worktree {worktree} is at {head}, not {sha}")
        target_dir = candidate_target_dir()
        build = _run_cargo(["cargo", "build", "--release", "--locked"], cwd=worktree,
                           env={**build_tool_env(), "CARGO_TARGET_DIR": str(target_dir)})
        if build.returncode != 0:
            raise RuntimeError(f"cargo build --release --locked failed at {sha}:\n{(build.stderr or '')[-2000:]}")
        # The shared target dir is overwritten by the next build: keep this binary beside its sha.
        shutil.copy2(target_dir / "release" / "tilth", binary)
    finally:
        # Never raise here: a cleanup failure must not hide the build error already propagating.
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=repo, capture_output=True)
    digest = _file_sha256(binary)
    record.write_text(json.dumps({"git_sha": sha, "binary_sha256": digest}) + "\n")
    return CandidateBuild(git_sha=sha, binary_path=str(binary), binary_sha256=digest)


def build_candidate(sha: str, *, repo: Path | None = None) -> CandidateBuild:
    """Build tilth at the local commit ``sha`` of ``repo`` (default: this checkout) with
    ``cargo build --release --locked``, once per full sha; a short or symbolic ref resolves to it first."""
    repo = Path(repo or REPO_ROOT)
    full = _git("rev-parse", "--verify", f"{sha}^{{commit}}", cwd=repo).strip()
    if full not in _CANDIDATE_BUILDS:
        _CANDIDATE_BUILDS[full] = _build_candidate(full, repo)
    return _CANDIDATE_BUILDS[full]


def candidate_mode(mode: ModeConfig, build: CandidateBuild) -> ModeConfig:
    return replace(mode, binary_path=build.binary_path, git_ref=build.git_sha, git_sha=build.git_sha,
                   binary_sha256=build.binary_sha256)


def planned_identity(cell: CellSpec) -> dict:
    """The run key and key inputs of one ``run_plan`` cell: a bare, non-strict harness at the default budget."""
    return cell_identity(cell.task, cell.mode, cell.model, cell.repetition, bare=True, reasoning_effort=None,
                         max_budget_usd=DEFAULT_MAX_BUDGET_USD, strict_file_tools=False)


def _refuse_baseline_drift(pending: list[tuple[CellSpec, dict]], history: list[dict]) -> None:
    drift = {}
    for cell, identity in pending:
        if is_stock_arm(MODES[cell.mode]):
            changed = baselines.baseline_drift({
                **identity, "task": cell.task, "model": MODELS[cell.model], "mode": cell.mode,
                "repetition": cell.repetition, "reasoning_effort": None,
            }, history)
            if changed:
                drift[f"{cell.task}/{cell.mode}/{cell.model}/rep{cell.repetition}"] = changed
    if drift:
        details = "; ".join(f"{slot}: {', '.join(fields)}" for slot, fields in drift.items())
        raise BaselineDrift(f"stored baseline rows were recorded under different key inputs ({details}); "
                            "pass --refreeze-baselines to re-run them")


def check_baseline_drift(cells, *, panel: panels.Panel) -> None:
    """Raise ``BaselineDrift`` when a stock-arm cell of ``cells`` that the store cannot answer drifted; runs nothing."""
    panel.register(TASKS)
    planned = [(cell, planned_identity(cell)) for cell in (CellSpec(*cell) for cell in cells)]
    history = baselines.load_rows(RESULTS_DIR / baselines.STORE_FILENAME)
    reusable = baselines.completed_by_key(history)
    _refuse_baseline_drift([(cell, identity) for cell, identity in planned if identity["run_key"] not in reusable],
                           history)


def run_plan(
    cells,
    *,
    panel: panels.Panel,
    ledger: SpendLedger,
    candidate_sha: str | None,
    refreeze_baselines: bool,
    output: Path | None = None,
    cell_estimate_usd: float = DEFAULT_MAX_BUDGET_USD,
    store_only: bool = False,
    repo: Path | None = None,
) -> list[dict]:
    """Run ``cells`` for one panel, answering each from the result store when it can.

    Registers the panel's members in ``TASKS`` before planning any cell, and merges
    ``panel.stamp(task)`` into every row before ``baselines.store``. Tilth arms are
    served from the binary built at ``candidate_sha``. ``store_only`` returns only
    stored rows and never starts a runner. Every row returned is also appended to
    ``output``. ``repo`` holds ``candidate_sha`` (default: this checkout). Raises ``CandidateBuildFailed``
    when ``candidate_sha`` does not build, ``BaselineDrift`` before any cell when a stock-arm cell
    drifted without ``refreeze_baselines``, and ``PlanStopped`` when the ledger or
    a usage limit stops it before a cell.
    """
    panel.register(TASKS)
    cells = [CellSpec(*cell) for cell in cells]
    saved: dict[str, ModeConfig] = {}
    if candidate_sha is not None:
        try:
            build = build_candidate(candidate_sha, repo=repo)
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            output = getattr(error, "stderr", None) or ""
            raise CandidateBuildFailed(f"{error}\n{output}".strip()) from error
        for name in {cell.mode for cell in cells if _is_tilth_arm(MODES[cell.mode])}:
            saved[name] = MODES[name]
            MODES[name] = candidate_mode(MODES[name], build)
    try:
        return _run_plan(cells, panel=panel, ledger=ledger, refreeze_baselines=refreeze_baselines, output=output,
                         cell_estimate_usd=cell_estimate_usd, store_only=store_only)
    finally:
        MODES.update(saved)


def _run_plan(cells: list[CellSpec], *, panel, ledger: SpendLedger, refreeze_baselines: bool,
              output: Path | None, cell_estimate_usd: float, store_only: bool) -> list[dict]:
    planned = [(cell, planned_identity(cell)) for cell in cells]
    store_path = RESULTS_DIR / baselines.STORE_FILENAME
    history = baselines.load_rows(store_path)
    reusable = baselines.completed_by_key(history)
    pending = [(cell, identity) for cell, identity in planned if identity["run_key"] not in reusable]
    if pending and not store_only:
        if any(RUNNERS[cell.model] == "claude" for cell, _ in pending):
            guard_claude_auth(os.environ)
        if not refreeze_baselines:
            _refuse_baseline_drift(pending, history)

    rows: list[dict] = []
    run_max_cost: float | None = None
    stream_dir = RESULTS_DIR / "streams" / "plan"

    def emit(row: dict) -> None:
        rows.append(row)
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("a") as out:
                out.write(json.dumps(row) + "\n")

    def settle(row: dict, amount: float) -> None:
        nonlocal run_max_cost
        if not isinstance(row.get("contaminated"), bool):
            row.update(contamination_fields(row.get("trajectory_path"), TASKS.get(row.get("task"))))
        row["charged_usd"] = amount
        ledger.charge(amount, source=row.get("cost_source") or "native")
        run_max_cost = amount if run_max_cost is None else max(run_max_cost, amount)
        baselines.store(row, path=store_path)
        history.append(row)
        emit(row)

    for cell, identity in planned:
        task = TASKS[cell.task]
        mode = MODES[cell.mode]
        stamp = panel.stamp(cell.task)
        stored = reusable.get(identity["run_key"])
        if stored is not None:
            scanned = {} if isinstance(stored.get("contaminated"), bool) else contamination_fields(
                stored.get("trajectory_path"), task)
            emit({**stored, **scanned, **stamp, "variant": _variant_metadata(mode), "charged_usd": 0.0,
                  "reused": True})
            continue
        if store_only:
            continue
        cell_id = f"{cell.task}/{cell.mode}/{cell.model}/rep{cell.repetition}"
        estimate = estimate_cell_cost(history, task=cell.task, mode=cell.mode, model=MODELS[cell.model],
                                      run_max_cost=run_max_cost, fallback=cell_estimate_usd)
        if ledger.would_cross(estimate):
            raise PlanStopped("ceiling", f"${ledger.spent:.4f} spent + ${estimate:.4f} estimated for {cell_id} "
                                         f"+ ${ledger.reserve:.4f} reserved exceeds ${ledger.max_usd}", rows)
        runner = RUNNERS[cell.model]
        # As in main: a mid-run CLI update must not run a cell under a key planned for the old version.
        current_cli = cli_version(runner, fresh=True)
        if current_cli != identity["cli_version"]:
            raise PlanStopped("cli-version", f"{runner} --version probe failed before {cell_id}" if current_cli is None
                              else f"{runner} was {identity['cli_version']!r} when planned, now {current_cli!r}", rows)
        # A per-attempt suffix: a retried cell has the same key and must not overwrite its earlier sidecar.
        attempt = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        stream_log_path = stream_dir / (f"{identity['run_key'][:16]}_{cell.task}_{cell.mode}_rep{cell.repetition}"
                                        f"_{attempt}.jsonl")
        metadata = {
            "task": cell.task, "mode": cell.mode, "model": MODELS[cell.model], "model_alias": cell.model,
            **({"max_budget_usd": DEFAULT_MAX_BUDGET_USD} if runner == "claude" else {}),
            "capability": getattr(task, "capability", None), "repetition": cell.repetition,
            "variant": _variant_metadata(mode), **stamp, **identity, "reused": False,
        }

        def fail(fields: dict) -> None:
            native = stream_native_cost(_read_stream(stream_log_path))
            settle({**metadata, **fields, "correct": False,
                    **({"total_cost_usd": native} if native is not None else {}),
                    "cost_source": "native" if native is not None else "estimate",
                    "trajectory_path": write_trajectory(stream_log_path, runner)},
                   native if native is not None else estimate)

        try:
            result = run_single(cell.task, cell.mode, cell.model, cell.repetition, stream_log_path=stream_log_path,
                                bare=True, max_budget_usd=DEFAULT_MAX_BUDGET_USD)
        except InvalidCodexCellError as error:
            fail({"error": f"invalid_codex_cell: {error}", "correctness_reason": f"Invalid cell: {error}"})
            raise PlanStopped("invalid-cell", str(error), rows) from error
        except McpUnavailableError as error:
            fail({"error": f"mcp_unavailable: {error}", "correctness_reason": f"Exception: {error}"})
            raise PlanStopped("mcp-unavailable", str(error), rows) from error
        except subprocess.TimeoutExpired:
            fail({"error": "timeout", "timed_out": True, "correctness_reason": "Subprocess timed out"})
        except Exception as error:
            quota = str(error) if isinstance(error, QuotaExhaustedError) else detect_quota_rejection(
                _read_stream(stream_log_path))
            if quota:
                fail({"infra": "quota", "error": f"infra:quota: {quota}", "correctness_reason": f"Usage limit: {quota}"})
                raise PlanStopped("quota", quota, rows) from error
            fail({"error": str(error), "correctness_reason": f"Exception: {error}"})
        else:
            result.update({**stamp, **identity, "variant": _variant_metadata(mode), "reused": False})
            result.setdefault("trajectory_path", None)
            settle(result, result["total_cost_usd"])
    return rows


def mcp_server_commands(mode_name: str, runner: str | None) -> dict[str, str]:
    """Return the server commands that the selected runner will launch."""
    mode = MODES[mode_name]
    if not mode.mcp_config_path:
        return {}
    if runner == "codex":
        return {"tilth": mode.binary_path or TILTH_BIN}
    config = claude_mcp_config(mode) if runner != "opencode" else json.loads(Path(mode.mcp_config_path).read_text())
    return {
        name: server.get("command", "")
        for name, server in config.get("mcpServers", {}).items()
    }


def main():
    parser = argparse.ArgumentParser(
        description="Run tilth benchmarks",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run.py --experiment benchmark/experiments/upstream-fork.json --models sonnet --reps 5
  python run.py --models haiku --reps 1 --tasks find_definition --modes baseline,tilth
        """,
    )

    parser.add_argument(
        "--runner", choices=["codex"],
        help="Run only Codex models; defaults to gpt5",
    )
    parser.add_argument(
        "--models",
        help="Comma-separated model names or 'all' (default: sonnet, or gpt5 for codex)",
    )
    parser.add_argument(
        "--reasoning-effort", choices=["low", "medium", "high", "xhigh"],
        help="Reasoning effort for Claude or Codex models",
    )
    parser.add_argument(
        "--max-budget-usd", type=float, default=DEFAULT_MAX_BUDGET_USD,
        help="Claude budget per cell (positive finite USD)",
    )
    parser.add_argument(
        "--wozcode-plugin-dir", type=Path,
        help="Enable the Woz Code Claude arm from this plugin directory",
    )
    parser.add_argument(
        "--arm-order-seed", type=int,
        help="Shuffle legacy arms deterministically inside each task/model/repetition block",
    )
    parser.add_argument(
        "--strict-file-tools", action="store_true",
        help="Give MCP arms only Bash and MCP tools; allow Bash only for Go checks and gofmt",
    )
    parser.add_argument(
        "--reps",
        type=int,
        default=DEFAULT_REPS,
        help=f"Number of repetitions (default: {DEFAULT_REPS})",
    )
    parser.add_argument(
        "--max-cells",
        type=int,
        help="Abort before model calls when the expanded schedule exceeds this count",
    )
    parser.add_argument(
        "--max-usd", type=float,
        help="Run-wide spend ceiling in USD; required when any cell is not answered from the result store",
    )
    parser.add_argument(
        "--cell-estimate-usd", type=float,
        help="Cost estimate for a cell with no stored or earlier cost in this run "
             "(default: --max-budget-usd)",
    )
    parser.add_argument(
        "--refreeze-baselines", action="store_true",
        help="Run baseline cells whose stored baseline rows were recorded under different key inputs",
    )
    parser.add_argument(
        "--tasks",
        default="all",
        help="Comma-separated task names or 'all' (default: all)",
    )
    parser.add_argument(
        "--panel",
        type=Path,
        help="Run a pre-registered panel (benchmark/panels/<name>.json); excludes --tasks and --repos",
    )
    parser.add_argument(
        "--panel-split",
        choices=["cheap", "dev", "test", "all"],
        default="all",
        help="Panel members to run (default: all)",
    )
    parser.add_argument(
        "--candidate-sha",
        help="Serve tilth arms from a binary built with cargo build --release --locked at this local commit",
    )
    arm_group = parser.add_mutually_exclusive_group()
    arm_group.add_argument(
        "--modes",
        help="Legacy local A/B: comma-separated mode names or 'all'",
    )
    arm_group.add_argument(
        "--experiment",
        type=Path,
        help="Pinned variant experiment manifest",
    )
    parser.add_argument(
        "--repos",
        default="all",
        help="Comma-separated repo names or 'all' (default: all). "
             "Filters tasks to those targeting specified repos.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print detailed output for debugging",
    )
    parser.add_argument(
        "--bare",
        action="store_true",
        help="Strip the harness to built-in tools + the per-mode MCP config; "
             "pinned experiments enable this automatically. "
             'claude: passes --setting-sources "" (drops settings-borne '
             "customizations while retaining OAuth/keychain auth and "
             "--mcp-config servers). "
             "opencode: redirects XDG_CONFIG_HOME + sets OPENCODE_DISABLE_*. "
             "codex: ignores user config and rules, and resets MCP servers.",
    )

    args = parser.parse_args()
    if args.reps < 1:
        parser.error("--reps must be at least 1")
    if args.max_cells is not None and args.max_cells < 1:
        parser.error("--max-cells must be at least 1")
    if not math.isfinite(args.max_budget_usd) or args.max_budget_usd <= 0:
        parser.error("--max-budget-usd must be a positive finite number")
    for flag, value in (("--max-usd", args.max_usd), ("--cell-estimate-usd", args.cell_estimate_usd)):
        if value is not None and (not math.isfinite(value) or value <= 0):
            parser.error(f"{flag} must be a positive finite number")
    if args.experiment and args.arm_order_seed is not None:
        parser.error("--arm-order-seed applies only to legacy modes")
    if args.panel and (args.tasks != "all" or args.repos.lower() != "all"):
        parser.error("--panel selects its own tasks; it cannot be combined with --tasks or --repos")

    RESULTS_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    experiment = None
    panel = None
    try:
        models = select_models(args.models, args.runner)
        if args.strict_file_tools and any(RUNNERS[model] != "claude" for model in models):
            raise ValueError("--strict-file-tools requires Claude models")
        if args.reasoning_effort is not None and any(RUNNERS[model] == "opencode" for model in models):
            raise ValueError("--reasoning-effort is not supported by OpenCode")
        if args.wozcode_plugin_dir:
            if any(RUNNERS[model] != "claude" for model in models):
                raise ValueError("--wozcode-plugin-dir requires Claude models")
            MODES["wozcode"] = wozcode_mode(args.wozcode_plugin_dir)
        if args.panel:
            panel = panels.load_panel(args.panel, store_path=RESULTS_DIR / baselines.STORE_FILENAME)
            panel.register(TASKS)
            tasks_list = panel.select(args.panel_split)
        else:
            tasks_list = select_tasks(args.tasks)
        if args.experiment:
            experiment = load_experiment(args.experiment)
            configured_modes = experiment_modes(
                experiment,
                RESULTS_DIR / "configs" / timestamp,
            )
            compiler = rustc_version()
            configured_modes = {
                name: hydrate_mode_metadata(mode, compiler)
                for name, mode in configured_modes.items()
            }
            MODES.update(configured_modes)
            modes = [variant.name for variant in experiment.variants]
            if args.runner == "codex" and "tilth_forced" in modes:
                raise ValueError("tilth_forced is not supported by the codex runner")
        else:
            requested_modes = args.modes or ("baseline,tilth,wozcode" if args.wozcode_plugin_dir else None)
            modes = select_modes(requested_modes, args.runner)
            if "wozcode" in modes and not args.wozcode_plugin_dir:
                raise ValueError("wozcode mode requires --wozcode-plugin-dir")
    except ValueError as error:
        parser.error(str(error))
    if args.candidate_sha:
        if experiment is not None:
            parser.error("--candidate-sha builds the tilth arm itself; it cannot be combined with --experiment")
        try:
            build = build_candidate(args.candidate_sha)
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            parser.error(f"cannot build --candidate-sha {args.candidate_sha}: {error}")
        for mode_name in modes:
            if _is_tilth_arm(MODES[mode_name]):
                MODES[mode_name] = candidate_mode(MODES[mode_name], build)

    # Verify the exact MCP command that each selected runner will launch.
    for mode_name in modes:
        cfg_path = MODES[mode_name].mcp_config_path
        try:
            server_commands = mcp_server_commands(mode_name, args.runner)
        except (OSError, json.JSONDecodeError) as e:
            print(f"ERROR: cannot read MCP config {cfg_path} for mode '{mode_name}': {e}", file=sys.stderr)
            sys.exit(1)
        for server_name, cmd_str in server_commands.items():
            resolved = shutil.which(cmd_str) if "/" not in cmd_str else (cmd_str if os.path.isfile(cmd_str) and os.access(cmd_str, os.X_OK) else None)
            if not resolved:
                print(f"ERROR: MCP server '{server_name}' in {cfg_path} (mode '{mode_name}')", file=sys.stderr)
                print(f"       command '{cmd_str}' is not executable / not on PATH.", file=sys.stderr)
                print(f"       Fix the 'command' field in {cfg_path} or install the binary.", file=sys.stderr)
                sys.exit(1)
            # Smoke-test the binary with --version to catch broken installs.
            try:
                probe = subprocess.run([resolved, "--version"], capture_output=True, text=True, timeout=5)
                if probe.returncode != 0:
                    print(f"WARNING: MCP server '{server_name}' --version exited {probe.returncode}: {probe.stderr.strip()}", file=sys.stderr)
            except (FileNotFoundError, subprocess.TimeoutExpired) as e:
                print(f"ERROR: MCP server '{server_name}' at {resolved} failed to run: {e}", file=sys.stderr)
                sys.exit(1)
            if server_name == "tilth" and MODES[mode_name].binary_sha256 is None:
                with open(resolved, "rb") as binary:
                    digest = hashlib.file_digest(binary, "sha256").hexdigest()
                MODES[mode_name] = replace(
                    MODES[mode_name], binary_path=str(Path(resolved).resolve()),
                    binary_sha256=digest,
                )

    # Filter tasks by repo
    if args.repos.lower() != "all":
        requested_repos = set(r.strip() for r in args.repos.split(",") if r.strip())
        tasks_list = [t for t in tasks_list if TASKS[t].repo in requested_repos]
        if not tasks_list:
            parser.error(f"No tasks found for repos: {args.repos}")

    total_runs = planned_cell_count(
        task_count=len(tasks_list),
        mode_count=len(modes),
        model_count=len(models),
        repetitions=args.reps,
    )
    try:
        enforce_cell_ceiling(total_runs, maximum=args.max_cells)
    except ValueError as error:
        parser.error(str(error))

    # Guard external cells before any model call: each must be admitted on this host.
    for task_name in tasks_list:
        if isinstance(TASKS[task_name], external.ExternalTask):
            verdict = external.preflight.admit(task_name)
            if not verdict.admitted:
                print(f"ERROR: external task {task_name} is not admitted: {verdict.reason}", file=sys.stderr)
                sys.exit(1)

    # Prepared tasks build their own workdirs; only the rest copy REPOS fixtures.
    fixture_tasks = [name for name in tasks_list if not _is_prepared(TASKS[name])]

    # Validate and restore the synthetic source once. Every cell runs from a
    # disposable copy, so the scheduler never mutates this source again.
    if "synthetic" in {TASKS[name].repo for name in fixture_tasks}:
        if not SYNTHETIC_REPO.exists():
            parser.error(
                f"Synthetic repo not found at {SYNTHETIC_REPO}; "
                "run python benchmark/fixtures/setup.py"
            )
        reset_repo()

    # Validate real-world repos exist (for selected tasks)
    selected_repos = set(TASKS[t].repo for t in fixture_tasks) - {"synthetic"}
    for repo_name in selected_repos:
        repo_path = REPOS[repo_name].path
        if not repo_path.exists():
            print(f"ERROR: Repo '{repo_name}' not cloned.")
            print(f"Expected at: {repo_path}")
            print("Run setup_repos.py to clone repositories:")
            print("  python benchmark/fixtures/setup_repos.py")
            sys.exit(1)

    # Clean real-world repos before starting (removes junk files from previous runs)
    for repo_name in selected_repos:
        repo_path = REPOS[repo_name].path
        ensure_repo_clean(repo_path, REPOS[repo_name].commit_sha)
        if args.verbose:
            print(f"Cleaned repo: {repo_name}")


    bare = args.bare or experiment is not None or args.wozcode_plugin_dir is not None or args.strict_file_tools
    cells: list[PlannedCell] = []
    for task_name in tasks_list:
        for model_name in models:
            for rep in range(args.reps):
                # Experiment arms are shuffled within each matched task/model/
                # repetition block from the manifest seed, never run in long arm runs.
                arm_order = (
                    randomized_arm_order(
                        modes,
                        seed=experiment.arm_order_seed if experiment else (args.arm_order_seed or 0),
                        task=task_name,
                        model=model_name,
                        repetition=rep,
                    )
                    if experiment or args.arm_order_seed is not None
                    else list(modes)
                )
                for arm_index, mode_name in enumerate(arm_order):
                    cells.append(PlannedCell(
                        task_name, model_name, rep, mode_name, tuple(arm_order), arm_index,
                        cell_identity(
                            task_name, mode_name, model_name, rep,
                            bare=bare, reasoning_effort=args.reasoning_effort,
                            max_budget_usd=args.max_budget_usd,
                            strict_file_tools=args.strict_file_tools,
                        ),
                    ))

    # A cell whose key matches a completed stored row is answered from the
    # store. Any other cell spawns a model runner, which makes this a paid run.
    store_path = RESULTS_DIR / baselines.STORE_FILENAME
    history = baselines.load_rows(store_path)
    reusable = baselines.completed_by_key(history)
    pending = [cell for cell in cells if cell.run_key not in reusable]
    if pending:
        if args.max_usd is None:
            parser.error(
                f"{len(pending)} of {len(cells)} cells have no reusable stored row; "
                "a paid run requires --max-usd"
            )
        if any(RUNNERS[cell.model_name] == "claude" for cell in pending):
            try:
                guard_claude_auth(os.environ)
            except ClaudeAuthError as error:
                parser.error(str(error))
        drift = {}
        for cell in pending:
            if not is_stock_arm(MODES[cell.mode_name]):
                continue
            planned = {
                **cell.identity, "task": cell.task_name, "model": MODELS[cell.model_name],
                "mode": cell.mode_name, "repetition": cell.repetition,
                "reasoning_effort": args.reasoning_effort,
            }
            changed = baselines.baseline_drift(planned, history)
            if changed:
                drift[f"{cell.task_name}/{cell.mode_name}/{cell.model_name}/rep{cell.repetition}"] = changed
        if drift and not args.refreeze_baselines:
            details = "; ".join(f"{slot}: {', '.join(fields)}" for slot, fields in drift.items())
            parser.error(
                "stored baseline rows were recorded under different key inputs "
                f"({details}); pass --refreeze-baselines to re-run them"
            )
        if drift:
            print(f"Refreezing {len(drift)} drifted baseline cell(s).")

    # Include the model in the filename when one process owns one model.
    model_suffix = f"_{models[0]}" if len(models) == 1 else ""
    output_file = RESULTS_DIR / f"benchmark_{timestamp}{model_suffix}.jsonl"
    stream_log_dir = RESULTS_DIR / "streams" / timestamp

    # Print configuration summary
    print("=" * 70)
    print("tilth Benchmark Runner")
    print("=" * 70)
    print(f"Models:      {', '.join(models)}")
    if panel:
        print(f"Panel:       {panel.name} ({args.panel_split}, split {panel.split_digest[:12]})")
    print(f"Tasks:       {', '.join(tasks_list)}")
    print(f"Modes:       {', '.join(modes)}")
    repos_used = sorted(set(TASKS[t].repo for t in tasks_list))
    print(f"Repos:       {', '.join(repos_used)}")
    print(f"Repetitions: {args.reps}")
    print(f"Reused:      {len(cells) - len(pending)} of {len(cells)} cells from {store_path}")
    if args.max_usd is not None:
        print(f"Spend cap:   ${args.max_usd:.2f}")
    print(f"Output:      {output_file}")
    print(f"Streams:     {stream_log_dir}/<cell>.jsonl  (tail -f for live agent output)")
    print("=" * 70)
    print()

    ledger = SpendLedger(max_usd=args.max_usd)
    run_max_cost: float | None = None
    stop_reason: str | None = None
    fallback_estimate = args.cell_estimate_usd or args.max_budget_usd
    reported_versions: dict[str, Optional[str]] = {}

    with open(output_file, "w") as output:

        def record(row: dict) -> None:
            output.write(json.dumps(row) + "\n")
            output.flush()

        def settle(row: dict, amount: float) -> None:
            """Charge and store one paid cell; called exactly once per cell."""
            nonlocal run_max_cost
            if not isinstance(row.get("contaminated"), bool):
                row.update(contamination_fields(row.get("trajectory_path"), TASKS.get(row.get("task"))))
            row["charged_usd"] = amount
            ledger.charge(amount, source=row.get("cost_source") or "native")
            run_max_cost = amount if run_max_cost is None else max(run_max_cost, amount)
            baselines.store(row, path=store_path)
            history.append(row)

        def report(row: dict) -> None:
            record(row)
            status = "✓" if row["correct"] else "✗"
            print(
                f"  {status} "
                f"{row['num_turns']}t "
                f"{row['context_tokens']:,}ctx "
                f"{row['output_tokens']:,}out "
                f"${row['total_cost_usd']:.4f} "
                f"{row['duration_ms']:,}ms"
            )
            if not row["correct"]:
                print(f"  → {row['correctness_reason']}")

        def record_failure(row: dict, stream_log_path: Path, runner: str, estimate: float) -> dict:
            """Store a failed cell, charging its native cost or else its pre-run estimate.

            Only a native cost is reported as ``total_cost_usd``; an estimate is
            charged to the ledger as ``charged_usd`` but is not a measured cost.
            """
            native = stream_native_cost(_read_stream(stream_log_path))
            failed = {
                **row,
                **({"total_cost_usd": native} if native is not None else {}),
                "cost_source": "native" if native is not None else "estimate",
                "trajectory_path": write_trajectory(stream_log_path, runner),
            }
            settle(failed, native if native is not None else estimate)
            record(failed)
            return failed

        for current_run, cell in enumerate(cells, start=1):
            task_name, model_name, rep, mode_name = (
                cell.task_name, cell.model_name, cell.repetition, cell.mode_name,
            )
            task = TASKS[task_name]
            runner = RUNNERS[model_name]
            run_id = f"{task_name}/{mode_name}/{model_name}/rep{rep}"
            print(f"[{current_run}/{total_runs}] {run_id}")
            experiment_metadata = {
                "experiment_manifest": str(experiment.path) if experiment else None,
                "arm_order_seed": experiment.arm_order_seed if experiment else args.arm_order_seed,
                "arm_order": list(cell.arm_order),
                "arm_order_index": cell.arm_index,
                **(panel.stamp(task_name) if panel else {}),
            }

            stored = reusable.get(cell.run_key)
            if stored is not None:
                # Written like a fresh row: this run's schedule and variant metadata,
                # the stored outcome. The run key already pins what the variant runs.
                # This run charged nothing for it, whatever the storing run did.
                if mode_name not in reported_versions:
                    reported_versions[mode_name] = _reported_tilth_version(MODES[mode_name])
                reported_version = reported_versions[mode_name]
                scanned = (
                    {} if isinstance(stored.get("contaminated"), bool)
                    else contamination_fields(stored.get("trajectory_path"), task)
                )
                record({
                    **stored, **scanned, **experiment_metadata, "tilth_version": reported_version,
                    "variant": _variant_metadata(MODES[mode_name], reported_version=reported_version),
                    "charged_usd": 0.0, "reused": True,
                })
                print(f"  ↺ reused stored row ({'✓' if stored.get('correct') else '✗'})")
                continue

            estimate = estimate_cell_cost(
                history, task=task_name, mode=mode_name, model=MODELS[model_name],
                run_max_cost=run_max_cost, fallback=fallback_estimate,
            )
            if ledger.would_cross(estimate):
                stop_reason = (
                    f"spend ceiling: ${ledger.spent:.4f} spent + ${estimate:.4f} estimated for "
                    f"{run_id} exceeds --max-usd {args.max_usd}"
                )
                break
            current_cli = cli_version(runner, fresh=True)
            if current_cli != cell.identity["cli_version"]:
                stop_reason = (
                    f"agent CLI version probe failed: {runner} --version before {run_id}"
                    if current_cli is None else
                    f"agent CLI version changed mid-run: {runner} was "
                    f"{cell.identity['cli_version']!r} when planned, now {current_cli!r}"
                )
                break

            cell_slug = f"{current_run:02d}_{task_name}_{mode_name}_{model_name}_rep{rep}"
            stream_log_path = stream_log_dir / f"{cell_slug}.jsonl"
            mode = MODES[mode_name]
            record_metadata = {
                "task": task_name,
                "mode": mode_name,
                "model": MODELS[model_name],
                "model_alias": model_name,
                **({"reasoning_effort": args.reasoning_effort} if args.reasoning_effort is not None else {}),
                **({"max_budget_usd": args.max_budget_usd} if runner == "claude" else {}),
                **({"strict_file_tools": True} if args.strict_file_tools else {}),
                "capability": task.capability,
                "source": asdict(task.source),
                "repetition": rep,
                "per_turn_output_tokens": [],
                "variant": _variant_metadata(mode),
                **experiment_metadata,
                **cell.identity,
                "reused": False,
            }

            try:
                result = run_single(
                    task_name,
                    mode_name,
                    model_name,
                    rep,
                    verbose=args.verbose,
                    stream_log_path=stream_log_path,
                    bare=bare,
                    reasoning_effort=args.reasoning_effort,
                    max_budget_usd=args.max_budget_usd,
                    strict_file_tools=args.strict_file_tools,
                )
                result.update(experiment_metadata)
                if args.reasoning_effort is not None:
                    result["reasoning_effort"] = args.reasoning_effort
                if runner == "claude":
                    result["max_budget_usd"] = args.max_budget_usd
                if args.strict_file_tools:
                    result["strict_file_tools"] = True
                result.update(cell.identity)
                result["reused"] = False
                result.setdefault("trajectory_path", None)

            except InvalidCodexCellError as error:
                print(f"  ✗ INVALID CODEX CELL: {error}")
                record_failure({
                    **record_metadata,
                    "error": f"invalid_codex_cell: {error}",
                    "correct": False,
                    "correctness_reason": f"Invalid cell: {error}",
                }, stream_log_path, runner, estimate)
                print("\nAborting run: invalid Codex cell; inspect its raw stream.")
                sys.exit(1)

            except McpUnavailableError as error:
                # Config-level failure: every later cell in this
                # mode would fail identically. Abort instead of
                # burning budget on an invalid comparison.
                print(f"  ✗ MCP UNAVAILABLE: {error}")
                record_failure({
                    **record_metadata,
                    "error": f"mcp_unavailable: {error}",
                    "correct": False,
                    "correctness_reason": f"Exception: {error}",
                }, stream_log_path, runner, estimate)
                print("\nAborting run: the MCP-armed mode is "
                      "misconfigured; fix it and re-run.")
                sys.exit(1)

            except subprocess.TimeoutExpired:
                print(f"  ✗ TIMEOUT (>{_task_timeout(task)}s)")
                record_failure({
                    **record_metadata,
                    "error": "timeout",
                    "timed_out": True,
                    "correct": False,
                    "correctness_reason": "Subprocess timed out",
                }, stream_log_path, runner, estimate)

            except Exception as error:
                quota = (
                    str(error) if isinstance(error, QuotaExhaustedError)
                    else detect_quota_rejection(_read_stream(stream_log_path))
                )
                if quota:
                    # Every later cell would be rejected too; stop and
                    # leave completed rows reusable for a resumed run.
                    print(f"  ✗ USAGE LIMIT: {quota}")
                    record_failure({
                        **record_metadata,
                        "infra": "quota",
                        "error": f"infra:quota: {quota}",
                        "correct": False,
                        "correctness_reason": f"Usage limit: {quota}",
                    }, stream_log_path, runner, estimate)
                    stop_reason = f"usage limit: {quota}"
                    break
                print(f"  ✗ ERROR: {error}")
                if args.verbose:
                    import traceback
                    traceback.print_exc()
                record_failure({
                    **record_metadata,
                    "error": str(error),
                    "correct": False,
                    "correctness_reason": f"Exception: {error}",
                }, stream_log_path, runner, estimate)

            else:
                # Outside the try: an error from here on must not settle the cell twice.
                settle(result, result["total_cost_usd"])
                try:
                    report(result)
                except Exception as error:
                    stop_reason = f"reporting failed after cell {run_id} was stored: {error!r}"
                    break

    # Clean real-world repos after run (remove junk files written by Claude sessions)
    for repo_name in selected_repos:
        repo_path = REPOS[repo_name].path
        ensure_repo_clean(repo_path, REPOS[repo_name].commit_sha)

    # Print summary
    print()
    print("=" * 70)
    print("Benchmark stopped early." if stop_reason else "Benchmark complete!")
    if stop_reason:
        print(f"Stop reason: {stop_reason}")
        print("Re-run the same command to resume; completed cells are reused from the store.")
    print(f"Spend: ${ledger.spent:.4f}" + (f" of ${args.max_usd:.2f}" if args.max_usd is not None else ""))
    print(f"Results saved to: {output_file}")
    print("=" * 70)
    print()
    print("To generate a report, run:")
    print(f"  python benchmark/analyze.py {output_file}")
    print()
    if stop_reason:
        sys.exit(1)


if __name__ == "__main__":
    main()
