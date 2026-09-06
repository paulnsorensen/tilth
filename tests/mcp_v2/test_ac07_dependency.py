"""Verify live dependency evidence through MCP."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import harness


class AC07Dependency(unittest.TestCase):
    def setUp(self):
        harness.build_if_needed()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.cwd, check=True)
        (self.cwd / "target.ts").write_text("export function target() {}\n")
        (self.cwd / "consumer.ts").write_text(
            "import { target } from './target';\nfunction consumer() { target(); }\n"
        )
        self.env = dict(os.environ, XDG_CACHE_HOME=str(self.cwd / "cache"))

    def call(self):
        run = harness.run_mcp([], [
            harness.initialize_request(1),
            harness.tools_call_request(2, "tilth_search", {
                "cwd": str(self.cwd), "queries": [{"query": "target"}]
            }),
        ], env=self.env)
        response = run.response_by_id(2)
        self.assertFalse(harness.tool_is_error(response), response)
        return json.loads(harness.tool_result_text(response))

    def test_fresh_complete_and_removed_edge(self):
        first = self.call()["results"][0]["dependency_impact"]
        self.assertEqual(first["coverage"], "complete")
        self.assertEqual(first["dependents"], ["consumer.ts"])
        (self.cwd / "consumer.ts").write_text("function consumer() {}\n")
        second = self.call()["results"][0]["dependency_impact"]
        self.assertEqual(second["coverage"], "complete")
        self.assertEqual(second["dependents"], [])

    def test_unavailable_index_retains_core_and_continuation(self):
        cache_file = self.cwd / "not_a_directory"
        cache_file.write_text("blocked")
        self.env["XDG_CACHE_HOME"] = str(cache_file)
        payload = self.call()
        result = payload["results"][0]
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["completeness"], "partial")
        self.assertIn("function target", result["core"])
        self.assertEqual(result["dependency_impact"]["coverage"], "partial")
        self.assertEqual(result["dependency_impact"]["index_state"], "unavailable")
        self.assertIn("fetch_dependencies", [h["kind"] for h in payload["hints"]])


if __name__ == "__main__":
    unittest.main()