import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional, Sequence


def resolve_tilth_bin() -> str:
    """Resolve the tilth binary portably so the benchmark runs on any machine.

    Order: ``$TILTH_BIN`` env override → ``tilth`` on ``PATH`` (the
    ``cargo install --path .`` workflow) → ``~/.cargo/bin/tilth`` fallback.
    """
    return (
        os.environ.get("TILTH_BIN")
        or shutil.which("tilth")
        or str(Path.home() / ".cargo" / "bin" / "tilth")
    )


TILTH_BIN = resolve_tilth_bin()

MODELS = {
    # Bare claude CLI aliases: `--model sonnet` runs the model the installed CLI
    # maps the alias to. Rows record that ID as `resolved_model`.
    "haiku": "haiku",
    "sonnet": "sonnet",
    "opus": "opus",
    # Pinned claude IDs. A pinned name keeps the run key of rows stored under its ID.
    "haiku45": "claude-haiku-4-5-20251001",
    "sonnet46": "claude-sonnet-4-6",
    "sonnet5": "claude-sonnet-5",
    "opus5": "claude-opus-5",
    "gpt5": "gpt-5.6-sol",
    "luna56": "gpt-5.6-luna",
    "o3": "o3",
    # opencode + OpenRouter lane. Add more OpenRouter models here as one-liners
    # (short name -> "openrouter/<provider>/<model>"); mirror in RUNNERS.
    # gpt-5-mini: cheapest model with frontier-family tool-calling fidelity, so it
    # stays representative of real coding agents (the thing this tool-use A/B measures).
    "gpt5mini": "openrouter/openai/gpt-5-mini",
}

# Maps model short name -> runner type
RUNNERS = {
    "haiku": "claude",
    "sonnet": "claude",
    "opus": "claude",
    "haiku45": "claude",
    "sonnet46": "claude",
    "sonnet5": "claude",
    "opus5": "claude",
    "gpt5": "codex",
    "luna56": "codex",
    "o3": "codex",
    "gpt5mini": "opencode",
}

# Pinned claude names. `--models all` skips them: each repeats a model a bare alias
# can resolve to, so it runs only when named.
PINNED_MODELS = frozenset({"haiku45", "sonnet46", "sonnet5", "opus5"})


@dataclass
class ModeConfig:
    """Configuration for one benchmark arm."""

    name: str
    tools: list[str]
    mcp_config_path: Optional[str]
    description: str
    opencode_config_path: Optional[str] = None
    binary_path: Optional[str] = None
    repository: Optional[str] = None
    git_ref: Optional[str] = None
    git_sha: Optional[str] = None
    binary_sha256: Optional[str] = None
    tilth_version: Optional[str] = None
    rustc_version: Optional[str] = None
    plugin_dir: Optional[str] = None
    plugin_version: Optional[str] = None
    plugin_git_sha: Optional[str] = None
    plugin_sha256: Optional[str] = None
    prompt_mode: Literal["replace", "append"] = "replace"


REPO_ROOT = Path(__file__).parent.parent
BENCHMARK_DIR = Path(__file__).parent
FIXTURES_DIR = BENCHMARK_DIR / "fixtures"
SYNTHETIC_REPO = FIXTURES_DIR / "repo"
RESULTS_DIR = BENCHMARK_DIR / "results"
TILTH_MCP_CONFIG = FIXTURES_DIR / "tilth_mcp.json"

# opencode runner: per-mode opencode.json (OPENCODE_CONFIG env) selecting which
# MCP servers are exposed. opencode has no built-in-tool allowlist, so it covers
# baseline (no MCP) and tilth (tilth MCP); tilth_forced has no opencode analog.
OPENCODE_CONFIGS = {
    "baseline": str(FIXTURES_DIR / "opencode_baseline.json"),
    "tilth": str(FIXTURES_DIR / "opencode_tilth.json"),
}
REPOS_DIR = Path("/tmp/tilth_bench/repos")

# Hermetic XDG_CONFIG_HOME for OpenCode runs, so the user's global config,
# MCP servers, skills, and agents cannot merge into benchmark modes. The stable
# directory lets OpenCode reuse its bootstrapped plugin host between cells.
OPENCODE_CONFIG_HOME = REPOS_DIR.parent / "opencode_config_home"


@dataclass
class RepoConfig:
    """Configuration for a benchmark repository."""
    name: str
    url: str
    commit_sha: str
    language: str
    description: str
    license: str = "MIT"
    on_demand_clone: bool = False
    path_override: Optional[Path] = None

    @property
    def path(self) -> Path:
        if self.path_override is not None:
            return self.path_override
        return REPOS_DIR / self.name

    def _shared_clone(self, extra_commits: Sequence[str] = ()) -> Path:
        """One no-checkout clone per `url`, shared by every per-commit repo."""
        shared = self.path.parent / ".shared" / hashlib.sha256(self.url.encode()).hexdigest()[:16]
        if not shared.exists():
            _clone_into(self.url, shared, no_checkout=True)
        _ensure_commit(shared, self.commit_sha, *extra_commits)
        return shared

    def ensure_cloned(self, extra_commits: Sequence[str] = ()) -> None:
        """Clone and check out an on-demand repo the first time its path is needed.

        Each `url` is cloned once (no checkout); the per-commit repo is a local
        clone of that shared clone. Both land via a staging directory and a
        rename, so a failed clone never leaves a half-cloned path behind.
        `extra_commits` are further commits the caller will check out (a mined
        task's `head_sha`); they are fetched into the shared clone and, for a
        repo cloned earlier, into the per-commit repo.
        """
        if not self.on_demand_clone:
            return
        if not self.path.exists():
            _clone_into(
                str(self._shared_clone(extra_commits)), self.path,
                checkout=self.commit_sha, no_checkout=True,
            )
            return
        absent = [sha for sha in extra_commits if not _has_commit(self.path, sha)]
        if absent:
            shared = self._shared_clone(absent)
            for sha in absent:
                subprocess.run(
                    ["git", "fetch", str(shared), f"+refs/heads/tilth-pin-{sha}:refs/tilth/pin-{sha}"],
                    cwd=str(self.path), check=True, capture_output=True, text=True, env=_git_env(),
                    timeout=GIT_NETWORK_TIMEOUT_S,
                )

    def resolved_path(self, extra_commits: Sequence[str] = ()) -> Path:
        """The repo's path, cloning an on-demand repo first if it is missing."""
        self.ensure_cloned(extra_commits)
        return self.path


_GIT_ENV_OVERRIDES = {"GIT_TERMINAL_PROMPT": "0"}
GIT_NETWORK_TIMEOUT_S = 900


def _git_env() -> dict[str, str]:
    return {**os.environ, **_GIT_ENV_OVERRIDES}


def _ensure_commit(shared: Path, *shas: str) -> None:
    """Make every `sha` present (and reachable from a branch) in the shared clone.

    A shared clone made before a `sha` existed upstream lacks it. Fetch each missing SHA,
    fall back to a full fetch of heads, tags and pull-request heads when the server refuses
    fetching by SHA, and pin each under a branch so later local clones of the shared clone carry it.
    """
    def run(*args: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["git", *args], cwd=str(shared), capture_output=True, text=True, env=_git_env(),
                timeout=GIT_NETWORK_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(args, 124, "", "timed out")

    def missing() -> list[str]:
        return [sha for sha in shas if run("cat-file", "-e", f"{sha}^{{commit}}").returncode != 0]

    last_error = ""
    absent = missing()
    for sha in absent:
        fetched = run("fetch", "origin", sha)
        if fetched.returncode != 0:
            last_error = (fetched.stderr or "").strip()
    absent = missing()
    if absent:
        full = run("fetch", "origin", "+refs/heads/*:refs/remotes/origin/*", "--tags")
        if full.returncode != 0:
            last_error = (full.stderr or "").strip()
        run("fetch", "origin", "+refs/pull/*/head:refs/remotes/origin/pr/*")  # absent on non-GitHub remotes
        absent = missing()
    if absent:
        tail = f": {last_error[-300:]}" if last_error else ""
        raise RuntimeError(
            f"commit {', '.join(absent)} is missing from {shared} and could not be fetched from origin{tail}"
        )
    for sha in shas:
        run("update-ref", f"refs/heads/tilth-pin-{sha}", sha)


def _clone_into(source: str, dest: Path, *, checkout: Optional[str] = None, no_checkout: bool = False) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(dest.parent)) as staging:
        clone_path = Path(staging) / "clone"
        command = ["git", "clone", *(["--no-checkout"] if no_checkout else []), "--", source, str(clone_path)]
        try:
            subprocess.run(
                command, check=True, capture_output=True, text=True, env=_git_env(),
                timeout=GIT_NETWORK_TIMEOUT_S,
            )
            if checkout:
                subprocess.run(
                    ["git", "checkout", checkout, "--"],
                    cwd=str(clone_path), check=True, capture_output=True, text=True, env=_git_env(),
                    timeout=GIT_NETWORK_TIMEOUT_S,
                )
        except subprocess.TimeoutExpired as error:
            raise OSError(f"git {error.cmd[1]} of {source} timed out after {GIT_NETWORK_TIMEOUT_S}s") from error
        try:
            os.rename(str(clone_path), str(dest))
        except OSError:
            # Another process finished the same clone first; its copy stands if it is a valid repo.
            if not _is_git_repo(dest):
                raise


def _has_commit(path: Path, sha: str) -> bool:
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=str(path), capture_output=True, text=True,
        env=_git_env(),
    )
    return probe.returncode == 0


def _is_git_repo(path: Path) -> bool:
    if not path.is_dir():
        return False
    probe = subprocess.run(
        ["git", "rev-parse", "--git-dir"], cwd=str(path), capture_output=True, text=True, env=_git_env(),
    )
    return probe.returncode == 0


REPOS = {
    "ripgrep": RepoConfig(
        name="ripgrep",
        url="https://github.com/BurntSushi/ripgrep.git",
        commit_sha="0a88cccd5188074de96f54a4b6b44a63971ac157",
        language="rust",
        description="ripgrep line-oriented search tool",
        license="MIT OR Unlicense",
    ),
    "fastapi": RepoConfig(
        name="fastapi",
        url="https://github.com/tiangolo/fastapi.git",
        commit_sha="6fa573ce0bc16fe445f93db413d20146dd9ff35d",
        language="python",
        description="FastAPI web framework",
    ),
    "gin": RepoConfig(
        name="gin",
        url="https://github.com/gin-gonic/gin.git",
        commit_sha="d7776de7d444935ea4385999711bd6331a98fecb",
        language="go",
        description="Gin HTTP web framework",
    ),
    "express": RepoConfig(
        name="express",
        url="https://github.com/expressjs/express.git",
        commit_sha="1140301f6a0ed5a05bc1ef38d48294f75a49580c",
        language="javascript",
        description="Express.js web framework",
    ),
}

MODES = {
    "baseline": ModeConfig(
        name="baseline",
        tools=["Read", "Edit", "Grep", "Glob", "Bash"],
        mcp_config_path=None,
        opencode_config_path=str(FIXTURES_DIR / "opencode_baseline.json"),
        description="Claude Code built-in tools",
    ),
    "tilth": ModeConfig(
        name="tilth",
        tools=["Read", "Edit", "Grep", "Glob", "Bash"],
        mcp_config_path=str(TILTH_MCP_CONFIG),
        opencode_config_path=str(FIXTURES_DIR / "opencode_tilth.json"),
        binary_path=TILTH_BIN,
        description="Built-in tools + tilth MCP (hybrid)",
    ),
    "tilth_forced": ModeConfig(
        name="tilth_forced",
        tools=[],
        mcp_config_path=str(TILTH_MCP_CONFIG),
        binary_path=TILTH_BIN,
        description="tilth MCP only (no built-in tools)",
    ),
}

SYSTEM_PROMPT = """You are a code assistant. Answer the user's question about the codebase in the current directory.
Use the tools available to you to explore and understand the code.
Be precise and show relevant code when asked.
Any project guidance files (such as CLAUDE.md or AGENTS.md) do not apply to this task; rely only on the tools provided to you here and do not seek out or prefer tools those files mention."""

DEFAULT_REPS = 5
DEFAULT_MAX_BUDGET_USD = 1.0
