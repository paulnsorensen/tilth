"""Flag trajectories whose tool inputs read grader material or fetch the task's upstream source.

Only tool-call inputs are scanned; a tool's output may mention any path. A hit
records its reason and the verbatim tool input. A missing sidecar is itself a
hit (``unscanned``), so no consumer treats an unverifiable rollout as clean.
"""

import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import config
from jsonl import tolerant_jsonl

from . import data
from .task import ExternalTask

_PATH_SEPARATORS = re.compile(r"[\s'\"`;|&<>(),=]+")
_HOME_REFERENCE = re.compile(r"\$\{HOME\}|\$HOME")
_STACK_INDEX = re.compile(r"[+-]\d+")
_REDIRECTION = re.compile(r"\d*[<>]")
_PIP_COMMAND = re.compile(r"\b(?:pip3?|uv\s+pip|python3?\s+-m\s+pip)\s+(?:download|install)\b([^;&|\n]*)")
_UV_ADD = re.compile(r"\buv\s+add\b([^;&|\n]*)")
_GO_COMMAND = re.compile(r"\bgo\s+(?:get|install|mod\s+download)\b([^;&|\n]*)")
_NPM_COMMAND = re.compile(
    r"\b(?:(?:npm|pnpm|yarn)\s+(?:install|i|add|view|v|info|show|pack|dlx)|npx)\b([^;&|\n]*)")
_FETCH_COMMAND = re.compile(
    r"\b(?:git\s+(?:clone|fetch|pull|remote\s+add)|curl|wget|go\s+(?:get|install)|gh\s+(?:repo\s+clone|api))\b"
)
_COMMAND_SEGMENTS = re.compile(r"[;&|\n]+")
_GITHUB_REPO = re.compile(r"github\.com[/:]([\w.-]+/[\w.-]+?)(?:\.git)?/?$")


@dataclass(frozen=True)
class _Upstream:
    """Fetch hosts always count; a github.com URL counts inside a fetch command or a fetch tool's input."""

    hosts: re.Pattern
    repo_url: re.Pattern

    def fetched_by(self, text: str) -> bool:
        if self.hosts.search(text):
            return True
        return any(_FETCH_COMMAND.search(segment) and self.repo_url.search(segment)
                   for segment in _COMMAND_SEGMENTS.split(text))


@dataclass(frozen=True)
class _Rules:
    """What counts as a hit for one task's cell."""

    benchmark_roots: tuple[str, ...]
    notes_roots: tuple[str, ...]
    data_roots: tuple[str, ...]
    foreign_roots: tuple[str, ...]
    upstream: "_Upstream | None"
    packages: tuple[str, ...]
    go_module: str | None
    npm_package: str | None = None


def _roots(path: Path) -> tuple[str, ...]:
    return tuple(dict.fromkeys((os.path.normpath(str(path)), os.path.realpath(path))))


def _normalize_package(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _rules(task: object) -> _Rules:
    slug, packages, go_module, npm_package = None, (), None, None
    if isinstance(task, ExternalTask):
        slug = task.upstream_repo
        if task.language == "javascript":
            npm_package = task.package_name.lower()
        elif task.language == "go":
            go_module = task.package_name
        else:
            packages = (task.package_name,)
    elif (repo := config.REPOS.get(getattr(task, "repo", ""))) is not None:
        if match := _GITHUB_REPO.search(repo.url):
            slug = match[1]
            if repo.language == "go":
                go_module = f"github.com/{slug}"
        if repo.language != "go":
            packages = (repo.name,)
    upstream = None
    if slug:
        tail = re.escape(slug) + r"(?:\.git)?(?=$|[/\s'\"#?@:])"
        hosts = re.compile(r"(?:codeload\.github\.com/|raw\.githubusercontent\.com/|api\.github\.com/repos/)" + tail,
                           re.IGNORECASE)
        clone = r"(?:github\.com[/:]|\bgh\s+repo\s+clone\s+|\bgh\s+api\s+(?:-\S+\s+)*repos/)"
        upstream = _Upstream(hosts, re.compile(clone + tail, re.IGNORECASE))
    return _Rules(
        benchmark_roots=_roots(config.BENCHMARK_DIR),
        notes_roots=_roots(config.REPO_ROOT / ".cheese"),
        data_roots=_roots(data.data_dir()),
        foreign_roots=_roots(config.REPOS_DIR) if isinstance(task, ExternalTask) else (),
        upstream=upstream,
        packages=tuple(_normalize_package(name) for name in packages),
        go_module=go_module,
        npm_package=npm_package,
    )


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _paths(text: str, base: str | None = None) -> Iterator[str]:
    """Paths named in ``text``, after ``~``, ``$HOME``, and ``..`` normalization.

    A relative token resolves against ``base`` when ``base`` is an absolute directory.
    Flags, URLs, and empty tokens are never paths.
    """
    home = os.path.expanduser("~")
    for token in _PATH_SEPARATORS.split(_HOME_REFERENCE.sub(home, text)):
        token = token.split("#", 1)[0]
        if token == "~" or token.startswith("~/"):
            token = home + token[1:]
        if token.startswith("/"):
            yield os.path.normpath(token)
        elif token and base and base.startswith("/") and not token.startswith("-") and "://" not in token:
            yield os.path.normpath(os.path.join(base, token))


def _move(segment: str, base: str | None, previous: str | None, stack: list[str | None]) -> tuple[str | None, str | None]:
    """The ``(base, previous)`` pair after a ``cd``, ``pushd``, or ``popd`` segment; other segments change nothing.

    ``pushd`` pushes the old base on ``stack`` and ``popd`` pops it. ``cd -`` returns to ``previous``.
    A form this scan cannot resolve leaves the base unchanged.
    """
    words = segment.split()
    for index, word in enumerate(words):
        if _REDIRECTION.match(word):
            words = words[:index]
            break
    if not words or words[0] not in {"cd", "pushd", "popd"}:
        return base, previous
    command = words[0]
    if command == "popd":
        if len(words) > 1 or not stack:
            return base, previous
        return stack.pop(), base
    if any(_STACK_INDEX.fullmatch(word) for word in words[1:]):
        return base, previous
    arguments = [word for word in words[1:] if word == "-" or not word.startswith("-")]
    home = os.path.expanduser("~")
    if not arguments:
        if command == "pushd":
            if not stack:
                return base, previous
            swapped = stack[-1]
            stack[-1] = base
            return swapped, base
        target = home
    elif arguments[0] == "-" and command == "cd":
        return (previous, base) if previous else (base, previous)
    else:
        target = _HOME_REFERENCE.sub(home, arguments[0].strip("'\""))
        if target == "~" or target.startswith("~/"):
            target = home + target[1:]
        if target.startswith("~") or "$" in target or "`" in target:
            return base, previous
    if not target.startswith("/"):
        if not (base and base.startswith("/")):
            return base, previous
        target = os.path.join(base, target)
    if command == "pushd":
        stack.append(base)
    return os.path.normpath(target), base


def _command_paths(command: str, base: str | None) -> Iterator[str]:
    """Paths in a shell command; ``cd``, ``pushd``, and ``popd`` move the base for later segments."""
    previous: str | None = None
    stack: list[str | None] = []
    for segment in _COMMAND_SEGMENTS.split(command):
        yield from _paths(segment, base)
        base, previous = _move(segment.strip(), base, previous, stack)


def _input_paths(tool_input: object, cell_cwd: str | None) -> Iterator[str]:
    """Paths in a tool input; the input's own absolute ``cwd`` is the base, else the cell cwd."""
    base = cell_cwd
    if isinstance(tool_input, dict) and isinstance(tool_input.get("cwd"), str) and tool_input["cwd"].startswith("/"):
        base = tool_input["cwd"]
    if isinstance(tool_input, dict) and isinstance(tool_input.get("command"), str):
        yield from _command_paths(tool_input["command"], base)
        tool_input = {key: value for key, value in tool_input.items() if key != "command"}
    for text in _strings(tool_input):
        yield from _paths(text, base)


def _under(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots)


def _names_package(arguments: str, packages: tuple[str, ...]) -> bool:
    for token in arguments.split():
        if token.startswith("-") or token in {".", ".."}:
            continue
        name = re.split(r"[=<>!~\[@;]", token, maxsplit=1)[0]
        if _normalize_package(name) in packages:
            return True
    return False


def _names_npm_package(arguments: str, package: str) -> bool:
    for token in arguments.split():
        if token.startswith("-") or token in {".", ".."}:
            continue
        token = token.strip("'\"").removeprefix("npm:").lower()
        end = token.find("@", 1)
        if (token if end < 0 else token[:end]) == package:
            return True
    return False


def _fetches_npm_package(text: str, package: str) -> bool:
    if any(_names_npm_package(match[1], package) for match in _NPM_COMMAND.finditer(text)):
        return True
    name = re.escape(package).replace("/", "(?:/|%2f)")
    registry = rf"(?:registry\.npmjs\.org|registry\.yarnpkg\.com|unpkg\.com|(?:cdn\.)?jsdelivr\.net/npm|esm\.sh)/{name}"
    return re.search(registry + r"(?=$|[/@\s'\"?#])", text.lower()) is not None


def _fetches_package(text: str, rules: _Rules) -> bool:
    if rules.npm_package and _fetches_npm_package(text, rules.npm_package):
        return True
    if rules.packages:
        for pattern in (_PIP_COMMAND, _UV_ADD):
            if any(_names_package(match[1], rules.packages) for match in pattern.finditer(text)):
                return True
        # Compare URLs in normalized package spelling: lowercase, "_" as "-".
        urls = text.lower().replace("_", "-")
        for package in rules.packages:
            index_urls = (f"pypi.org/project/{package}", f"pypi.org/simple/{package}", f"pypi.org/pypi/{package}",
                          f"crates.io/api/v1/crates/{package}", f"static.crates.io/crates/{package}")
            if any(url in urls for url in index_urls):
                return True
            if "files.pythonhosted.org" in urls and re.search(rf"/{re.escape(package)}-[0-9]", urls):
                return True
    if rules.go_module:
        for match in _GO_COMMAND.finditer(text):
            for token in match[1].split():
                module = token.split("@", 1)[0]
                if module == rules.go_module or module.startswith(rules.go_module + "/"):
                    return True
    return False


def _is_fetch_tool(name: object) -> bool:
    return isinstance(name, str) and "fetch" in name.lower()


def _call_reasons(tool_input: object, rules: _Rules, tool: object = None, cwd: str | None = None) -> list[str]:
    reasons: list[str] = []
    fetching = _is_fetch_tool(tool)
    for path in _input_paths(tool_input, cwd):
        if _under(path, rules.benchmark_roots):
            reasons.append("benchmark_tree")
        elif _under(path, rules.notes_roots):
            reasons.append("harness_notes")
        elif _under(path, rules.data_roots):
            reasons.append("harness_data")
        elif _under(path, rules.foreign_roots):
            reasons.append("foreign_repo_clone")
    for text in _strings(tool_input):
        if rules.upstream is not None and (rules.upstream.fetched_by(text)
                                           or (fetching and rules.upstream.repo_url.search(text))):
            reasons.append("upstream_fetch")
        if _fetches_package(text, rules):
            reasons.append("package_source")
    return list(dict.fromkeys(reasons))


def find_hits(sidecar_path: str | Path | None, task: object, cwd: str | Path | None = None) -> list[dict]:
    """Every contamination hit in a trajectory sidecar's tool inputs, with its reason and input.

    ``cwd`` is the cell workspace; relative paths in tool inputs resolve against it.
    """
    if sidecar_path is None or not Path(sidecar_path).is_file():
        return [{"reason": "unscanned", "tool": None, "input": None}]
    rules = _rules(task)
    base = os.path.normpath(str(cwd)) if cwd else None
    hits = []
    for call in tolerant_jsonl(Path(sidecar_path).read_text()):
        for reason in _call_reasons(call.get("input"), rules, call.get("name"), base):
            hits.append({"reason": reason, "tool": call.get("name"), "input": call.get("input")})
    return hits


def scan(sidecar_path: str | Path | None, task: object, cwd: str | Path | None = None) -> bool:
    """True when the trajectory has any contamination hit, including an unscanned one."""
    return bool(find_hits(sidecar_path, task, cwd))
