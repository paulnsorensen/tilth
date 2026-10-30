#!/usr/bin/env python3
"""
Benchmark runner for tilth performance evaluation.

Executes the selected agent CLI for each task, mode, model, and repetition.
Records token usage, cost, correctness, and tool usage to JSONL format.
"""

import argparse
import atexit
import functools
import hashlib
import inspect
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
from collections.abc import Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, is_dataclass, replace
from dataclasses import fields as _dataclass_fields
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple, Optional

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

import auth
import baselines
import chains
import erosion
import external
import external.contamination
import external.proc
import external.preflight
import panels
from spend import BudgetBusyError, SpendLedger, hold_run_lock, plan_budget_id, settle_failed
from config import (
    BENCHMARK_DIR,
    DEFAULT_MAX_BUDGET_USD,
    DEFAULT_REPS,
    FIXTURES_DIR,
    MODELS,
    PINNED_MODELS,
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
    tool_batch_sizes,
    tool_call_counts,
    tool_op_kinds,
)
from tasks import TASKS
from tasks.base import SYNTHETIC_LANGUAGE, Task
from tasks.json_tasks import register_json_tasks
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
        "plugin_sha256": mode.plugin_sha256,
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
        plugin_git_sha=plugin_git_sha, plugin_sha256=_tree_sha256(plugin_dir),
    )


def _is_woz_arm(mode: ModeConfig) -> bool:
    """A Woz Code arm: a plugin directory that also serves an MCP server."""
    return bool(mode.plugin_dir and mode.mcp_config_path)


def _is_skill_arm(mode: ModeConfig) -> bool:
    """A skill arm: a plugin directory loaded for its skills, with no MCP server."""
    return bool(mode.plugin_dir and not mode.mcp_config_path)


def claude_mcp_config(mode: ModeConfig) -> dict:
    if _is_woz_arm(mode):
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
    return REPOS[repo_name].resolved_path()


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
# No runner bills Anthropic through the API or needs an ANTHROPIC_* setting:
# Claude cells use the subscription (CLAUDE_CODE_OAUTH_TOKEN or /login), and a variable such as
# ANTHROPIC_BASE_URL or ANTHROPIC_MODEL would redirect a cell outside its run key.
_PROVIDER_AUTH_PREFIXES = ("OPENAI_", "OPENROUTER_")
_PROVIDER_AUTH_KEYS = frozenset({"CODEX_API_KEY"})


_DEFAULT_TILTH_BIN = object()


def claude_auth_targets(cells, *, bare: bool) -> list[auth.AuthTarget]:
    """One auth target per claude cell of ``(task, mode, model)`` names; none when no cell uses claude.

    A target holds the seed repo that each cell copies and the seed cell env, and whether the cell loads
    user, project, and local settings (it does not when it passes ``--setting-sources ""``). A cell runs in a
    temp worktree or copy and may get a fresh temp config dir, so the check is conservative. A task with no
    fixture repo builds its own workdir; its target is the system temp dir, so only the init gate checks its
    project settings.
    """
    claude_cells = [(task_name, mode_name) for task_name, mode_name, model_name in cells
                    if RUNNERS[model_name] == "claude"]
    if not claude_cells:
        return []
    env = build_runner_env("claude", tilth_bin=None)
    return [
        auth.AuthTarget(
            cwd=get_repo_path(TASKS[task_name].repo) if TASKS[task_name].fixture_path is not None
            else Path(tempfile.gettempdir()),
            env=env, loads_user_settings=not (bare or MODES[mode_name].plugin_dir),
        )
        for task_name, mode_name in claude_cells
    ]


@functools.cache
def _isolated_config_dir() -> str:
    """One empty CLAUDE_CONFIG_DIR per process for the evolve and judge preflight, removed at exit."""
    path = tempfile.mkdtemp(prefix="tilth-auth-")
    atexit.register(shutil.rmtree, path, ignore_errors=True)
    return path


@contextmanager
def isolated_claude_config(env: dict[str, str], prefix: str):
    """Point ``env`` at an empty temp CLAUDE_CONFIG_DIR for one evolve or judge call when it uses the token.

    A ``/login`` session keeps its credentials in the host config dir, so without the token the call keeps
    the host dir. ``--setting-sources ""`` and ``--strict-mcp-config`` still keep its settings and MCP
    servers out.
    """
    if not env.get(auth.OAUTH_TOKEN_KEY):
        yield
        return
    with tempfile.TemporaryDirectory(prefix=prefix) as config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
        yield


def isolated_call_target() -> auth.AuthTarget:
    """The auth target of an evolve or judge call: temp cwd, no settings sources, and a temp
    CLAUDE_CONFIG_DIR under the token (see ``isolated_claude_config``)."""
    env = dict(build_runner_env("claude", tilth_bin=None))
    if env.get(auth.OAUTH_TOKEN_KEY):
        env["CLAUDE_CONFIG_DIR"] = _isolated_config_dir()
    return auth.AuthTarget(cwd=Path(tempfile.gettempdir()), env=env, loads_user_settings=False)


def failed_cell_cost(stream: str, runner: str, estimate: float, model: str | None) -> tuple[float, str]:
    """A failed cell's charge: native cost, else the priced stream, else the estimate, with its source."""
    if runner in {"claude", "codex"}:
        return settle_failed(stream, runner, estimate, model)
    return estimate, "estimate"


def _agent_venv(task: object, workdir: Path) -> Path | None:
    """The workdir's venv when the task prepared one; never the separate grading venv."""
    venv = workdir / ".venv"
    return venv if (venv / "bin").is_dir() else None


def _host_runner_argv(cmd: list[str], env: Mapping[str, str], agent_venv: Path | None) -> list[str]:
    """Resolve the runner on PATH without the agent venv, so a task-installed console script cannot replace it."""
    if agent_venv is None:
        return cmd
    venv_bin = str(agent_venv / "bin")
    host_path = os.pathsep.join(entry for entry in env.get("PATH", "").split(os.pathsep) if entry != venv_bin)
    host_runner = shutil.which(cmd[0], path=host_path)
    if host_runner is None:
        raise FileNotFoundError(f"runner not found on the host PATH: {cmd[0]}")
    return [host_runner, *cmd[1:]]


def build_runner_env(
    runner: str,
    *,
    opencode_config: Optional[str] = None,
    bare: bool = False,
    ambient: Optional[Mapping[str, str]] = None,
    tilth_bin: object = _DEFAULT_TILTH_BIN,
    language: str | None = None,
    agent_venv: Path | None = None,
) -> dict[str, str]:
    """Build a minimal environment for one runner subprocess.

    ``language`` is the task's language. The agent's own builds then get the C settings its grade uses.
    ``agent_venv`` is the agent workdir's venv; its ``bin`` goes first on PATH so the agent finds its dependencies.
    """
    source = os.environ if ambient is None else ambient
    if runner == "claude":
        auth.refuse_env(source)
    env = {
        key: value
        for key, value in source.items()
        if key in _RUNTIME_ENV_KEYS
        or key in _PROVIDER_AUTH_KEYS
        or (runner == "claude" and key in {"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN"})
        or key.startswith(_PROVIDER_AUTH_PREFIXES)
        or key.startswith("LC_")
    }
    # Pin the agent CLI for the whole run: a mid-run update changes the run key.
    env["DISABLE_AUTOUPDATER"] = "1"
    if language is not None:
        env.update(external.proc.c_build_env(language))
    if language == "javascript":
        env["PATH"] = str(external.javascript.node_bin()) + os.pathsep + env.get("PATH", "")

    selected_tilth_bin = TILTH_BIN if tilth_bin is _DEFAULT_TILTH_BIN else tilth_bin
    if isinstance(selected_tilth_bin, str):
        tilth_dir = os.path.dirname(selected_tilth_bin)
        if tilth_dir:
            env["PATH"] = tilth_dir + os.pathsep + env.get("PATH", "")

    if agent_venv is not None:
        env["VIRTUAL_ENV"] = str(agent_venv)
        env["PATH"] = str(agent_venv / "bin") + os.pathsep + env.get("PATH", "")

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
def _built_workdir():
    """Yield a path for a workdir that a task with no fixture builds in ``prepare``."""
    with tempfile.TemporaryDirectory(prefix="tilth-benchmark-") as temp_dir:
        yield Path(temp_dir) / "repo"


class ArmUnavailableError(RuntimeError):
    """An arm lacks something its config promised; every later cell of the arm would fail alike."""

    kind = "arm"
    abort_hint = "the arm is misconfigured"


class McpUnavailableError(ArmUnavailableError):
    """A mode expected an MCP server that the session did not expose."""

    kind = "mcp"
    abort_hint = "the MCP-armed mode is misconfigured"


class SkillUnavailableError(ArmUnavailableError):
    """A skill arm expected a plugin skill that the session did not load."""

    kind = "skill"
    abort_hint = "the skill arm is misconfigured"


def _uses_claude_session_features(mode: ModeConfig) -> bool:
    """Skill arms and appended-prompt arms need Claude's session init, which other runners lack."""
    return _is_skill_arm(mode) or mode.prompt_mode == "append"


def _expected_plugin_skills(plugin_dir: str) -> list[str]:
    """The ``<plugin>:<skill>`` names that a ``--plugin-dir`` manifest declares.

    A manifest without a ``skills`` field uses the default ``skills/`` folder.
    """
    root = Path(plugin_dir)
    manifest = json.loads((root / ".claude-plugin" / "plugin.json").read_text())
    entries = manifest.get("skills", ["skills"] if (root / "skills").is_dir() else [])  # default folder
    if isinstance(entries, str):
        entries = [entries]
    names: list[str] = []
    for entry in entries:
        folder = (root / entry).resolve()
        if (folder / "SKILL.md").is_file() or not folder.is_dir():
            names.append(folder.name)  # one skill folder (or a declared path not on disk)
        else:
            names.extend(sorted(child.name for child in folder.iterdir() if (child / "SKILL.md").is_file()))
    return [f"{manifest['name']}:{name}" for name in names]


def _require_plugin_skills(mode_name: str, mode: ModeConfig, run_result) -> None:
    """Fail closed when the init event does not list every skill the plugin declares.

    Claude Code can start without a plugin's skills (a flag or a manifest problem), and
    the cell would then run as a plain baseline. A manifest that declares no skills
    cannot be checked, so it fails too.
    """
    expected = _expected_plugin_skills(mode.plugin_dir)
    missing = [skill for skill in expected if skill not in run_result.skills]
    if expected and not missing:
        return
    raise SkillUnavailableError(
        f"mode '{mode_name}' expects plugin_dir '{mode.plugin_dir}' to load "
        f"{missing if expected else 'at least one skill (its manifest declares none)'} but the session's "
        f"skills were {run_result.skills} (plugins={run_result.plugins}, "
        f"plugin_errors={run_result.plugin_errors})"
    )


def _plugin_name(plugin_dir: str) -> str:
    return json.loads((Path(plugin_dir) / ".claude-plugin" / "plugin.json").read_text())["name"]


_SKILL_AUDIT_UNMEASURED = {"skill_calls": None, "non_plugin_skill_calls": None, "bundled_skills_exposed": None}


def _skill_audit_fields(mode: ModeConfig, run_result) -> dict:
    """Audit which skills a skill arm could and did call; ``None`` fields for other arms.

    Skill arms keep the ``Skill`` tool and drop ``--disable-slash-commands``, so the
    init event also lists Claude Code's bundled skills and the agent may call them.
    """
    if not _is_skill_arm(mode):
        return dict(_SKILL_AUDIT_UNMEASURED)
    prefix = f"{_plugin_name(mode.plugin_dir)}:"
    calls = [
        str(call.input.get("skill", ""))
        for turn in run_result.turns for call in turn.tool_calls if call.name == "Skill"
    ]
    return {
        "skill_calls": calls,
        "non_plugin_skill_calls": sum(1 for name in calls if not name.startswith(prefix)),
        "bundled_skills_exposed": sorted(s for s in run_result.skills if not s.startswith(prefix)),
    }



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
    woz = _is_woz_arm(mode)
    if not seed:
        if woz:
            raise ValueError("Woz Code requires an explicit CLAUDE_CONFIG_DIR auth seed")
        yield None
        return
    seed_auth = Path(seed) / "wozcode" / "auth.json"
    if woz and not seed_auth.is_file():
        raise ValueError(f"Woz Code auth seed not found: {seed_auth}")
    with tempfile.TemporaryDirectory(prefix="tilth-claude-config-") as temp_dir:
        config_dir = Path(temp_dir)
        cell_auth = config_dir / "wozcode" / "auth.json"
        if woz:
            cell_auth.parent.mkdir()
            _copy_private(seed_auth, cell_auth)
        try:
            yield config_dir
        finally:
            if woz and cell_auth.is_file():
                _copy_private(cell_auth, seed_auth)

_STRICT_NATIVE_FILE_TOOLS = frozenset({"Read", "Edit", "Write", "MultiEdit", "Grep", "Glob", "NotebookEdit", "LS"})
_STRICT_BASELINE_TOOLS = ("Read", "Edit", "Write", "Grep", "Glob", "Bash")
_STRICT_SYSTEM_GUIDANCE = (
    "Use only the available file tools to read, search, or edit source files. "
    "Use Bash only to run tests, builds, and other commands that do not read or write source files. "
    "Do not delegate tasks."
)


# The hashed command template names the baseline hook binary by placeholder: its path is a
# host detail, and the hook source is keyed by _hook_source_key.
_HOOK_BIN_PLACEHOLDER = "<hook-bin>"


# Exec-form hooks and `onFailure: block` need this Claude CLI; older ones can run a bare `tilth` unguarded.
_MIN_STRICT_CLAUDE = (2, 1, 295)


def _require_strict_cli() -> None:
    """Refuse a strict run on a Claude CLI too old to honor exec-form hooks with ``onFailure: block``."""
    found = cli_version("claude")
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", found or "")
    needed = ".".join(map(str, _MIN_STRICT_CLAUDE))
    if match is None or tuple(map(int, match.groups())) < _MIN_STRICT_CLAUDE:
        raise ValueError(
            f"strict_file_tools needs Claude CLI >= {needed} (exec-form hooks, onFailure=block); found {found!r}")


class HookLoosenedError(RuntimeError):
    """A strict cell ran a Bash call that the hook at ``hook_sha`` denies."""


def _strict_bash_settings(hook_binary: str, hint: str) -> str:
    # Exec form (no shell) and onFailure=block: a missing or crashing hook binary denies the call.
    return json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{
        "type": "command", "command": hook_binary, "args": ["hook", "--hint", hint], "onFailure": "block",
    }]}]}})


def _strict_hook_spec(mode: ModeConfig, baseline_hook_binary: str | None) -> tuple[str, str]:
    """The hook binary and hint a strict cell registers: a tilth arm runs its own binary."""
    if _is_tilth_arm(mode):
        binary = mode.binary_path or baseline_hook_binary
        hint = "tilth"
    else:
        binary, hint = baseline_hook_binary, "native"
    if not binary:
        raise ValueError("strict_file_tools needs a hook binary")
    return binary, hint


# Files outside src/hook that change the built hook: the dependency lock, the manifest, and the CLI entry.
_HOOK_BUILD_INPUTS = ("Cargo.lock", "Cargo.toml", "src/main.rs")
_FULL_SHA = re.compile(r"[0-9a-f]{40}")


@functools.lru_cache(maxsize=None)
def _hook_source_key_at(full_sha: str, repo: str) -> str:
    tree = _git("rev-parse", f"{full_sha}:src/hook", cwd=Path(repo)).strip()
    digest = hashlib.sha256()
    for name in _HOOK_BUILD_INPUTS:
        blob = subprocess.run(["git", "show", f"{full_sha}:{name}"], cwd=repo, check=True, capture_output=True).stdout
        digest.update(name.encode() + b"\0" + hashlib.sha256(blob).digest())
    return f"{tree}:{digest.hexdigest()}"


def _hook_source_key(hook_sha: str, repo: Path | None = None) -> str:
    """The git tree hash of ``src/hook`` plus a digest of ``_HOOK_BUILD_INPUTS``, all at ``hook_sha``.

    A full commit sha names immutable content, so the key is cached per (full sha, repo)
    and a full sha skips the per-call ``rev-parse``.
    """
    repo = Path(repo or REPO_ROOT)
    full = hook_sha if _FULL_SHA.fullmatch(hook_sha) else _git(
        "rev-parse", "--verify", f"{hook_sha}^{{commit}}", cwd=repo).strip()
    return _hook_source_key_at(full, str(repo))


def _run_hook(binary: str, hint: str, command: str, timeout: float = 30) -> subprocess.CompletedProcess:
    """Send one Bash pre-tool event to ``binary hook --hint <hint>``."""
    event = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    return subprocess.run([binary, "hook", "--hint", hint], input=event, capture_output=True, text=True,
                          timeout=timeout)


def run_hook(binary: str, hint: str, command: str, *, timeout: float = 30) -> subprocess.CompletedProcess:
    return _run_hook(binary, hint, command, timeout)


def probe_hook(binary: str, hint: str) -> None:
    """Allow/deny-probe one hook binary in one hint mode; raises ``HookProbeFailed``."""
    _probe_hook(binary, hint)


def hook_source_key(hook_sha: str, repo: Path | None = None) -> str:
    return _hook_source_key(hook_sha, repo)


def require_strict_cli() -> None:
    """Raise ``ValueError`` when the Claude CLI cannot honor a strict run."""
    _require_strict_cli()


def build_failure_detail(error: BaseException) -> str:
    """A build error's message with its captured stderr, for ``CandidateBuildFailed`` and refusals."""
    detail = getattr(error, "stderr", None) or ""
    return f"{error}\n{detail}".strip()


def _hook_replay(hook_binary: str, command: str) -> bool:
    """Replay one Bash command through the hook; exit 2 denies, 0 allows, anything else is an error."""
    try:
        result = _run_hook(hook_binary, "native", command)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"strict audit could not run the hook {hook_binary}: {error}") from error
    if result.returncode not in (0, 2):
        raise RuntimeError(f"strict audit hook exited {result.returncode}: {result.stderr[-500:]}")
    return result.returncode == 2


_PROBE_ALLOW = "go test ./..."
_PROBE_DENY = "cat src/lib.rs"
# Text each hint's denial must contain: tilth names its MCP read tool, native names the host Read tool.
_PROBE_HINT_TEXT = {"tilth": "tilth_read", "native": "Read tool"}


def _probe_hook(binary: str, hint: str) -> None:
    for command, want_exit in ((_PROBE_ALLOW, 0), (_PROBE_DENY, 2)):
        try:
            result = _run_hook(binary, hint, command)
        except (OSError, subprocess.SubprocessError) as error:
            raise HookProbeFailed(f"strict hook probe could not run {binary}: {error}") from error
        problem = None
        if result.returncode != want_exit:
            problem = f"exited {result.returncode} on `{command}`, expected {want_exit}: {result.stderr.strip()[-300:]}"
        elif want_exit == 2 and _PROBE_HINT_TEXT[hint] not in result.stderr:
            problem = f"denial for `{command}` lacks the {hint!r} hint text {_PROBE_HINT_TEXT[hint]!r}"
        if problem:
            raise HookProbeFailed(f"strict hook {binary} (--hint {hint}) {problem}")


def probe_strict_hooks(mode_names, hook_binary: str) -> None:
    """Refuse a strict run, before any spend, when a hook binary cannot allow and deny correctly.

    Probes each distinct (binary, hint) pair that the modes register: a tilth arm's own binary with
    ``--hint tilth``, the baseline hook binary with ``--hint native``. Raises ``HookProbeFailed``.
    """
    pairs = {_strict_hook_spec(MODES[name], hook_binary) for name in mode_names}
    for binary, hint in sorted(pairs):
        _probe_hook(binary, hint)


def _result_text(content: object) -> str:
    if isinstance(content, list):
        content = "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content.lstrip() if isinstance(content, str) else ""


def _carries_hook_denial(content: object) -> bool:
    return _result_text(content).startswith("PreToolUse:Bash hook")


def _refused_before_running(content: object) -> bool:
    """Claude Code rejected the call itself (input validation, such as its long-``sleep`` guard)."""
    return _result_text(content).startswith("<tool_use_error>")


def _audit_strict_claude(raw: str, mode: ModeConfig, available_tools: list[str], hook_binary: str) -> int:
    available = set(available_tools)
    if mode.mcp_config_path:
        if "Bash" not in available or available & (_STRICT_NATIVE_FILE_TOOLS | {"Agent", "Task"}):
            raise RuntimeError("strict MCP arm exposed forbidden native tools or no Bash")
    elif not set(_STRICT_BASELINE_TOOLS) <= available or available & {"Agent", "Task"}:
        raise RuntimeError("strict baseline did not expose its file tools or exposed delegation")
    if not mode.mcp_config_path and any(name.startswith("mcp__") for name in available):
        raise RuntimeError("strict baseline exposed an MCP tool")

    # Pair tool_use with tool_result by id; an id-less call keys by stream position and has no
    # result to pair, so a denied one is ran-unknown and fails closed as hook_loosened.
    calls: dict[object, str] = {}
    results: dict[str, object] = {}
    for position, line in enumerate(raw.splitlines()):
        event = json.loads(line)
        message = event.get("message", {})
        for index, block in enumerate(message.get("content", [])):
            if block.get("type") == "tool_use":
                name = block.get("name", "")
                if name in {"Agent", "Task"} or (mode.mcp_config_path and name in _STRICT_NATIVE_FILE_TOOLS):
                    raise RuntimeError(f"strict cell attempted forbidden native tool: {name}")
                if name == "Bash":
                    key = block.get("id") or ("position", position, index)
                    calls[key] = block.get("input", {}).get("command", "")
            elif block.get("type") == "tool_result" and block.get("is_error") is True and block.get("tool_use_id"):
                results[block["tool_use_id"]] = block.get("content")
    # Replay through the hook at hook_sha, whichever hook the cell ran with: a call that ran and
    # that hook denies means the cell's own hook let through what the seed denies. A call did not run
    # when its error result text starts with Claude Code's `PreToolUse:Bash hook` prefix (a hook
    # denial, counted) or with `<tool_use_error>` (Claude Code refused it, not counted). Any other
    # error text, or none, means the command ran.
    denied = 0
    for call_id, command in calls.items():
        if not _hook_replay(hook_binary, command):
            continue
        if call_id in results and _refused_before_running(results[call_id]):
            continue
        if call_id not in results or not _carries_hook_denial(results[call_id]):
            raise HookLoosenedError(f"hook_loosened: strict cell ran a Bash command the hook denies: {command}")
        denied += 1
    return denied


CELL_TIMEOUT_S = 600
_SYNTHETIC_LANGUAGE = SYNTHETIC_LANGUAGE

_run_probe = baselines.run_probe


# Probed once per run when planning cell identities.
_probe_version = functools.lru_cache(maxsize=None)(_run_probe)


def cli_version(runner: str, *, fresh: bool = False) -> str | None:
    """Return the agent CLI version that a runner subprocess will use.

    ``fresh`` bypasses the per-run cache; the scheduler re-probes before each paid
    cell so a mid-run CLI update cannot mix versions under one planned key.
    """
    binary = resolve_claude_binary() if runner == "claude" else runner
    return (_run_probe if fresh else _probe_version)((binary, "--version"))


CLAUDE_PIN_FILE = BENCHMARK_DIR / "claude-cli.version"
CLAUDE_BINARY_ENV = "TILTH_BENCH_CLAUDE"
MISE_CLAUDE_ROOT = Path.home() / ".local" / "share" / "mise" / "installs" / "aqua-anthropics-claude-code"


class CliPinError(ValueError):
    """The Claude CLI a run would use is not the pinned version."""


def claude_cli_pin() -> str:
    return CLAUDE_PIN_FILE.read_text().strip()


def resolve_claude_binary() -> str:
    """The Claude CLI a run spawns: $TILTH_BENCH_CLAUDE, else the pinned mise install, else ``claude`` on PATH.

    A path-form override is made absolute, because a cell runs from a disposable workspace. mise names version
    directories with or without a ``v`` prefix, so both are tried.
    """
    override = os.environ.get(CLAUDE_BINARY_ENV)
    if override:
        return str(Path(override).resolve()) if os.sep in override else override
    pin = claude_cli_pin()
    for name in (f"v{pin}", pin):
        pinned = MISE_CLAUDE_ROOT / name / "claude"
        if pinned.is_file():
            return str(pinned)
    return "claude"


def require_pinned_cli(*, allow_drift: bool = False) -> str:
    """Refuse to plan a paid cell when the resolved Claude CLI is not the pinned version; return its description.

    ``--version`` needs no model call. The version is part of every cell's run key, so a drifted CLI would buy
    every stored cell again.
    """
    pin = claude_cli_pin()
    binary = resolve_claude_binary()
    found = cli_version("claude")
    match = re.match(r"\d+\.\d+\.\d+", found or "")
    version = match.group() if match else None
    line = f"claude CLI: {binary} -> {found!r} (pin {pin})"
    if version == pin:
        return line
    if allow_drift:
        return f"{line}; --allow-cli-drift: the run keys do not match the stored cells"
    raise CliPinError(
        f"{binary} reports {found!r}, but benchmark/claude-cli.version pins {pin}, so every stored cell's run key "
        f"would drift. Install {pin} (mise install aqua:anthropics/claude-code@{pin}), point {CLAUDE_BINARY_ENV} at "
        f"that binary, or pass --allow-cli-drift to run anyway and buy the cells again.")


_file_sha256 = baselines.file_sha256
_tree_sha256 = baselines.tree_sha256


def _task_timeout(task: Task) -> int:
    return task.timeout_s or CELL_TIMEOUT_S


def env_fingerprint(task_or_repo: object) -> str:
    """Fingerprint the task language's toolchains and its dependency lockfiles.

    A task supplies its own language and dependency hashes. A bare repo name
    resolves both through ``REPOS``.
    """
    if isinstance(task_or_repo, str):
        repo_name = task_or_repo
        language = _SYNTHETIC_LANGUAGE if repo_name == "synthetic" else REPOS[repo_name].language
        repo_path = get_repo_path(repo_name)
        hashes = baselines.local_dependency_hashes(lambda names: {
            name: _file_sha256(repo_path / name) for name in names if (repo_path / name).is_file()
        })
    else:
        language = task_or_repo.language
        hashes = task_or_repo.dependency_hashes()
    return baselines.host_env_fingerprint(language, hashes, probe=_probe_version)


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

    ``chains.ChainTask`` is skipped: ``chains.py`` holds every chain definition, so
    hashing it would move each chain's key when another chain changes. A chain keys
    on its members' digests and ``chains.CHAIN_PROTOCOL`` instead.
    """
    sources = {}
    for cls in type(task).__mro__:
        if cls is chains.ChainTask:
            continue
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
    return baselines.task_digest(
        prompt=task.prompt,
        ground_truth=asdict(ground_truth) if is_dataclass(ground_truth) else ground_truth,
        test_command=list(getattr(task, "test_command", ()) or ()),
        fixture_files=_task_fixture_files(task),
        repo_commit=REPOS[task.repo].commit_sha if task.repo in REPOS else None,
        mutations=[asdict(mutation) if is_dataclass(mutation) else mutation for mutation in mutations],
        hide_git=bool(getattr(task, "hide_git", False)),
        task_sources=_task_source_files(task),
        identity_inputs=task.identity_inputs(),
    )


_REPO_PLACEHOLDER = Path("<repo>")
_TILTH_BIN_PLACEHOLDER = "<tilth-bin>"
_BUDGET_PLACEHOLDER = "<max-budget>"
_PREAMBLE_KEY = "model_instructions_file="
_PREAMBLE_PLACEHOLDER = _PREAMBLE_KEY + "<preamble-file>"


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
    tilth binary path (keyed by its SHA-256), the strict Bash hook's binary
    path (the baseline hook binary, keyed by the hook source key), and the task prompt (keyed
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
            strict_hook_binary=_HOOK_BIN_PLACEHOLDER,
        )
    except (ValueError, RuntimeError) as error:
        # The cell fails when it runs; key it by the reason it cannot be built.
        return [f"unbuildable: {error}"]
    # The digest records max_budget_usd on its own; the budget format must not change the key.
    # The system prompt keys the preamble text, so its content-addressed file path does not.
    return [_BUDGET_PLACEHOLDER if cmd[i - 1] == "--max-budget-usd"
            else _PREAMBLE_PLACEHOLDER if part.startswith(_PREAMBLE_KEY)
            else part for i, part in enumerate(cmd)]


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
    hook_sha: str | None = None,
    hook_repo: Path | None = None,
) -> dict:
    """Return the run key, the key-input fields, and the harness variant flags one cell's row records.

    A strict cell needs ``hook_sha``, the commit (of ``hook_repo``, default this checkout) whose hook
    guards the baseline arm and replays every Bash call; its hook source key joins the harness digest.
    """
    if strict_file_tools and hook_sha is None:
        raise ValueError("strict_file_tools requires hook_sha")
    hook_source_key = _hook_source_key(hook_sha, hook_repo) if strict_file_tools else None
    task = TASKS[task_name]
    mode = MODES[mode_name]
    runner = RUNNERS[model_name]
    if strict_file_tools:
        tools = list(_STRICT_BASELINE_TOOLS) if not mode.mcp_config_path else ["Bash"]
    else:
        tools = list(mode.tools)
    identity = {
        "harness_digest": baselines.harness_digest(
            system_prompt=_base_prompt(mode) + ("\n" + _STRICT_SYSTEM_GUIDANCE if strict_file_tools else ""),
            tools=tools,
            strict_file_tools=strict_file_tools,
            bare=bare,
            max_budget_usd=max_budget_usd if runner == "claude" else None,
            mcp_shape=_mcp_shape(mode, runner, mode_name),
            command=_command_template(
                task, mode, mode_name, model_name, bare=bare, reasoning_effort=reasoning_effort,
                max_budget_usd=max_budget_usd, strict_file_tools=strict_file_tools,
            ),
            hook_source_key=hook_source_key,
        ),
        "task_digest": _cell_task_digest(task),
        "env_fingerprint": env_fingerprint(task),
        "cli_version": cli_version(runner),
        "timeout_s": _task_timeout(task),
        **({"git_sha": mode.git_sha, "binary_sha256": mode.binary_sha256} if _is_tilth_arm(mode) else {}),
        **({"plugin_sha256": mode.plugin_sha256} if mode.plugin_dir else {}),
        **({"preamble_sha256": hashlib.sha256(_base_prompt(mode).encode("utf-8")).hexdigest()}
           if mode.prompt_mode == "preamble" else {}),
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
    return {"run_key": baselines.run_key(key_inputs), **identity, **variant_flags,
            **({"hook_source_key": hook_source_key} if strict_file_tools else {})}


TRAJECTORY_RUNNERS = frozenset({"claude", "codex"})


def write_trajectory(stream_log_path: Path | None, runner: str) -> str | None:
    """Write the tool-call sidecar beside a teed claude or codex stream."""
    if runner not in TRAJECTORY_RUNNERS or stream_log_path is None or not stream_log_path.is_file():
        return None
    calls = extract_trajectory(stream_log_path.read_text(), runner)
    sidecar = stream_log_path.with_name(f"{stream_log_path.stem}.trajectory.jsonl")
    sidecar.write_text("".join(json.dumps(call) + "\n" for call in calls))
    return str(sidecar)


def contamination_fields(trajectory_path: str | None, task: object, cwd: str | Path | None = None) -> dict:
    """The ``contaminated`` flag and its hits for a row's trajectory sidecar."""
    hits = external.contamination.find_hits(trajectory_path, task, cwd)
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
    hook_sha: str | None = None,
    hook_repo: Path | None = None,
) -> dict:
    """Run one benchmark cell in the task's configured agent workspace."""
    task = TASKS[task_name]
    if isinstance(task, chains.ChainTask):
        return _run_chain(
            task, mode_name, model_name, repetition, verbose=verbose, stream_log_path=stream_log_path,
            bare=bare, reasoning_effort=reasoning_effort, max_budget_usd=max_budget_usd,
            strict_file_tools=strict_file_tools, hook_sha=hook_sha, hook_repo=hook_repo,
        )
    workspace = (
        _built_workdir() if task.fixture_path is None
        else _agent_repo(get_repo_path(task.repo), task.hide_git)
    )
    with workspace as repo_path, _cell_claude_config(model_name, MODES[mode_name]) as config_dir:
        task.prepare(repo_path)
        with erosion.snapshot(repo_path) as baseline:
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
                hook_sha=hook_sha,
                hook_repo=hook_repo,
                claude_config_dir=config_dir,
                baseline=baseline,
            )


def _chain_turn_argv(cmd: list[str], resume_session: str | None) -> list[str]:
    """Keep the session of a chain turn on disk, and resume the earlier turn's session."""
    argv = [part for part in cmd if part != "--no-session-persistence"]
    if resume_session is not None:
        at = argv.index("-p") + 1
        argv[at:at] = ["--resume", resume_session]
    return argv


def _append_turn_log(stream_log_path: Path | None, turn_log: Path | None) -> None:
    """Add one turn's stream to the chain's stream file, so failure costing reads the whole chain."""
    if stream_log_path is None or turn_log is None or not turn_log.is_file():
        return
    with stream_log_path.open("a") as out:
        out.write(turn_log.read_text())


def _run_chain(
    task: chains.ChainTask,
    mode_name: str,
    model_name: str,
    repetition: int,
    *,
    verbose: bool = False,
    stream_log_path: Optional[Path] = None,
    bare: bool = False,
    reasoning_effort: str | None = None,
    max_budget_usd: float = DEFAULT_MAX_BUDGET_USD,
    strict_file_tools: bool = False,
    hook_sha: str | None = None,
    hook_repo: Path | None = None,
) -> dict:
    """Run a chain's tasks as turns of one claude session in one workspace.

    ``max_budget_usd`` is the budget of one member; the chain may spend the sum, and each turn is
    capped at what the earlier turns left. Each member is prepared and graded with its own
    methods, right after its turn. A wrong sub-task does not stop the next turn; an exception does.
    """
    if RUNNERS[model_name] != "claude":
        raise ValueError("a chained cell needs a Claude model: only claude -p resumes a session")
    chain_budget = max_budget_usd * task.size
    identity = cell_identity(
        task.name, mode_name, model_name, repetition,
        bare=bare, reasoning_effort=reasoning_effort,
        max_budget_usd=max_budget_usd, strict_file_tools=strict_file_tools,
        hook_sha=hook_sha, hook_repo=hook_repo,
    )
    workspace = (
        _built_workdir() if task.fixture_path is None
        else _agent_repo(get_repo_path(task.repo), task.hide_git)
    )
    rows: list[dict] = []
    spent = 0.0
    session_id: str | None = None
    with workspace as repo_path, _cell_claude_config(model_name, MODES[mode_name]) as config_dir:
        for turn, member in enumerate(task.members, start=1):
            remaining = chain_budget - spent
            if remaining <= 0:
                raise RuntimeError(f"chain {task.name} spent its ${chain_budget:.4f} before {member.name}")
            member.prepare(repo_path)
            turn_log = (
                stream_log_path.with_name(f"{stream_log_path.stem}.turn{turn}.jsonl")
                if stream_log_path is not None else None
            )
            with erosion.snapshot(repo_path) as baseline:
                try:
                    row = _run_single_in_repo(
                        member.name, mode_name, model_name, repetition, repo_path,
                        verbose=verbose, stream_log_path=turn_log, bare=bare,
                        reasoning_effort=reasoning_effort, max_budget_usd=remaining,
                        strict_file_tools=strict_file_tools, hook_sha=hook_sha, hook_repo=hook_repo,
                        claude_config_dir=config_dir, baseline=baseline,
                        chain_turn=True, resume_session=session_id,
                    )
                finally:
                    _append_turn_log(stream_log_path, turn_log)
            rows.append(row)
            spent += row["total_cost_usd"]
            session_id = row.get("session_id") or session_id
    return chains.chain_row(task, rows, identity=identity, chain_budget=chain_budget)


_CODEX_GUIDANCE = (
    "Do not discover, read, or invoke host skills or external agent guidance. "
    "Batch independent source reads in one call when the tool supports it."
)
_CODEX_TILTH_GUIDANCE = (
    " Use tilth MCP first for source discovery, reads, and writes. "
    "Batch independent files into one tilth call. "
    "Use shell only for tests and builds. Native tools remain available."
)


def _codex_command(
    task: object,
    mode: ModeConfig,
    model_id: str,
    repo_path: Path,
    *,
    reasoning_effort: str | None,
    skill_paths: list[str],
) -> list[str]:
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
    if mode.prompt_mode == "preamble":
        # As `tilth codex` does: the preamble replaces Codex's model instructions, not the developer message.
        cmd += ["-c", f"{_PREAMBLE_KEY}{json.dumps(str(_preamble_file(_base_prompt(mode))))}"]
        lead = ""
    else:
        lead = f"{SYSTEM_PROMPT}\n"
    instructions = f"{lead}Your current working directory is: {repo_path}\n{_CODEX_GUIDANCE}"
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
    return cmd


def _opencode_command(
    task: object,
    mode: ModeConfig,
    mode_name: str,
    model_id: str,
    repo_path: Path,
    *,
    bare: bool,
) -> tuple[list[str], str]:
    opencode_config = mode.opencode_config_path or OPENCODE_CONFIGS.get(mode_name)
    if opencode_config is None:
        raise RuntimeError(
            f"opencode runner has no config for mode '{mode_name}'. "
            f"Supported modes: {', '.join(sorted(OPENCODE_CONFIGS))}."
        )
    # opencode has no --system-prompt; prepend like codex. OPENCODE_CONFIG
    # (set by the caller) selects the MCP servers; --dangerously-skip-permissions
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
    return cmd, opencode_config


@dataclass(frozen=True)
class _ClaudeArmFlags:
    """The arm-shape facts that decide a Claude argv."""

    skill: bool
    woz: bool
    mcp: bool
    append_prompt: bool


def _claude_arm_flags(mode: ModeConfig) -> _ClaudeArmFlags:
    return _ClaudeArmFlags(
        skill=_is_skill_arm(mode),
        woz=_is_woz_arm(mode),
        mcp=bool(mode.mcp_config_path),
        append_prompt=mode.prompt_mode == "append",
    )


def _claude_command(
    task: object,
    mode: ModeConfig,
    model_id: str,
    repo_path: Path,
    *,
    bare: bool,
    reasoning_effort: str | None,
    max_budget_usd: float,
    strict_file_tools: bool,
    strict_hook_binary: str | None,
) -> list[str]:
    arm = _claude_arm_flags(mode)
    cmd = [
        "claude", "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--model", model_id,
        "--max-budget-usd", f"{max_budget_usd:.4f}",
    ]
    if not arm.skill:
        # Live probe 1: with this flag a skill arm's plugin loads but its skills do not (see
        # benchmark-harness-gotchas.md). Only a skill arm drops it, so every other arm's argv is unchanged.
        cmd.append("--disable-slash-commands")
    cmd += [
        "--disallowedTools", ",".join(("Agent", "Task", *sorted(_STRICT_NATIVE_FILE_TOOLS))) if strict_file_tools and arm.mcp else "Agent,Task",
        "--no-session-persistence",
        "--dangerously-skip-permissions",
        "--strict-mcp-config",
    ]
    cwd_prompt = f"Your current working directory is: {repo_path}" + ("\n" + _STRICT_SYSTEM_GUIDANCE if strict_file_tools else "")
    if arm.append_prompt:
        # Keep claude's own system prompt (and its native skill handling); add only the cwd line.
        cmd += ["--append-system-prompt", cwd_prompt]
    else:
        # A preamble arm replaces the system prompt with the preamble, as `tilth claude` does.
        cmd += ["--system-prompt", _base_prompt(mode) + "\n" + cwd_prompt]

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
        cmd += ["--plugin-dir", mode.plugin_dir]
    if arm.woz:
        cmd += ["--agent", "woz:code"]

    if strict_file_tools:
        cmd += ["--settings", _strict_bash_settings(*_strict_hook_spec(mode, strict_hook_binary))]
    tools_list = (list(_STRICT_BASELINE_TOOLS) if not arm.mcp else ["Bash"]) if strict_file_tools else list(mode.tools)
    if arm.skill and tools_list and "Skill" not in tools_list:
        tools_list.append("Skill")

    # --tools "" disables all built-ins (tilth_forced); --tools "a,b,c" allowlists; absent = default
    if tools_list:
        cmd += ["--tools", ",".join(tools_list)]
    elif arm.mcp:
        cmd += ["--tools", ""]

    if arm.mcp:
        cmd += ["--mcp-config", json.dumps(claude_mcp_config(mode))]

    cmd += ["--", task.prompt]
    return cmd


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
    strict_hook_binary: str | None = None,
) -> tuple[list[str], Optional[str]]:
    """Build one cell's runner argv and, for opencode, the config it selects."""
    model_id = MODELS[model_name]
    runner = RUNNERS[model_name]
    if runner == "codex":
        if mode_name == "tilth_forced":
            raise ValueError("Codex cannot enforce tilth_forced tool restrictions")
        cmd = _codex_command(
            task, mode, model_id, repo_path,
            reasoning_effort=reasoning_effort, skill_paths=skill_paths,
        )
        return cmd, None
    if runner == "opencode":
        return _opencode_command(task, mode, mode_name, model_id, repo_path, bare=bare)
    cmd = _claude_command(
        task, mode, model_id, repo_path,
        bare=bare, reasoning_effort=reasoning_effort, max_budget_usd=max_budget_usd,
        strict_file_tools=strict_file_tools, strict_hook_binary=strict_hook_binary,
    )
    return cmd, None


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
    hook_sha: str | None = None,
    hook_repo: Path | None = None,
    claude_config_dir: Path | None = None,
    baseline: Path | None = None,
    chain_turn: bool = False,
    resume_session: str | None = None,
) -> dict:
    """Execute and grade one benchmark cell.

    ``baseline`` is the workspace snapshot taken before the agent ran; without one the
    workspace diff is not measured.
    ``chain_turn`` keeps the claude session on disk and adds ``session_id`` to the row;
    ``resume_session`` resumes the session of the chain's earlier turn.
    """
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
        hook_sha=hook_sha, hook_repo=hook_repo,
    )
    hook_binary = (
        build_candidate(hook_sha, repo=hook_repo).binary_path if strict_file_tools and runner == "claude" else None
    )

    if runner == "codex":
        skill_paths = _codex_skill_paths(repo_path, os.environ)
        skill_roots = [str(path) for path in _codex_skill_roots(repo_path, os.environ)]
    cmd, opencode_config = _runner_command(
        task, mode, mode_name, model_name, repo_path,
        bare=bare, reasoning_effort=reasoning_effort, max_budget_usd=max_budget_usd,
        strict_file_tools=strict_file_tools, skill_paths=skill_paths, strict_hook_binary=hook_binary,
    )

    if chain_turn:
        if runner != "claude":
            raise ValueError("a chained cell needs a Claude model: only claude -p resumes a session")
        cmd = _chain_turn_argv(cmd, resume_session)

    if verbose:
        print(f"    Running: {' '.join(cmd)}")

    # Build a fresh allowlist for every runner subprocess. Runner-specific
    # config is added only by the lane that consumes it.
    agent_venv = _agent_venv(task, repo_path)
    env = build_runner_env(
        runner,
        opencode_config=opencode_config,
        bare=bare,
        tilth_bin=mode.binary_path,
        language=task.language,
        agent_venv=agent_venv,
    )
    if runner == "claude":
        cmd = [resolve_claude_binary(), *cmd[1:]]
    cmd = _host_runner_argv(cmd, env, agent_venv)
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
        auth_error: auth.ClaudeAuthError | None = None
        gate = auth.InitGate() if runner == "claude" else None

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
                    if gate is not None:
                        try:
                            event = json.loads(line)
                            if isinstance(event, dict):
                                gate.observe(event)
                        except ValueError:
                            continue
                        except auth.ClaudeAuthError as error:
                            auth_error = error
                            proc.kill()
                            break
            proc.wait()
            stderr_thread.join()
        finally:
            timer.cancel()
            proc.stdout.close()
            stderr_pipe.close()

        stderr_text = "".join(stderr_chunks)
        if auth_error is not None:
            raise auth_error
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
        if runner == "claude":
            auth.check_captured(result.stdout)
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

    if runner == "claude" and _is_skill_arm(mode):
        _require_plugin_skills(mode_name, mode, run_result)

    strict_denied_bash_calls = (
        _audit_strict_claude(result.stdout, mode, run_result.available_tools, hook_binary)
        if strict_file_tools and runner == "claude" else 0
    )

    # Override duration if needed (subprocess timing may be more accurate)
    if run_result.duration_ms == 0:
        run_result.duration_ms = elapsed_ms

    # Erosion fields are measured before grading, which restores and patches test files.
    lint_ok, lint_reason = erosion.lint_status(task.lint_command, repo_path)
    workspace_diff_lines = (
        erosion.workspace_diff_lines(baseline, repo_path) if baseline is not None else None
    )

    # Check correctness
    correct, reason = task.check_correctness(
        run_result.result_text,
        str(repo_path),
    )
    run_result.correct = correct
    run_result.correctness_reason = reason
    details = task.grade_details()

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
        "resolved_model": run_result.resolved_model,
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
        **({"skills": run_result.skills, "plugins": run_result.plugins,
            "plugin_errors": run_result.plugin_errors, "prompt_mode": mode.prompt_mode}
           if _uses_claude_session_features(mode) else {}),
        **_skill_audit_fields(mode, run_result),
        "model_usage": run_result.model_usage,
        "batch_sizes": tool_batch_sizes(run_result),
        "op_kinds": tool_op_kinds(run_result),
        "cost_source": run_result.cost_source,
        "trajectory_path": trajectory_path,
        **contamination_fields(trajectory_path, task, repo_path),
        **details,
        "workspace_diff_lines": workspace_diff_lines,
        "reference_changed_lines": task.reference_changed_lines,
        "lint_ok": lint_ok,
        "lint_reason": lint_reason,
        "reused": False,
        **({"session_id": run_result.session_id} if chain_turn else {}),
        **identity,
    }


def select_models(value: str | None, runner: str | None) -> list[str]:
    """Keep a selected runner from scheduling another provider's models.

    ``all`` skips ``PINNED_MODELS``; a pinned name runs only when named.
    """
    requested = value or ("gpt5" if runner == "codex" else "sonnet")
    aliases = {name: model for name, model in MODELS.items() if runner is None or RUNNERS[name] == runner}
    if requested.lower() == "all":
        return [name for name in aliases if name not in PINNED_MODELS]
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
        return [name for name, task in TASKS.items()
                if not isinstance(task, (external.ExternalTask, chains.ChainTask))]
    for name in (item.strip() for item in value.split(",")):
        if not name or name in TASKS:
            continue
        if name.startswith(chains.PREFIX):
            chains.register(TASKS, name, digest=_cell_task_digest)
            continue
        resolved, verdict = external.resolve_task(name)
        if resolved is not None:
            TASKS[name] = resolved
        elif verdict is not None:
            raise ValueError(f"external task {name} is not admitted: {verdict.reason}")
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
        and row.get("reused") is not True and baselines.is_completed(row) and isinstance(row.get("total_cost_usd"), (int, float))
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
    """``run_plan`` stopped before a cell for one of these reasons.

    ``ceiling``: the spend ceiling. ``quota``: a usage limit.
    ``cli-version``: the agent CLI changed, or stopped answering ``--version``, since the cell was planned.
    ``auth``: a cell's init event failed the auth gate; the cell is stored as infra ``auth`` and charged.

    After the failed cell is stored, it stops for ``mcp-unavailable``, ``skill-unavailable`` or ``invalid-cell``.
    Those are config-level failures that main aborts on.
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


class HookProbeFailed(CandidateBuildFailed):
    """A strict hook binary failed its allow/deny probe; ``run_plan`` raises it before any spend."""


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
        try:
            built = json.loads(record.read_text())
        except json.JSONDecodeError:
            built = {}  # a torn record counts as a missing cache: rebuild
        if built.get("binary_sha256") == _file_sha256(binary):
            return CandidateBuild(git_sha=sha, binary_path=str(binary), binary_sha256=built["binary_sha256"])
    # A stable checkout path per sha; the target dir is shared so dependencies build once.
    worktree = directory / "src"
    if worktree.exists():
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=repo, capture_output=True)
        shutil.rmtree(worktree, ignore_errors=True)
    subprocess.run(["git", "worktree", "prune"], cwd=repo, capture_output=True)  # drop a stale registration
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


def discard_candidate_build(sha: str) -> None:
    """Delete the kept binary and record of the full ``sha`` and forget its cache entry; the shared target stays.

    A later ``build_candidate(sha)`` rebuilds. Only a 40-hex sha is accepted, so no argument can name
    ``candidates/target`` or a path outside ``candidates/``.
    """
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError(f"discard_candidate_build needs a full 40-hex sha, got {sha!r}")
    _CANDIDATE_BUILDS.pop(sha, None)
    shutil.rmtree(RESULTS_DIR / "candidates" / sha, ignore_errors=True)


def candidate_mode(mode: ModeConfig, build: CandidateBuild) -> ModeConfig:
    return replace(mode, binary_path=build.binary_path, git_ref=build.git_sha, git_sha=build.git_sha,
                   binary_sha256=build.binary_sha256)


PREAMBLE_FILE = "prompts/preamble.md"


def read_preamble(sha: str | None = None, *, repo: Path | None = None) -> str:
    """The launcher preamble of the tree under test: ``prompts/preamble.md`` at the commit ``sha`` of ``repo``,
    or in the ``repo`` checkout when ``sha`` is None (default repo: this checkout).

    Raises ``OSError`` or ``subprocess.CalledProcessError`` when the file is absent.
    """
    repo = Path(repo or REPO_ROOT)
    if sha is None:
        return (repo / PREAMBLE_FILE).read_text(encoding="utf-8")
    return subprocess.run(["git", "show", f"{sha}:{PREAMBLE_FILE}"], cwd=repo, check=True,
                          capture_output=True).stdout.decode("utf-8")


def preamble_mode(mode: ModeConfig, text: str) -> ModeConfig:
    """``mode`` with ``text`` as its whole system prompt, in place of ``SYSTEM_PROMPT``."""
    return replace(mode, prompt_mode="preamble", preamble=text)


def _base_prompt(mode: ModeConfig) -> str:
    """The system prompt text of an arm: its preamble for a preamble arm, else ``SYSTEM_PROMPT``."""
    if mode.prompt_mode != "preamble":
        return SYSTEM_PROMPT
    if mode.preamble is None:
        raise ValueError(f"mode '{mode.name}' is a preamble arm with no preamble text")
    return mode.preamble


def _preamble_file(text: str) -> Path:
    """A content-addressed copy of ``text`` under the results dir, for Codex's ``model_instructions_file``."""
    data = text.encode("utf-8")
    path = RESULTS_DIR / "configs" / "preambles" / f"preamble-{hashlib.sha256(data).hexdigest()}.md"
    if not path.is_file() or path.read_bytes() != data:
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        partial.write_bytes(data)
        partial.replace(path)
    return path


def planned_identity(cell: CellSpec, *, max_budget_usd: float = DEFAULT_MAX_BUDGET_USD,
                     strict_file_tools: bool = False, hook_sha: str | None = None,
                     hook_repo: Path | None = None) -> dict:
    """The run key and key inputs of one ``run_plan`` cell: a bare harness at ``max_budget_usd``.

    ``strict_file_tools`` guards Bash with the hook built at ``hook_sha``.
    """
    return cell_identity(cell.task, cell.mode, cell.model, cell.repetition, bare=True, reasoning_effort=None,
                         max_budget_usd=max_budget_usd, strict_file_tools=strict_file_tools,
                         hook_sha=hook_sha, hook_repo=hook_repo)


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


def _register_chains(task_names) -> None:
    """Register each ``chain:<id>`` name in ``TASKS``; other names are left to their own loaders."""
    for name in task_names:
        if name.startswith(chains.PREFIX):
            chains.register(TASKS, name, digest=_cell_task_digest)


def check_baseline_drift(cells, *, panel: panels.Panel, max_budget_usd: float = DEFAULT_MAX_BUDGET_USD,
                         strict_file_tools: bool = False, hook_sha: str | None = None,
                         hook_repo: Path | None = None) -> None:
    """Raise ``BaselineDrift`` when a stock-arm cell of ``cells`` that the store cannot answer drifted; runs nothing."""
    panel.register(TASKS)
    specs = [CellSpec(*cell) for cell in cells]
    _register_chains(cell.task for cell in specs)
    planned = [(cell, planned_identity(cell, max_budget_usd=max_budget_usd, strict_file_tools=strict_file_tools,
                                       hook_sha=hook_sha, hook_repo=hook_repo))
               for cell in specs]
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
    cell_estimate_usd: float | None = None,
    max_budget_usd: float = DEFAULT_MAX_BUDGET_USD,
    store_only: bool = False,
    repo: Path | None = None,
    strict_file_tools: bool = False,
    hook_sha: str | None = None,
    preamble: bool = False,
) -> list[dict]:
    """Run ``cells`` for one panel, answering each from the result store when it can.

    Registers the panel's members in ``TASKS`` before planning any cell.
    Merges ``panel.stamp(task)`` into every row before ``baselines.store``.
    Tilth arms use the binary built at ``candidate_sha``, which ``repo`` holds (default: this checkout).
    Every cell is keyed and run at the per-cell Claude budget ``max_budget_usd``; ``cell_estimate_usd``
    (default: ``max_budget_usd``) estimates a cell with no stored or earlier cost.
    ``store_only`` returns only stored rows and never starts a runner.
    Every row returned is also appended to ``output``.
    ``strict_file_tools`` runs every cell with Bash guarded by the hook: a tilth arm runs its own binary's hook,
    the baseline runs the hook built at ``hook_sha`` (a commit of ``repo``), and that hook replays every Bash call.
    ``preamble`` gives every tilth arm ``prompts/preamble.md`` as its system prompt, read at ``candidate_sha``
    (or from the ``repo`` checkout when ``candidate_sha`` is None).

    Raises ``CandidateBuildFailed`` when ``candidate_sha`` or ``hook_sha`` does not build, ``hook_sha`` has no
    ``src/hook``, or ``preamble`` finds no preamble file; its subclass ``HookProbeFailed`` when a strict hook binary fails the allow/deny probe.
    Raises ``ValueError`` when ``strict_file_tools`` meets a non-Claude cell or a Claude CLI older than the
    minimum for exec-form hooks.
    Raises ``BaselineDrift`` before any cell when a stock-arm cell drifted without ``refreeze_baselines``.
    Raises ``PlanStopped`` when the ledger or a usage limit stops it before a cell.
    """
    panel.register(TASKS)
    cells = [CellSpec(*cell) for cell in cells]
    _register_chains(cell.task for cell in cells)
    if strict_file_tools:
        if hook_sha is None:
            raise ValueError("strict_file_tools requires hook_sha")
        if any(RUNNERS[cell.model] != "claude" for cell in cells):
            raise ValueError("strict_file_tools requires Claude models")
        if not store_only:
            _require_strict_cli()
        try:
            _hook_source_key(hook_sha, repo)
            if not store_only:
                hook_binary = build_candidate(hook_sha, repo=repo).binary_path
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            raise CandidateBuildFailed(build_failure_detail(error)) from error
    saved: dict[str, ModeConfig] = {}
    build = None
    if candidate_sha is not None:
        try:
            build = build_candidate(candidate_sha, repo=repo)
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            raise CandidateBuildFailed(build_failure_detail(error)) from error
    text = None
    if preamble:
        try:
            text = read_preamble(build.git_sha if build else None, repo=repo)
        except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as error:
            raise CandidateBuildFailed(f"cannot read {PREAMBLE_FILE}: {error}") from error
    if build is not None or text is not None:
        for name in {cell.mode for cell in cells if _is_tilth_arm(MODES[cell.mode])}:
            saved[name] = MODES[name]
            if build is not None:
                MODES[name] = candidate_mode(MODES[name], build)
            if text is not None:
                MODES[name] = preamble_mode(MODES[name], text)
    try:
        if strict_file_tools and not store_only:
            probe_strict_hooks({cell.mode for cell in cells}, hook_binary)
        return _run_plan(cells, panel=panel, ledger=ledger, refreeze_baselines=refreeze_baselines, output=output,
                         cell_estimate_usd=max_budget_usd if cell_estimate_usd is None else cell_estimate_usd,
                         max_budget_usd=max_budget_usd, store_only=store_only,
                         strict_file_tools=strict_file_tools, hook_sha=hook_sha, hook_repo=repo)
    finally:
        MODES.update(saved)


def _run_plan(cells: list[CellSpec], *, panel, ledger: SpendLedger, refreeze_baselines: bool,
              output: Path | None, cell_estimate_usd: float, max_budget_usd: float, store_only: bool,
              strict_file_tools: bool = False, hook_sha: str | None = None,
              hook_repo: Path | None = None) -> list[dict]:
    planned = [(cell, planned_identity(cell, max_budget_usd=max_budget_usd, strict_file_tools=strict_file_tools,
                                       hook_sha=hook_sha, hook_repo=hook_repo)) for cell in cells]
    store_path = RESULTS_DIR / baselines.STORE_FILENAME
    history = baselines.load_rows(store_path)
    reusable = baselines.completed_by_key(history)
    pending = [(cell, identity) for cell, identity in planned if identity["run_key"] not in reusable]
    if pending and not store_only:
        auth.preflight_claude_auth(
            claude_auth_targets([(cell.task, cell.mode, cell.model) for cell, _ in pending], bare=True))
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
        ledger.charge(amount, source=row.get("cost_source") or "native", run_key=row.get("run_key"))
        if row.get("infra") != "prepare":
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
            _store_reused_panel_row({**stored, **scanned}, stamp, store_path=store_path, history=history)
            emit({**stored, **scanned, **stamp, "variant": _variant_metadata(mode), "charged_usd": 0.0,
                  "reused": True})
            continue
        if store_only:
            continue
        cell_id = f"{cell.task}/{cell.mode}/{cell.model}/rep{cell.repetition}"
        size = getattr(task, "size", 1)
        estimate = estimate_cell_cost(history, task=cell.task, mode=cell.mode, model=MODELS[cell.model],
                                      run_max_cost=run_max_cost, fallback=cell_estimate_usd * size)
        try:
            crossing = ledger.would_cross(estimate)
        except ValueError as error:
            raise PlanStopped("ceiling", str(error), rows) from error
        if crossing:
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
        stream_log_path = stream_dir / (f"{identity['run_key'][:16]}_{cell.task.replace(':', '_')}_{cell.mode}"
                                        f"_rep{cell.repetition}_{attempt}.jsonl")
        # Any runner stops without unreserved headroom. The run key pins the run's budget; a claude cell's
        # --max-budget-usd cap shrinks to the headroom, so an overrunning cell cannot spend the finish reserve.
        try:
            headroom = ledger.headroom()
            exhausted = ledger.exhausted()
        except ValueError as error:
            raise PlanStopped("ceiling", str(error), rows) from error
        if exhausted:
            raise PlanStopped("ceiling", f"${ledger.spent:.4f} spent + ${ledger.reserve:.4f} reserved leaves no "
                                         f"budget for {cell_id} under ${ledger.max_usd}", rows)
        cell_cap = max_budget_usd
        if runner == "claude" and headroom is not None:
            cell_cap = min(max_budget_usd, headroom / size)
        applied_cap = {"applied_max_budget_usd": cell_cap} if runner == "claude" else {}
        metadata = {
            "task": cell.task, "mode": cell.mode, "model": MODELS[cell.model], "model_alias": cell.model,
            **({"max_budget_usd": max_budget_usd} if runner == "claude" else {}),
            "capability": getattr(task, "capability", None), "repetition": cell.repetition,
            "variant": _variant_metadata(mode), **erosion.UNMEASURED_FIELDS, **_SKILL_AUDIT_UNMEASURED,
            **stamp, **identity, **applied_cap,
            "reused": False,
        }

        def fail(fields: dict, charge: float | None = None) -> None:
            cost, source = failed_cell_cost(_read_stream(stream_log_path), runner, estimate, MODELS[cell.model])
            settle({**metadata, **fields, "correct": False,
                    **({"total_cost_usd": cost} if source != "estimate" else {}),
                    "cost_source": source,
                    "trajectory_path": write_trajectory(stream_log_path, runner)},
                   charge if charge is not None else cost)

        try:
            result = run_single(cell.task, cell.mode, cell.model, cell.repetition, stream_log_path=stream_log_path,
                                bare=True, max_budget_usd=cell_cap,
                                **({"strict_file_tools": True, "hook_sha": hook_sha, "hook_repo": hook_repo}
                                   if strict_file_tools else {}))
        except InvalidCodexCellError as error:
            fail({"error": f"invalid_codex_cell: {error}", "correctness_reason": f"Invalid cell: {error}"})
            raise PlanStopped("invalid-cell", str(error), rows) from error
        except ArmUnavailableError as error:
            fail({"error": f"{error.kind}_unavailable: {error}", "correctness_reason": f"Exception: {error}"})
            raise PlanStopped(f"{error.kind}-unavailable", str(error), rows) from error
        except subprocess.TimeoutExpired:
            fail({"error": "timeout", "timed_out": True, "correctness_reason": "Subprocess timed out"})
        except (external.task.EnvBuildError, external.task.PrepareError) as error:
            # As in main: no model call ran, so the cell estimate is not charged.
            fail({"infra": "prepare", "error": f"infra:prepare: {error}",
                  "correctness_reason": f"Prepare failed: {error}"}, charge=0.0)
        except auth.ClaudeAuthError as error:
            fail({"infra": "auth", "error": f"infra:auth: {error}", "correctness_reason": f"Auth refused: {error}"})
            raise PlanStopped("auth", str(error), rows) from error
        except Exception as error:
            quota = str(error) if isinstance(error, QuotaExhaustedError) else detect_quota_rejection(
                _read_stream(stream_log_path))
            if quota:
                fail({"infra": "quota", "error": f"infra:quota: {quota}", "correctness_reason": f"Usage limit: {quota}"})
                raise PlanStopped("quota", quota, rows) from error
            fail({"error": str(error), "correctness_reason": f"Exception: {error}"})
            if cell_cap < max_budget_usd and "error_max_budget_usd" in str(error):
                raise PlanStopped("ceiling", f"{cell_id} hit its ${cell_cap:.4f} cap at the unreserved headroom",
                                  rows) from error
        else:
            result.update({**stamp, **identity, **applied_cap, "variant": _variant_metadata(mode), "reused": False})
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


def _store_reused_panel_row(stored: dict, stamp: dict, *, store_path: Path, history: list[dict] | None = None) -> None:
    """Record a panel's stamp for a reused row, so the panel's split lock sees it.

    The copy costs nothing and keeps the stored run key and outcome, so reuse lookup is unchanged.
    No row for the run key that already carries this panel's name and split digest is stored again.
    A caller that holds the store rows passes them as ``history``; the new row joins that list.
    """
    if not stamp:
        return
    fields = ("panel_name", "panel_split_digest")

    def carries(row: dict) -> bool:
        return row.get("run_key") == stored.get("run_key") and all(row.get(key) == stamp.get(key) for key in fields)

    if carries(stored) or any(carries(row) for row in (baselines.load_rows(store_path) if history is None else history)):
        return
    copy = {**stored, **stamp, "charged_usd": 0.0, "reused": True}
    baselines.store(copy, path=store_path)
    if history is not None:
        history.append(copy)


_MODE_FIELDS = frozenset(field.name for field in _dataclass_fields(ModeConfig))
_MODE_REQUIRED = frozenset({"name", "tools", "description"})
# The caller sets policy only. Every other ModeConfig field is provenance the harness computes.
_MODE_CALLER_FIELDS = frozenset({"name", "tools", "description", "plugin_dir", "prompt_mode", "mcp_config_path"})


def _validate_plugin_arm(path: Path, mode: ModeConfig) -> ModeConfig:
    """Check a plugin arm's manifest and return it stamped with the plugin tree digest."""
    if mode.mcp_config_path:
        raise ValueError(f"--modes-json {path}: mode '{mode.name}' sets plugin_dir with mcp_config_path; "
                         "a skill arm serves no MCP server")
    if not os.path.isabs(mode.plugin_dir):
        raise ValueError(f"--modes-json {path}: mode '{mode.name}' plugin_dir must be an absolute path")
    try:
        declared = _expected_plugin_skills(mode.plugin_dir)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError(f"--modes-json {path}: mode '{mode.name}' plugin manifest failed to load: {error}") from error
    if not declared:
        raise ValueError(f"--modes-json {path}: mode '{mode.name}' plugin manifest declares no skills")
    return replace(mode, plugin_sha256=_tree_sha256(Path(mode.plugin_dir)))


def load_modes_json(path: Path) -> list[str]:
    """Register the arms of a ``--modes-json`` file in ``MODES``; return their names in file order.

    An entry takes ``ModeConfig`` fields only, so no arm can set its own budget or timeout:
    the harness owns both caps. A plugin arm loads skills and serves no MCP server.
    """
    try:
        entries = json.loads(path.read_text())
    except OSError as error:
        raise ValueError(f"cannot read --modes-json {path}: {error}") from error
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"--modes-json {path} must hold a non-empty list of mode objects")
    loaded: dict[str, ModeConfig] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"--modes-json {path}: every entry must be an object")
        unknown = sorted(set(entry) - _MODE_FIELDS)
        if unknown:
            raise ValueError(f"--modes-json {path}: unknown mode field(s) {unknown}; allowed: {sorted(_MODE_CALLER_FIELDS)}")
        computed = sorted(set(entry) - _MODE_CALLER_FIELDS)
        if computed:
            raise ValueError(f"--modes-json {path}: mode entry '{entry.get('name')}' sets harness-computed "
                             f"field(s) {computed}; allowed: {sorted(_MODE_CALLER_FIELDS)}")
        absent = sorted(_MODE_REQUIRED - set(entry))
        if absent:
            raise ValueError(f"--modes-json {path}: mode entry lacks {absent}")
        mode = ModeConfig(**{"mcp_config_path": None, **entry})
        if not isinstance(mode.tools, list) or not all(isinstance(tool, str) for tool in mode.tools):
            raise ValueError(f"--modes-json {path}: mode '{mode.name}' tools must be a list of strings")
        if mode.prompt_mode not in ("replace", "append"):
            raise ValueError(f"--modes-json {path}: mode '{mode.name}' prompt_mode must be replace or append")
        if mode.name in MODES or mode.name in loaded:
            raise ValueError(f"--modes-json {path}: mode name '{mode.name}' is already taken")
        if mode.plugin_dir:
            mode = _validate_plugin_arm(path, mode)
        loaded[mode.name] = mode
    MODES.update(loaded)
    return list(loaded)


def _select_tasks(args: argparse.Namespace, models: list[str]) -> tuple[object, list[str]]:
    """Resolve the panel (None unless --panel) and the task list from the CLI args."""
    if args.panel:
        if any(RUNNERS[model] not in TRAJECTORY_RUNNERS for model in models):
            raise ValueError("--panel requires claude or codex models; other runners write no trajectory "
                             "sidecar, so every panel row would count as contaminated")
        panel = panels.load_panel(args.panel, store_path=RESULTS_DIR / baselines.STORE_FILENAME)
        panel.register(TASKS)
        return panel, panel.select(args.panel_split)
    if args.tasks_json:
        json_tasks = register_json_tasks(args.tasks_json, TASKS)
        return None, list(json_tasks)
    return None, select_tasks(args.tasks)


def _select_arms(args: argparse.Namespace, models: list[str], timestamp: str) -> tuple[list[str], object]:
    """Resolve the arm (mode) names and the experiment manifest, if any, from the CLI args."""
    experiment = None
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
    elif args.modes_json:
        modes = load_modes_json(args.modes_json)
        if any(RUNNERS[model] != "claude" for model in models) and any(
            _uses_claude_session_features(MODES[name]) for name in modes
        ):
            raise ValueError("--modes-json skill and prompt_mode arms require Claude models")
    else:
        requested_modes = args.modes or ("baseline,tilth,wozcode" if args.wozcode_plugin_dir else None)
        modes = select_modes(requested_modes, args.runner)
        if "wozcode" in modes and not args.wozcode_plugin_dir:
            raise ValueError("wozcode mode requires --wozcode-plugin-dir")
    return modes, experiment


def _apply_preamble(modes: list[str], models: list[str], experiment: object, candidate_sha: str | None) -> None:
    """Make every selected tilth arm a preamble arm, with the preamble at ``candidate_sha`` or in this checkout."""
    if experiment is not None:
        raise ValueError("a pinned experiment manifest owns its arms; it cannot be combined with --preamble")
    if any(RUNNERS[model] not in ("claude", "codex") for model in models):
        raise ValueError("the preamble arm runs on Claude and Codex models only")
    tilth_modes = [name for name in modes if _is_tilth_arm(MODES[name])]
    if not tilth_modes:
        raise ValueError("no selected arm serves tilth")
    if appended := [name for name in tilth_modes if MODES[name].prompt_mode != "replace"]:
        raise ValueError(f"arm(s) {appended} already set prompt_mode")
    text = read_preamble(candidate_sha)
    for name in tilth_modes:
        MODES[name] = preamble_mode(MODES[name], text)


def _verify_mcp_servers(modes: list[str], runner: Optional[str]) -> None:
    """Verify the exact MCP command that each selected runner will launch."""
    for mode_name in modes:
        cfg_path = MODES[mode_name].mcp_config_path
        try:
            server_commands = mcp_server_commands(mode_name, runner)
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


def _clone_on_demand_repos(repo_names: set[str]) -> None:
    """Clone each mined repo a selected task needs, ahead of any `Task.fixture_path` read."""
    for repo_name in sorted(repo_names):
        repo = REPOS.get(repo_name)
        if repo is None or not repo.on_demand_clone:
            continue
        try:
            repo.ensure_cloned()
        except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
            print(f"ERROR: cannot clone repo '{repo_name}': {getattr(error, 'stderr', None) or error}")
            sys.exit(1)


def _prepare_repos(selected_repos: set[str], *, verbose: bool) -> None:
    """Check each selected real-world repo exists, then clean it."""
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
        if verbose:
            print(f"Cleaned repo: {repo_name}")


def _run_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def main(argv: Optional[list[str]] = None):
    # The stack releases the budget run lock however the run ends, so one process can run main again.
    with ExitStack() as held:
        _main(argv, held)


def _main(argv: Optional[list[str]], held: ExitStack):
    parser = argparse.ArgumentParser(
        description="Run tilth benchmarks",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run.py --experiment benchmark/experiments/upstream-fork.json --models sonnet --reps 5 --max-usd 40
  python run.py --models haiku --reps 1 --tasks find_definition --modes baseline,tilth --max-usd 1
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
        "--allow-cli-drift", action="store_true",
        help="Run with a Claude CLI that is not the version in benchmark/claude-cli.version; every stored cell's "
             "run key then drifts and is bought again",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the planned cells and which would call a model, then exit; no model call, no --max-usd needed",
    )
    parser.add_argument(
        "--max-usd", type=float,
        help="Run-wide spend ceiling in USD; required when any cell is not answered from the result store",
    )
    parser.add_argument(
        "--budget-id",
        help="Name of the persistent spend budget this run charges (default: a digest of the planned run keys, "
             "so a rerun of the same command resumes its spend)",
    )
    parser.add_argument(
        "--fresh-budget", action="store_true",
        help="Start the budget over: record a reset marker so earlier charges stop counting against --max-usd",
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
        "--tasks-json",
        type=Path,
        help="JSON file of task definitions (mutation or mined shape); runs exactly those tasks. "
             "Excludes --tasks and --panel",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write result rows to this JSONL path (default: benchmark/results/benchmark_<timestamp>.jsonl); "
             "stream logs go to a sibling <stem>_streams directory",
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
    parser.add_argument(
        "--preamble",
        action="store_true",
        help="Give every tilth arm prompts/preamble.md as its whole system prompt, as `tilth claude` and "
             "`tilth codex` do; read at --candidate-sha, else from this checkout. Claude and Codex models only",
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
    arm_group.add_argument(
        "--modes-json",
        type=Path,
        help="JSON list of ModeConfig objects (skill-plugin arms); runs exactly those arms. "
             "Excludes --panel",
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

    args = parser.parse_args(argv)
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
    if args.panel_split != "all" and not args.panel:
        parser.error("--panel-split requires --panel")
    if args.modes_json and args.panel:
        parser.error("--modes-json cannot be combined with --panel")
    if args.tasks_json and args.panel:
        parser.error("--tasks-json cannot be combined with --panel")
    if args.tasks_json and args.tasks.lower() != "all":
        parser.error("--tasks-json selects its own tasks; it cannot be combined with --tasks")
    if args.output is not None and args.output.is_dir():
        parser.error(f"--output {args.output} is a directory; pass a file path")

    RESULTS_DIR.mkdir(exist_ok=True)
    timestamp = _run_timestamp()
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
        panel, tasks_list = _select_tasks(args, models)
        modes, experiment = _select_arms(args, models, timestamp)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    if args.strict_file_tools:
        try:
            _require_strict_cli()
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
    if args.preamble:
        try:
            _apply_preamble(modes, models, experiment, args.candidate_sha)
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            parser.error(f"--preamble: {error}")

    if any(RUNNERS[name] == "claude" for name in models):
        try:
            print(require_pinned_cli(allow_drift=args.allow_cli_drift))
        except CliPinError as error:
            parser.error(str(error))

    # Ad hoc, the baseline hook and the replay audit use this checkout's HEAD (the seed hook), even with
    # --candidate-sha: a candidate hook is audited against the seed hook, never against itself.
    hook_sha = None
    if args.strict_file_tools:
        try:
            hook_sha = _git("rev-parse", "--verify", "HEAD^{commit}", cwd=REPO_ROOT).strip()
            _hook_source_key(hook_sha)
            hook_binary = build_candidate(hook_sha).binary_path
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
            parser.error(f"cannot build the strict hook at {hook_sha or 'HEAD'}: {error}")

    _verify_mcp_servers(modes, args.runner)

    if args.strict_file_tools:
        try:
            probe_strict_hooks(modes, hook_binary)
        except HookProbeFailed as error:
            parser.error(f"strict hook probe failed, no cell ran: {error}")

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

    # Mined repos clone first: `fixture_path` clones on demand and must not escape as a traceback.
    _clone_on_demand_repos({TASKS[name].repo for name in tasks_list})

    # Tasks without a fixture build their own workdirs; only the rest copy fixtures.
    fixture_tasks = [name for name in tasks_list if TASKS[name].fixture_path is not None]

    # Validate and restore the synthetic source once. Every cell runs from a
    # disposable copy, so the scheduler never mutates this source again.
    if "synthetic" in {TASKS[name].repo for name in fixture_tasks}:
        if not SYNTHETIC_REPO.exists():
            parser.error(
                f"Synthetic repo not found at {SYNTHETIC_REPO}; "
                "run python benchmark/fixtures/setup.py"
            )
        reset_repo()

    selected_repos = set(TASKS[t].repo for t in fixture_tasks) - {"synthetic"}
    _prepare_repos(selected_repos, verbose=args.verbose)


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
                            strict_file_tools=args.strict_file_tools, hook_sha=hook_sha,
                        ),
                    ))

    # A cell whose key matches a completed stored row is answered from the
    # store. Any other cell spawns a model runner, which makes this a paid run.
    store_path = RESULTS_DIR / baselines.STORE_FILENAME
    history = baselines.load_rows(store_path)
    reusable = baselines.completed_by_key(history)
    pending = [cell for cell in cells if cell.run_key not in reusable]
    if args.dry_run:
        for cell in cells:
            members = getattr(TASKS[cell.task_name], "members", ())
            detail = (
                f" | {len(members)} tasks in one session: {', '.join(member.name for member in members)}"
                f" | budget ${args.max_budget_usd * len(members):.2f} (${args.max_budget_usd:.2f} each)"
            ) if members else ""
            state = "reuse" if cell.run_key in reusable else "run"
            print(f"{cell.run_key[:12]} {state:5} {cell.task_name} {cell.mode_name} {cell.model_name} "
                  f"rep{cell.repetition}{detail}")
        print(f"dry run: {len(cells)} cells planned, {len(pending)} would call a model; nothing ran")
        return
    if pending:
        if args.max_usd is None:
            parser.error(
                f"{len(pending)} of {len(cells)} cells have no reusable stored row; "
                "a paid run requires --max-usd"
            )
        try:
            auth.preflight_claude_auth(claude_auth_targets(
                [(cell.task_name, cell.mode_name, cell.model_name) for cell in pending], bare=bare))
        except auth.ClaudeAuthError as error:
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
    if args.output is not None:
        output_file = args.output
        stream_log_dir = output_file.parent / f"{output_file.stem}_streams" / timestamp
    else:
        output_file = RESULTS_DIR / f"benchmark_{timestamp}{model_suffix}.jsonl"
        stream_log_dir = RESULTS_DIR / "streams" / timestamp
    output_file.parent.mkdir(parents=True, exist_ok=True)

    budget_id = args.budget_id if args.budget_id is not None else plan_budget_id(cell.run_key for cell in cells)
    # Take the run lock before the budget or the output file changes: a second run with the
    # same budget would count spend twice and truncate the output of the live run.
    try:
        held.enter_context(hold_run_lock(budget_id))
    except BudgetBusyError as error:
        print(f"run.py: {error}", file=sys.stderr)
        sys.exit(os.EX_TEMPFAIL)
    except ValueError as error:
        parser.error(str(error))
    try:
        ledger = (SpendLedger.persistent(budget_id, args.max_usd, fresh=args.fresh_budget) if pending
                  else SpendLedger(max_usd=args.max_usd))
    except ValueError as error:
        parser.error(str(error))

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
    if pending:
        print(f"Budget:      {budget_id} (${ledger.spent:.4f} recorded; --fresh-budget starts it over)")
    print(f"Output:      {output_file}")
    print(f"Streams:     {stream_log_dir}/<cell>.jsonl  (tail -f for live agent output)")
    print("=" * 70)
    print()

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
            ledger.charge(amount, source=row.get("cost_source") or "native", run_key=row.get("run_key"))
            if row.get("infra") != "prepare":
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
            """Store a failed cell, charging its native cost, else the cost priced from its stream, else its estimate.

            A native or priced cost is reported as ``total_cost_usd``; an estimate is
            charged to the ledger as ``charged_usd`` but is not a measured cost.
            """
            cost, source = failed_cell_cost(_read_stream(stream_log_path), runner, estimate, row.get("model"))
            failed = {
                **row,
                **({"total_cost_usd": cost} if source != "estimate" else {}),
                "cost_source": source,
                "trajectory_path": write_trajectory(stream_log_path, runner),
            }
            settle(failed, cost)
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
                if panel:
                    _store_reused_panel_row({**stored, **scanned}, panel.stamp(task_name), store_path=store_path,
                                              history=history)
                record({
                    **panels.unstamp(stored), **scanned, **experiment_metadata, "tilth_version": reported_version,
                    "variant": _variant_metadata(MODES[mode_name], reported_version=reported_version),
                    "charged_usd": 0.0, "reused": True,
                })
                print(f"  ↺ reused stored row ({'✓' if stored.get('correct') else '✗'})")
                continue

            estimate = estimate_cell_cost(
                history, task=task_name, mode=mode_name, model=MODELS[model_name],
                run_max_cost=run_max_cost, fallback=fallback_estimate * getattr(task, "size", 1),
            )
            try:
                exhausted = ledger.exhausted()
                crossing = not exhausted and ledger.would_cross(estimate)
            except ValueError as error:
                stop_reason = f"spend ceiling: {error}"
                break
            if exhausted:
                stop_reason = (
                    f"spend ceiling: budget {budget_id} has ${ledger.spent:.4f} recorded against "
                    f"--max-usd {args.max_usd}; no paid cell starts for {run_id}"
                )
                break
            if crossing:
                stop_reason = (
                    f"spend ceiling: budget {budget_id}: ${ledger.spent:.4f} spent + ${estimate:.4f} estimated for "
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

            cell_slug = f"{current_run:02d}_{task_name.replace(':', '_')}_{mode_name}_{model_name}_rep{rep}"
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
                **erosion.UNMEASURED_FIELDS,
                **_SKILL_AUDIT_UNMEASURED,
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
                    **({"hook_sha": hook_sha} if args.strict_file_tools else {}),
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

            except ArmUnavailableError as error:
                # Config-level failure: every later cell of this arm would fail
                # identically. Abort instead of burning budget on an invalid comparison.
                print(f"  ✗ {error.kind.upper()} UNAVAILABLE: {error}")
                record_failure({
                    **record_metadata,
                    "error": f"{error.kind}_unavailable: {error}",
                    "correct": False,
                    "correctness_reason": f"Exception: {error}",
                }, stream_log_path, runner, estimate)
                print(f"\nAborting run: {error.abort_hint}; fix it and re-run.")
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

            except (external.task.EnvBuildError, external.task.PrepareError) as error:
                # No model call ran, so the cell estimate must not be charged.
                print(f"  ✗ PREPARE FAILED: {error}")
                record_failure({
                    **record_metadata,
                    "infra": "prepare",
                    "error": f"infra:prepare: {error}",
                    "correct": False,
                    "correctness_reason": f"Prepare failed: {error}",
                }, stream_log_path, runner, 0.0)

            except auth.ClaudeAuthError as error:
                print(f"  ✗ AUTH REFUSED: {error}")
                record_failure({
                    **record_metadata,
                    "infra": "auth",
                    "error": f"infra:auth: {error}",
                    "correct": False,
                    "correctness_reason": f"Auth refused: {error}",
                }, stream_log_path, runner, estimate)
                stop_reason = f"auth: {error}"
                break

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
        print(f"Spend is recorded in budget {budget_id}; a rerun counts it against --max-usd. "
              "Pass --fresh-budget to start the budget over.")
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
