"""The fork has its own version identity, separate from upstream.

The fork version is `0.0.0-experimental`. `tilth --version` adds the git commit, so a
stale binary is visible. An upstream sync must not bring back upstream's number, and a
`v*` tag must not publish a release.
"""
import json
import os
import re
import shutil
import subprocess
import tomllib
import unittest

import harness

FORK_VERSION = "0.0.0-experimental"
VERSION_LINE = re.compile(r"^tilth 0\.0\.0-experimental(\+[0-9a-f]{7,40})?$")


def setUpModule():
    harness.build_if_needed()


def git_line(*args):
    proc = subprocess.run(["git", *args], cwd=harness.REPO_ROOT, capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else None


def head_sha():
    """HEAD of this checkout, or None when `build.rs` gives no suffix.

    `build.rs` gives no suffix without git, and when the git top level is not the package:
    a source archive inside another repository must not take that repository's commit.
    """
    if shutil.which("git") is None:
        return None
    top_level = git_line("rev-parse", "--show-toplevel")
    if top_level is None or os.path.realpath(top_level) != os.path.realpath(harness.REPO_ROOT):
        return None
    return git_line("rev-parse", "HEAD")


def cli_version():
    proc = subprocess.run([str(harness.BIN), "--version"], capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def has_push_tag_trigger(text):
    """True when a workflow's `on.push` trigger has a `tags` or `tags-ignore` filter.

    A line scan, not a YAML parser: it reads the block form that the workflows use, and a
    flow form (`on: {push: {tags: ...}}`) that names `tags` on the `on` or `push` line.
    """
    on_indent = push_indent = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        key, _, rest = stripped.partition(":")
        key = key.strip("'\"")
        if push_indent is not None and indent <= push_indent:
            push_indent = None
        if on_indent is not None and indent <= on_indent:
            on_indent = None
        if on_indent is None:
            if indent == 0 and key == "on":
                if re.search(r"\btags", rest):
                    return True
                on_indent = indent
        elif push_indent is None:
            if key == "push":
                if re.search(r"\btags", rest):
                    return True
                push_indent = indent
        elif key in ("tags", "tags-ignore"):
            return True
    return False


class VersionIdentity(unittest.TestCase):
    def test_cli_version_names_fork_version_and_head(self):
        line = cli_version()
        match = VERSION_LINE.fullmatch(line)
        self.assertIsNotNone(match, f"unexpected --version output: {line!r}")
        head = head_sha()
        if head is None:
            self.skipTest("no git metadata: the commit suffix is optional")
        suffix = match.group(1)
        self.assertIsNotNone(suffix, f"git checkout build has no commit suffix: {line!r}")
        self.assertTrue(
            head.startswith(suffix[1:]),
            f"{line!r} does not name HEAD {head}: {harness.BIN} is stale. Run `cargo build`.",
        )

    def test_mcp_server_info_matches_cli_version(self):
        result = harness.run_mcp([], [harness.initialize_request()])
        self.assertEqual(result.returncode, 0, result.stderr)
        server_info = result.responses[0]["result"]["serverInfo"]
        self.assertEqual(server_info["version"], cli_version().removeprefix("tilth "))

    def test_manifests_declare_fork_version(self):
        cargo = tomllib.loads((harness.REPO_ROOT / "Cargo.toml").read_text())
        npm = json.loads((harness.REPO_ROOT / "npm" / "package.json").read_text())
        self.assertEqual(cargo["package"]["version"], FORK_VERSION)
        self.assertEqual(npm["version"], FORK_VERSION)

    def test_no_tag_release_workflow(self):
        self.assertFalse((harness.REPO_ROOT / ".github" / "workflows" / "release.yml").exists())
        workflows = sorted((harness.REPO_ROOT / ".github" / "workflows").glob("*.y*ml"))
        self.assertTrue(workflows, "no workflow files found")
        tagged = [path.name for path in workflows if has_push_tag_trigger(path.read_text())]
        self.assertEqual(tagged, [], "a workflow runs on a tag push; the fork has no tagged releases")

    def test_push_tag_trigger_scan(self):
        self.assertTrue(has_push_tag_trigger("on:\n  push:\n    tags: ['v*']\n"))
        self.assertTrue(has_push_tag_trigger("on:\n  push:\n    branches: [main]\n    tags-ignore: [x]\n"))
        self.assertTrue(has_push_tag_trigger("'on':\n  push:\n    tags:\n      - 'v*'\n"))
        self.assertTrue(has_push_tag_trigger("on: {push: {tags: ['v*']}}\n"))
        self.assertFalse(has_push_tag_trigger("on:\n  push:\n    branches: [main]\n  pull_request:\n"))
        self.assertFalse(has_push_tag_trigger("on:\n  pull_request:\n    tags: [x]\njobs:\n  tags:\n"))
        self.assertFalse(has_push_tag_trigger("on:\n  push:\n    branches: [main]\njobs:\n  tags:\n    runs-on: x\n"))
