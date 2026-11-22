"""The fork has its own version identity, separate from upstream.

The fork version is `0.0.0-experimental`. `tilth --version` adds the git commit, so a
stale binary is visible. An upstream sync must not bring back upstream's number, and a
`v*` tag must not publish a release.
"""
import json
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


def head_sha():
    if shutil.which("git") is None:
        return None
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=harness.REPO_ROOT, capture_output=True, text=True
    )
    return proc.stdout.strip() if proc.returncode == 0 else None


def cli_version():
    proc = subprocess.run([str(harness.BIN), "--version"], capture_output=True, text=True, check=True)
    return proc.stdout.strip()


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
        self.assertTrue(head.startswith(suffix[1:]), f"{line!r} does not name HEAD {head}")

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
