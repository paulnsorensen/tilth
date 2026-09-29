"""Tests for scripts/verify.py sccache setup and command dispatch."""

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFY = REPO_ROOT / "scripts" / "verify.py"
CONTROLLED_VARIABLES = (
    "RUSTC_WRAPPER",
    "CARGO_BUILD_RUSTC_WRAPPER",
    "RUSTC_WORKSPACE_WRAPPER",
    "CARGO_BUILD_RUSTC_WORKSPACE_WRAPPER",
    "CARGO_INCREMENTAL",
)
PRINT_ENV = [
    sys.executable,
    "-c",
    "import json, os; print(json.dumps({k: os.environ.get(k) for k in ('RUSTC_WRAPPER', 'CARGO_INCREMENTAL')}))",
]


class VerifyTest(unittest.TestCase):
    def setUp(self):
        self.bin_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.bin_dir.cleanup)
        self.empty_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.empty_dir.cleanup)
        self.sccache = Path(self.bin_dir.name) / "sccache"
        self.sccache.write_text("#!/bin/sh\nexec \"$@\"\n")
        self.sccache.chmod(self.sccache.stat().st_mode | stat.S_IXUSR)

    def run_verify(self, overrides, *, with_sccache=True, command=PRINT_ENV):
        environment = {
            key: value for key, value in os.environ.items() if key not in CONTROLLED_VARIABLES
        }
        environment["PATH"] = self.bin_dir.name if with_sccache else self.empty_dir.name
        environment.update(overrides)
        return subprocess.run(
            [sys.executable, str(VERIFY), *command],
            env=environment,
            capture_output=True,
            text=True,
        )

    def child_env(self, overrides, **kwargs):
        result = self.run_verify(overrides, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_enables_sccache_on_path(self):
        self.assertEqual(
            self.child_env({}),
            {"RUSTC_WRAPPER": str(self.sccache), "CARGO_INCREMENTAL": "0"},
        )

    def test_leaves_environment_without_sccache(self):
        self.assertEqual(
            self.child_env({}, with_sccache=False),
            {"RUSTC_WRAPPER": None, "CARGO_INCREMENTAL": None},
        )

    def test_empty_wrapper_opts_out(self):
        self.assertEqual(
            self.child_env({"RUSTC_WRAPPER": ""}),
            {"RUSTC_WRAPPER": "", "CARGO_INCREMENTAL": None},
        )

    def test_explicit_wrapper_is_preserved(self):
        for name in CONTROLLED_VARIABLES[1:4]:
            with self.subTest(name=name):
                self.assertEqual(
                    self.child_env({name: "/opt/other-wrapper"}),
                    {"RUSTC_WRAPPER": None, "CARGO_INCREMENTAL": None},
                )

    def test_incremental_request_opts_out(self):
        self.assertEqual(
            self.child_env({"CARGO_INCREMENTAL": "1"}),
            {"RUSTC_WRAPPER": None, "CARGO_INCREMENTAL": "1"},
        )

    def test_propagates_command_failure(self):
        result = self.run_verify({}, command=[sys.executable, "-c", "raise SystemExit(7)"])
        self.assertEqual(result.returncode, 7)


if __name__ == "__main__":
    unittest.main()
