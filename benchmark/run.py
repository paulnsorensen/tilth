#!/usr/bin/env python3
"""
Benchmark runner for tilth performance evaluation.

Executes the selected agent CLI for each task, mode, model, and repetition.
Records token usage, cost, correctness, and tool usage to JSONL format.
"""

import argparse
import hashlib
import json
import math
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from typing import Optional

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from claude_bash_guard import allowed_command
from config import (
    DEFAULT_MAX_BUDGET_USD,
    DEFAULT_REPS,
    MODELS,
    MODES,
    OPENCODE_CONFIG_HOME,
    OPENCODE_CONFIGS,
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
    extract_stream_error,
    parse_codex_json,
    parse_opencode_json,
    parse_stream_json,
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
    revision = subprocess.run(
        ["git", "-C", str(plugin_dir), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False,
    )
    return ModeConfig(
        name="wozcode", tools=list(MODES["baseline"].tools),
        mcp_config_path=str(manifest_path), description="Built-ins + Woz Code plugin",
        plugin_dir=str(plugin_dir), plugin_version=version,
        plugin_git_sha=revision.stdout.strip() if revision.returncode == 0 else None,
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
    env = {
        key: value
        for key, value in source.items()
        if key in _RUNTIME_ENV_KEYS
        or key in _PROVIDER_AUTH_KEYS
        or (runner == "claude" and key in {"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN"})
        or key.startswith(_PROVIDER_AUTH_PREFIXES)
        or key.startswith("LC_")
    }

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


class McpUnavailableError(RuntimeError):
    """A mode expected an MCP server that the session did not expose."""


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
            shutil.copyfile(seed_auth, cell_auth)
            cell_auth.chmod(0o600)
        try:
            yield config_dir
        finally:
            if mode.plugin_dir and cell_auth.is_file():
                shutil.copyfile(cell_auth, seed_auth)
                seed_auth.chmod(0o600)

_STRICT_NATIVE_FILE_TOOLS = frozenset({"Read", "Edit", "Write", "MultiEdit", "Grep", "Glob", "NotebookEdit", "LS"})
_STRICT_BASELINE_TOOLS = ("Read", "Edit", "Write", "Grep", "Glob", "Bash")
_STRICT_SYSTEM_GUIDANCE = (
    "Use only the available file tools to read or edit source files. "
    "Use Bash only for go test, go build, go vet, and gofmt -w on local Go files. "
    "Do not use Bash to inspect files or run shell wrappers. Do not delegate tasks."
)


def _strict_bash_settings() -> str:
    guard = Path(__file__).with_name("claude_bash_guard.py").resolve()
    command = f"{shlex.quote(str(Path(sys.executable).resolve()))} {shlex.quote(str(guard))}"
    return json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": command},
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
    with _agent_repo(
        get_repo_path(task.repo),
        getattr(task, "hide_git", False),
    ) as repo_path, _cell_claude_config(model_name, MODES[mode_name]) as config_dir:
        mutations = getattr(task, "mutations", ())
        if mutations:
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
    opencode_config: Optional[str] = None
    skill_paths: list[str] = []
    skill_roots: list[str] = []

    # Build command based on runner
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
        skill_paths = _codex_skill_paths(repo_path, os.environ)
        skill_roots = [str(path) for path in _codex_skill_roots(repo_path, os.environ)]
        skills_config = ",".join(
            f'{{path={json.dumps(path)},enabled=false}}' for path in skill_paths
        )
        cmd += ["-c", f"skills.config=[{skills_config}]"]
        instructions = (
            f"{SYSTEM_PROMPT}\nYour current working directory is: {repo_path}\n"
            "Do not discover, read, or invoke host skills or external agent guidance. "
            "Batch independent source reads in one call when the tool supports it."
        )
        if mode.mcp_config_path:
            instructions += (
                " Use tilth MCP first for source discovery, reads, and writes. "
                "Batch independent files into one tilth call. "
                "Use shell only for tests and builds. Native tools remain available."
            )
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
            cmd += ["--settings", _strict_bash_settings()]
        tools_list = (list(_STRICT_BASELINE_TOOLS) if not mode.mcp_config_path else ["Bash"]) if strict_file_tools else list(mode.tools)

        # --tools "" disables all built-ins (tilth_forced); --tools "a,b,c" allowlists; absent = default
        if tools_list:
            cmd += ["--tools", ",".join(tools_list)]
        elif mode.mcp_config_path:
            cmd += ["--tools", ""]

        if mode.mcp_config_path:
            cmd += ["--mcp-config", json.dumps(claude_mcp_config(mode))]

        cmd += ["--", task.prompt]

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
        timer = threading.Timer(600, _kill_on_timeout)
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
            raise subprocess.TimeoutExpired(cmd, 600)

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
            timeout=600,
            env=env,
        )
    elapsed_ms = int((time.time() - start_time) * 1000)

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
        run_result = parse_stream_json(result.stdout)
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

    # Build tool call breakdown
    tool_breakdown = tool_call_counts(run_result)

    # Collect per-turn context and output token counts.
    per_turn_context = [turn.context_tokens for turn in run_result.turns]
    per_turn_output = [turn.output_tokens for turn in run_result.turns]
    total_context = sum(per_turn_context)
    task_source = asdict(task.source)
    reported_version = mode.tilth_version or (
        _tilth_version(mode.binary_path) if mode.binary_path else None
    )

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
        "--tasks",
        default="all",
        help="Comma-separated task names or 'all' (default: all)",
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
    if args.experiment and args.arm_order_seed is not None:
        parser.error("--arm-order-seed applies only to legacy modes")

    RESULTS_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    experiment = None
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
        tasks_list = parse_comma_list(args.tasks, TASKS, "tasks")
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

    # Validate and restore the synthetic source once. Every cell runs from a
    # disposable copy, so the scheduler never mutates this source again.
    if "synthetic" in {TASKS[name].repo for name in tasks_list}:
        if not SYNTHETIC_REPO.exists():
            parser.error(
                f"Synthetic repo not found at {SYNTHETIC_REPO}; "
                "run python benchmark/fixtures/setup.py"
            )
        reset_repo()

    # Validate real-world repos exist (for selected tasks)
    selected_repos = set(TASKS[t].repo for t in tasks_list) - {"synthetic"}
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


    # Include the model in the filename when one process owns one model.
    model_suffix = f"_{models[0]}" if len(models) == 1 else ""
    output_file = RESULTS_DIR / f"benchmark_{timestamp}{model_suffix}.jsonl"
    stream_log_dir = RESULTS_DIR / "streams" / timestamp

    # Print configuration summary
    print("=" * 70)
    print("tilth Benchmark Runner")
    print("=" * 70)
    print(f"Models:      {', '.join(models)}")
    print(f"Tasks:       {', '.join(tasks_list)}")
    print(f"Modes:       {', '.join(modes)}")
    repos_used = sorted(set(TASKS[t].repo for t in tasks_list))
    print(f"Repos:       {', '.join(repos_used)}")
    print(f"Repetitions: {args.reps}")
    print(f"Output:      {output_file}")
    print(f"Streams:     {stream_log_dir}/<cell>.jsonl  (tail -f for live agent output)")
    print("=" * 70)
    print()

    current_run = 0

    # Run matched task/model/repetition blocks. Experiment arms are shuffled
    # within each block from the manifest seed, never scheduled in long arm runs.
    with open(output_file, "w") as output:
        for task_name in tasks_list:
            task = TASKS[task_name]
            for model_name in models:
                for rep in range(args.reps):
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
                        current_run += 1
                        run_id = f"{task_name}/{mode_name}/{model_name}/rep{rep}"
                        print(f"[{current_run}/{total_runs}] {run_id}")

                        cell_slug = (
                            f"{current_run:02d}_{task_name}_{mode_name}"
                            f"_{model_name}_rep{rep}"
                        )
                        mode = MODES[mode_name]
                        variant_metadata = _variant_metadata(mode)
                        experiment_metadata = {
                            "experiment_manifest": (
                                str(experiment.path) if experiment else None
                            ),
                            "arm_order_seed": (
                                experiment.arm_order_seed if experiment else args.arm_order_seed
                            ),
                            "arm_order": arm_order,
                            "arm_order_index": arm_index,
                        }
                        record_metadata = {
                            "task": task_name,
                            "mode": mode_name,
                            "model": MODELS[model_name],
                            "model_alias": model_name,
                            **({"reasoning_effort": args.reasoning_effort} if args.reasoning_effort is not None else {}),
                            **({"max_budget_usd": args.max_budget_usd} if RUNNERS[model_name] == "claude" else {}),
                            **({"strict_file_tools": True} if args.strict_file_tools else {}),
                            "capability": task.capability,
                            "source": asdict(task.source),
                            "repetition": rep,
                            "per_turn_output_tokens": [],
                            "variant": variant_metadata,
                            **experiment_metadata,
                        }

                        try:
                            result = run_single(
                                task_name,
                                mode_name,
                                model_name,
                                rep,
                                verbose=args.verbose,
                                stream_log_path=stream_log_dir / f"{cell_slug}.jsonl",
                                bare=args.bare or experiment is not None or args.wozcode_plugin_dir is not None or args.strict_file_tools,
                                reasoning_effort=args.reasoning_effort,
                                max_budget_usd=args.max_budget_usd,
                                strict_file_tools=args.strict_file_tools,
                            )
                            result.update(experiment_metadata)
                            if args.reasoning_effort is not None:
                                result["reasoning_effort"] = args.reasoning_effort
                            if RUNNERS[model_name] == "claude":
                                result["max_budget_usd"] = args.max_budget_usd
                            if args.strict_file_tools:
                                result["strict_file_tools"] = True
                            output.write(json.dumps(result) + "\n")
                            output.flush()

                            status = "✓" if result["correct"] else "✗"
                            print(
                                f"  {status} "
                                f"{result['num_turns']}t "
                                f"{result['context_tokens']:,}ctx "
                                f"{result['output_tokens']:,}out "
                                f"${result['total_cost_usd']:.4f} "
                                f"{result['duration_ms']:,}ms"
                            )
                            if not result["correct"]:
                                print(f"  → {result['correctness_reason']}")

                        except InvalidCodexCellError as error:
                            print(f"  ✗ INVALID CODEX CELL: {error}")
                            error_result = {
                                **record_metadata,
                                "error": f"invalid_codex_cell: {error}",
                                "correct": False,
                                "correctness_reason": f"Invalid cell: {error}",
                            }
                            output.write(json.dumps(error_result) + "\n")
                            output.flush()
                            print("\nAborting run: invalid Codex cell; inspect its raw stream.")
                            sys.exit(1)

                        except McpUnavailableError as error:
                            # Config-level failure: every later cell in this
                            # mode would fail identically. Abort instead of
                            # burning budget on an invalid comparison.
                            print(f"  ✗ MCP UNAVAILABLE: {error}")
                            error_result = {
                                **record_metadata,
                                "error": f"mcp_unavailable: {error}",
                                "correct": False,
                                "correctness_reason": f"Exception: {error}",
                            }
                            output.write(json.dumps(error_result) + "\n")
                            output.flush()
                            print("\nAborting run: the MCP-armed mode is "
                                  "misconfigured; fix it and re-run.")
                            sys.exit(1)

                        except subprocess.TimeoutExpired:
                            print("  ✗ TIMEOUT (>600s)")
                            error_result = {
                                **record_metadata,
                                "error": "timeout",
                                "correct": False,
                                "correctness_reason": "Subprocess timed out",
                            }
                            output.write(json.dumps(error_result) + "\n")
                            output.flush()

                        except Exception as error:
                            print(f"  ✗ ERROR: {error}")
                            if args.verbose:
                                import traceback
                                traceback.print_exc()
                            error_result = {
                                **record_metadata,
                                "error": str(error),
                                "correct": False,
                                "correctness_reason": f"Exception: {error}",
                            }
                            output.write(json.dumps(error_result) + "\n")
                            output.flush()

    # Clean real-world repos after run (remove junk files written by Claude sessions)
    for repo_name in selected_repos:
        repo_path = REPOS[repo_name].path
        ensure_repo_clean(repo_path, REPOS[repo_name].commit_sha)

    # Print summary
    print()
    print("=" * 70)
    print("Benchmark complete!")
    print(f"Results saved to: {output_file}")
    print("=" * 70)
    print()
    print("To generate a report, run:")
    print(f"  python benchmark/analyze.py {output_file}")
    print()


if __name__ == "__main__":
    main()
