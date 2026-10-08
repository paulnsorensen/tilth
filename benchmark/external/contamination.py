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
_PIP_COMMAND = re.compile(r"\b(?:pip3?|uv\s+pip|python3?\s+-m\s+pip)\s+(?:download|install)\b([^;&|\n]*)")
_UV_ADD = re.compile(r"\buv\s+add\b([^;&|\n]*)")
_GO_COMMAND = re.compile(r"\bgo\s+(?:get|install|mod\s+download)\b([^;&|\n]*)")
_GITHUB_REPO = re.compile(r"github\.com[/:]([\w.-]+/[\w.-]+?)(?:\.git)?/?$")


@dataclass(frozen=True)
class _Rules:
    """What counts as a hit for one task's cell."""

    benchmark_roots: tuple[str, ...]
    data_roots: tuple[str, ...]
    foreign_roots: tuple[str, ...]
    upstream: re.Pattern | None
    packages: tuple[str, ...]
    go_module: str | None


def _roots(path: Path) -> tuple[str, ...]:
    return tuple(dict.fromkeys((os.path.normpath(str(path)), os.path.realpath(path))))


def _normalize_package(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _rules(task: object) -> _Rules:
    slug, packages, go_module = None, (), None
    if isinstance(task, ExternalTask):
        slug = task.upstream_repo
        if task.language == "go":
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
        hosts = r"(?:github\.com[/:]|codeload\.github\.com/|raw\.githubusercontent\.com/|api\.github\.com/repos/)"
        upstream = re.compile(hosts + re.escape(slug) + r"(?:\.git)?(?=$|[/\s'\"#?@:])", re.IGNORECASE)
    return _Rules(
        benchmark_roots=_roots(config.BENCHMARK_DIR),
        data_roots=_roots(data.data_dir()),
        foreign_roots=_roots(config.REPOS_DIR) if isinstance(task, ExternalTask) else (),
        upstream=upstream,
        packages=tuple(_normalize_package(name) for name in packages),
        go_module=go_module,
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


def _paths(text: str) -> Iterator[str]:
    """Absolute paths named in ``text``, after ``~``, ``$HOME``, and ``..`` normalization."""
    home = os.path.expanduser("~")
    for token in _PATH_SEPARATORS.split(_HOME_REFERENCE.sub(home, text)):
        token = token.split("#", 1)[0]
        if token == "~" or token.startswith("~/"):
            token = home + token[1:]
        if token.startswith("/"):
            yield os.path.normpath(token)


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


def _fetches_package(text: str, rules: _Rules) -> bool:
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


def _call_reasons(tool_input: object, rules: _Rules) -> list[str]:
    reasons: list[str] = []
    for text in _strings(tool_input):
        for path in _paths(text):
            if _under(path, rules.benchmark_roots):
                reasons.append("benchmark_tree")
            elif _under(path, rules.data_roots):
                reasons.append("harness_data")
            elif _under(path, rules.foreign_roots):
                reasons.append("foreign_repo_clone")
        if rules.upstream is not None and rules.upstream.search(text):
            reasons.append("upstream_fetch")
        if _fetches_package(text, rules):
            reasons.append("package_source")
    return list(dict.fromkeys(reasons))


def find_hits(sidecar_path: str | Path | None, task: object) -> list[dict]:
    """Every contamination hit in a trajectory sidecar's tool inputs, with its reason and input."""
    if sidecar_path is None or not Path(sidecar_path).is_file():
        return [{"reason": "unscanned", "tool": None, "input": None}]
    rules = _rules(task)
    hits = []
    for call in tolerant_jsonl(Path(sidecar_path).read_text()):
        for reason in _call_reasons(call.get("input"), rules):
            hits.append({"reason": reason, "tool": call.get("name"), "input": call.get("input")})
    return hits


def scan(sidecar_path: str | Path | None, task: object) -> bool:
    """True when the trajectory has any contamination hit, including an unscanned one."""
    return bool(find_hits(sidecar_path, task))
