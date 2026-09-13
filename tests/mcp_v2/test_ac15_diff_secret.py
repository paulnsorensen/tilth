"""Verify tilth_diff never inlines secret-file contents in an unscoped overview.

D1: an unscoped `tilth_diff` overview must not expose signatures or source
lines from a secret-named file. The shared `is_secret_file` policy already
protects incidental search previews; the diff path must apply it too.

The file-pair (`a`/`b`) seam is used because git-based sources run in the
server's project directory, while `a`/`b`/`patch` paths anchor under `cwd`.
This exercises the real MCP `tilth_diff` tool end to end.
"""
import os
from pathlib import Path
import tempfile
import unittest

import harness

SECRET_VALUE = "SYNTHETIC_SECRET_9f3a2b"
OLD_VALUE = "OLD_PLACEHOLDER_VALUE"


class AC15DiffSecret(unittest.TestCase):
    def setUp(self):
        harness.build_if_needed()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path(self.tmp.name)
        # Same basename on both sides so the secret-name policy applies
        # regardless of which side the diff parser records as the path.
        (self.cwd / "before").mkdir()
        (self.cwd / "after").mkdir()
        (self.cwd / "before" / "credentials.py").write_text(
            f'def connect(token="{OLD_VALUE}"):\n    return token\n'
        )
        (self.cwd / "after" / "credentials.py").write_text(
            f'def connect(token="{SECRET_VALUE}"):\n    return token\n'
        )
        self.env = dict(os.environ, XDG_CACHE_HOME=str(self.cwd / "cache"))

    def overview(self):
        run = harness.run_mcp([], [
            harness.initialize_request(1),
            harness.tools_call_request(2, "tilth_diff", {
                "cwd": str(self.cwd),
                "a": "before/credentials.py",
                "b": "after/credentials.py",
            }),
        ], env=self.env)
        response = run.response_by_id(2)
        self.assertIsNotNone(response, run.stderr)
        self.assertFalse(harness.tool_is_error(response), response)
        return harness.tool_result_text(response)

    def test_unscoped_overview_redacts_secret_file(self):
        out = self.overview()
        # The secret file must still be surfaced as changed.
        self.assertIn("credentials.py", out, out)
        # But its signatures/source must never be inlined incidentally.
        self.assertNotIn(SECRET_VALUE, out, f"secret value leaked in overview:\n{out}")
        self.assertNotIn(OLD_VALUE, out, f"old secret value leaked in overview:\n{out}")


if __name__ == "__main__":
    unittest.main()
