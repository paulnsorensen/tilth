"""JavaScript and TypeScript runners for native external tasks.

One `javascript` language covers both: jest, vitest and qunit transpile TypeScript themselves.
Grading never reads log text. Each runner writes a machine-readable report to a file the harness names,
and `normalize` turns it into `[entry id, passed]` pairs:

- mocha: the JSON reporter; the entry ID is the full title.
- qunit: a harness reporter appends one JSON line per test; the ID is `module > ... > test`.
- vitest: the JSON reporter; the ID is `<file> > <describe> > ... > <test>`.
- jest: `--json`; the ID is the test title.

Karma needs a real browser. The harness runs none, so karma tasks are refused.
"""

import fcntl
import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath

from . import data

# The dataset repositories date from 2022 to 2024. Their old toolchains (mocha 8, yargs 16) fail on the
# newest Node, so the harness runs every JavaScript task under one pinned Node 20 that it downloads.
NODE_VERSION = "20.20.2"
NODE_SHA256 = "df770b2a6f130ed8627c9782c988fda9669fa23898329a61a871e32f965e007d"
NODE_ARCHIVE = f"node-v{NODE_VERSION}-linux-x64.tar.xz"

QUNIT_REPORTER = (
    "const fs = require('fs');\n"
    "const out = process.env.TILTH_QUNIT_REPORT;\n"
    "QUnit.on('testEnd', (t) => fs.appendFileSync(out, "
    "JSON.stringify({id: t.fullName.join(' > '), passed: t.status === 'passed'}) + '\\n'));\n"
)


class UnsupportedRunner(Exception):
    pass


def runner_of(command: str) -> str:
    if "karma" in command:
        raise UnsupportedRunner("karma needs a real browser; the harness runs none")
    if "qunit" in command:
        return "qunit"
    if "mocha" in command:
        return "mocha"
    if "vitest" in command or "--reporter=verbose" in command:
        return "vitest"
    if "jest" in command or "--verbose" in command:
        return "jest"
    raise UnsupportedRunner(f"no known JavaScript test runner in: {command[:120]}")


def rewrite(command: str, runner: str, report: Path, reporter: Path) -> tuple[str, dict[str, str]]:
    """Point the runner at a JSON report; returns the new command and the extra env it needs."""
    if runner == "mocha":
        return re.sub(r"(?:-R|--reporter)[ =]tap", f"--reporter json --reporter-option output={report}", command), {}
    if runner == "qunit":
        return command.replace("qunit ", f"qunit --require {reporter} ", 1), {"TILTH_QUNIT_REPORT": str(report)}
    if runner == "vitest":
        return command.replace("--reporter=verbose", f"--reporter=json --outputFile={report}"), {}
    return command.replace("--verbose", f"--json --outputFile={report}"), {}


def _pairs(rows) -> list[list]:
    return [[name, status == "passed"] for name, status in rows if status in ("passed", "failed")]


def normalize(runner: str, raw: str, root: Path) -> str:
    """The runner's raw report as a JSON list of `[id, passed]` pairs."""
    if runner == "qunit":
        lines = [json.loads(line) for line in raw.splitlines() if line.strip()]
        return json.dumps([[row["id"], bool(row["passed"])] for row in lines])
    data = json.loads(raw)
    if runner == "mocha":
        rows = [(t["fullTitle"], "passed") for t in data.get("passes", [])]
        rows += [(t["fullTitle"], "failed") for t in data.get("failures", [])]
        return json.dumps(_pairs(rows))
    rows = []
    for suite in data.get("testResults", []):
        name = Path(suite["name"])
        for case in suite.get("assertionResults", []):
            title = case["title"]
            if runner == "vitest":
                path = name.relative_to(root).as_posix() if name.is_absolute() and root in name.parents else name.as_posix()
                title = " > ".join([path, *case.get("ancestorTitles", []), title])
            rows.append((title, case["status"]))
    return json.dumps(_pairs(rows))


def parse(report: str) -> list[tuple[str, bool]]:
    return [(name, passed) for name, passed in json.loads(report)]


# Files the runners read. Grading resets each one to base_commit, so the agent cannot steer a run through them.
CONFIG_PREFIXES = (
    "jest.config", "jest.setup", "vitest.", "vite.config", "babel.config", "karma.conf",
    ".mocharc", ".babelrc", ".npmrc", ".yarnrc", ".pnpmfile", ".qunitrc", "tsconfig",
)
# package.json keys that configure a runner or run commands.
RUNNER_KEYS = ("scripts", "jest", "mocha", "vitest", "babel", "ava", "nyc", "qunit", "pnpm", "packageManager")


def is_config(name: str) -> bool:
    return name.startswith(CONFIG_PREFIXES)


def reset_package_json(current: bytes | None, base: bytes | None) -> bytes | None:
    """``current`` with the runner keys of ``base``; other edits survive. None removes the file."""
    if base is None:
        return None
    if current is None:
        return base
    try:
        edited, original = json.loads(current), json.loads(base)
        if not isinstance(edited, dict) or not isinstance(original, dict):
            return base
    except ValueError:
        return base
    changed = False
    for key in RUNNER_KEYS:
        if edited.get(key) != original.get(key):
            changed = True
            if key in original:
                edited[key] = original[key]
            else:
                del edited[key]
    return (json.dumps(edited, indent=2) + "\n").encode() if changed else current


def install_argv(tree: Path, frozen: bool) -> list[str]:
    """The dependency install for the package manager the base tree's lockfile names; lifecycle scripts stay off."""
    if (tree / "pnpm-lock.yaml").is_file():
        return ["pnpm", "install", *(["--frozen-lockfile"] if frozen else []), "--ignore-scripts"]
    if (tree / "yarn.lock").is_file():
        return ["yarn", "install", *(["--frozen-lockfile"] if frozen else []), "--ignore-scripts", "--non-interactive"]
    return ["npm", "install", "--ignore-scripts"]


def node_bin() -> Path:
    """The ``bin`` directory of the harness-owned Node under the data directory; downloads it on first use.

    The archive must match the pinned SHA-256. Raises OSError when the download or the check fails.
    """
    root = data.data_dir() / "node"
    target = root / f"v{NODE_VERSION}"
    root.mkdir(parents=True, exist_ok=True)
    with open(root / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not (target / "bin" / "node").is_file():
            with tempfile.TemporaryDirectory(prefix="tilth-node-", dir=root) as temp:
                archive = Path(temp) / NODE_ARCHIVE
                with urllib.request.urlopen(f"https://nodejs.org/dist/v{NODE_VERSION}/{NODE_ARCHIVE}", timeout=120) as reply:
                    archive.write_bytes(reply.read())
                if hashlib.sha256(archive.read_bytes()).hexdigest() != NODE_SHA256:
                    raise OSError(f"{NODE_ARCHIVE} does not match its pinned SHA-256")
                with tarfile.open(archive) as tree:
                    tree.extractall(temp, filter="data")
                shutil.rmtree(target, ignore_errors=True)
                (Path(temp) / NODE_ARCHIVE.removesuffix(".tar.xz")).rename(target)
    return target / "bin"


def package_env(env: dict[str, str], cache: Path) -> None:
    """Node and non-interactive package manager settings with caches under ``cache``, not the real home."""
    env["PATH"] = f"{node_bin()}{os.pathsep}{env.get('PATH', '')}"
    env.update(
        CI="1", npm_config_cache=str(cache / "npm"), YARN_CACHE_FOLDER=str(cache / "yarn"),
        npm_config_store_dir=str(cache / "pnpm"), COREPACK_HOME=str(cache / "corepack"),
        npm_config_manage_package_manager_versions="false", npm_config_update_notifier="false",
        npm_config_audit="false", npm_config_fund="false",
    )


def module_dirs(tree: Path) -> list[str]:
    """Relative paths of every ``node_modules`` directory in ``tree``, without descending into one."""
    found = []
    for current, names, _ in os.walk(tree):
        if ".git" in names:
            names.remove(".git")
        for name in [n for n in names if n == "node_modules"]:
            names.remove(name)
            found.append((Path(current) / name).relative_to(tree).as_posix())
    return sorted(found)


def build_argv(tree: Path) -> list[str] | None:
    """The root ``build:packages`` script, which monorepos run on install to build workspace packages."""
    try:
        scripts = json.loads((tree / "package.json").read_text()).get("scripts", {})
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(scripts, dict) or "build:packages" not in scripts:
        return None
    manager = install_argv(tree, frozen=False)[0]
    return [manager, "run", "build:packages"]


def built_paths(tree: Path, base_paths: frozenset[str]) -> list[str]:
    """Files and directories in ``tree`` that the base commit lacks, outside ``node_modules``: build outputs.

    A directory the base lacks counts as one path.
    """
    known_directories = {parent.as_posix() for path in base_paths for parent in PurePosixPath(path).parents}
    found = []
    for current, names, files in os.walk(tree):
        relative = Path(current).relative_to(tree)
        for name in list(names):
            path = (relative / name).as_posix()
            if name in ("node_modules", ".git") or path in base_paths:
                names.remove(name)
            elif path not in known_directories:
                names.remove(name)
                found.append(path)
        found += [path for name in files if (path := (relative / name).as_posix()) not in base_paths]
    return sorted(found)
