"""Copy emitted hints through the real MCP request boundary."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import harness


class Continuations(unittest.TestCase):
    def setUp(self):
        harness.build_if_needed()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.cwd, check=True)
        (self.cwd / "fixture.ts").write_text(
            "import { leaf } from './helper';\n"
            "export function root() { leaf(); }\n"
            "function production_caller() { root(); }\n"
            "function sibling() {}\n"
        )
        (self.cwd / "helper.ts").write_text("export function leaf() {}\n")
        (self.cwd / "fixture.test.ts").write_text(
            "import { root } from './fixture';\nfunction test_root() { root(); }\n"
        )
        (self.cwd / "other.ts").write_text(
            "function root() {}\nfunction wrong_caller() { root(); }\n"
        )

    def run_entries(self, entries, budget=4000):
        result = harness.run_mcp([], [
            harness.initialize_request(1),
            harness.tools_call_request(2, "tilth_search", {
                "cwd": str(self.cwd), "queries": entries, "budget": budget,
            }),
        ]).response_by_id(2)
        self.assertFalse(harness.tool_is_error(result), result)
        return json.loads(harness.tool_result_text(result))

    def test_all_hints_follow_in_order_and_preserve_scope(self):
        first = self.run_entries([{
            "query": "root", "glob": "{fixture.ts,fixture.test.ts,helper.ts}",
        }])
        hints = first["hints"]
        expected = ["production_caller", "leaf", "sibling", "test_root", "helper.ts"]
        self.assertEqual([h["kind"] for h in hints], [
            "fetch_callers", "fetch_callees", "fetch_siblings", "fetch_tests", "fetch_dependencies",
        ])
        requests = [harness.initialize_request(1)]
        for req_id in range(2, 5):
            requests.append(harness.tools_call_request(req_id, "tilth_search", {
                "cwd": str(self.cwd), "queries": [{"follow": hint} for hint in hints],
            }))
        run = harness.run_mcp([], requests)
        for req_id in range(2, 5):
            response = run.response_by_id(req_id)
            self.assertFalse(harness.tool_is_error(response), response)
            text = harness.tool_result_text(response)
            payload = json.loads(text)
            self.assertNotIn("TIP:", text)
            self.assertEqual(payload["hints"], [])
            self.assertEqual(len(payload["results"]), 5)
            for result, needle, hint in zip(payload["results"], expected, hints):
                self.assertEqual(result["resolved_as"], hint["kind"])
                self.assertEqual(result["target"], hint["target"])
                self.assertEqual(result["status"], "ok")
                if hint["kind"] == "fetch_dependencies":
                    impact = result["dependency_impact"]
                    identities = impact["imports"] + impact["dependents"]
                else:
                    items = result["items"]
                    self.assertEqual(result["total_found"], len(items))
                    identities = [
                        value
                        for item in items
                        for value in (item.get("name"), item.get("path"))
                        if value is not None
                    ]
                self.assertIn(needle, identities)
                self.assertNotIn("wrong_caller", identities)

    def test_noncode_filename_searches_references(self):
        (self.cwd / "uv.lock").write_text("not a reference\n")
        (self.cwd / "notes.md").write_text("Use uv.lock to pin versions.\n")
        result = self.run_entries([{"query": "uv.lock"}])["results"][0]
        self.assertEqual(result["status"], "ok")
        self.assertIn("Use uv.lock to pin versions.", result["preview"])
        self.assertNotIn("not a reference", result["preview"])

    def test_budget_and_legacy_selectors(self):
        for selector in ("kind", "expand", "context"):
            entry = {"query": "root", selector: "symbol"}
            response = harness.run_mcp([], [
                harness.tools_call_request(1, "tilth_search", {
                    "cwd": str(self.cwd), "queries": [entry],
                }),
            ]).response_by_id(1)
            self.assertTrue(harness.tool_is_error(response), response)
        payload = self.run_entries([{"query": "^absent$"}], budget=100)
        self.assertEqual(payload["results"][0]["status"], "no_match")
        self.assertLessEqual((len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()) + 3) // 4, 100)


if __name__ == "__main__":
    unittest.main()
