"""Adversarial MCP boundary checks for stateless continuations."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import harness


class ContinuationBoundary(unittest.TestCase):
    def setUp(self):
        harness.build_if_needed()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.cwd, check=True)
        (self.cwd / "fixture.ts").write_text(
            "export function root() {}\n"
            "function caller() { root(); }\n"
        )

    def call(self, entries, budget=4000):
        run = harness.run_mcp(
            [],
            [
                harness.initialize_request(1),
                harness.tools_call_request(
                    2,
                    "tilth_search",
                    {"cwd": str(self.cwd), "queries": entries, "budget": budget},
                ),
            ],
        )
        return run.response_by_id(2)

    def payload(self, entries, budget=4000):
        response = self.call(entries, budget)
        self.assertFalse(harness.tool_is_error(response), response)
        return json.loads(harness.tool_result_text(response))

    def test_malformed_follow_shapes_are_rejected(self):
        scope = str(self.cwd)
        cases = [
            ({"follow": None}, "follow hint requires kind"),
            ({"follow": {"kind": "fetch_callers"}}, "invalid follow hint"),
            ({"follow": {"kind": "unknown", "target": {}}}, "unknown continuation kind"),
            (
                {"follow": {"kind": "fetch_callers", "target": {"path": "fixture.ts"}}},
                "follow target requires line and name",
            ),
            (
                {"follow": {"kind": "fetch_callers", "target": {"path": "../fixture.ts", "line": 1, "name": "root", "scope": scope}}},
                "follow target requires a normalized path",
            ),
            (
                {"follow": {"kind": "fetch_callers", "target": {"path": "/etc/hosts", "line": 1, "name": "root", "scope": scope}}},
                "follow target requires a cwd-relative path",
            ),
            (
                {"follow": {"kind": "fetch_callers", "target": {"path": "fixture.ts", "line": 1, "name": "root", "scope": "/tmp"}}},
                "follow target scope does not match cwd",
            ),
            (
                {"follow": {"kind": "fetch_callers", "target": {"path": "fixture.ts", "line": 0, "name": "root", "scope": scope}}},
                "positive line",
            ),
            (
                {"follow": {"kind": "fetch_callers", "target": {"path": "fixture.ts", "line": 1, "name": "root", "scope": scope, "extra": True}}},
                "invalid follow hint",
            ),
            (
                {"query": "root", "follow": {"kind": "fetch_callers", "target": {"path": "fixture.ts", "line": 1, "name": "root", "scope": scope}}},
                "exactly one of query or follow",
            ),
            ({}, "exactly one of query or follow"),
        ]
        for entry, expected in cases:
            with self.subTest(entry=entry):
                response = self.call([entry])
                self.assertTrue(harness.tool_is_error(response), response)
                self.assertIn(expected, harness.tool_result_text(response))

    def test_follow_rejects_identity_drift_after_emission(self):
        first = self.payload([{"query": "root"}])
        hint = next(item for item in first["hints"] if item["kind"] == "fetch_callers")
        (self.cwd / "fixture.ts").write_text(
            "export function renamed() {}\n"
            "function caller() { renamed(); }\n"
        )
        response = self.call([{"follow": hint}])
        self.assertTrue(harness.tool_is_error(response), response)

    def test_mixed_query_and_follow_preserve_input_order(self):
        first = self.payload([{"query": "root"}])
        hint = next(item for item in first["hints"] if item["kind"] == "fetch_callers")
        payload = self.payload([{"follow": hint}, {"query": "caller"}])
        self.assertEqual(len(payload["results"]), 2)
        self.assertEqual(payload["results"][0]["resolved_as"], "fetch_callers")
        self.assertEqual(payload["results"][1]["query"], "caller")

    def test_unfittable_budget_returns_explicit_error(self):
        response = self.call([{"query": "root"}], budget=1)
        self.assertTrue(harness.tool_is_error(response), response)
        self.assertIn("budget", harness.tool_result_text(response).lower())

    def test_follow_budget_reduction_keeps_one_result_per_entry(self):
        first = self.payload([{"query": "root"}])
        hints = first["hints"][:2]
        response = self.call([{"follow": hint} for hint in hints], budget=160)
        self.assertFalse(harness.tool_is_error(response), response)
        payload = json.loads(harness.tool_result_text(response))
        self.assertEqual(len(payload["results"]), 2)
        self.assertIn("partial", [result["completeness"] for result in payload["results"]])
        self.assertNotIn("TIP:", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
