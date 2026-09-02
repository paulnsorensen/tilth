import json
import unittest

import harness

import sys as _sys
_AC = "AC-4"
print(harness.WITNESS[_AC], file=_sys.stderr)

CWD = str(harness.REPO_ROOT)

ROUTE_EXPECTATIONS = {
    "exact_path": ("src/mcp/mod.rs", "path"),
    "unique_symbol": ("detect_file_type", "symbol"),
    "literal": ("DO NOT re-read expanded search content", "literal"),
    "regex_fallback": (r"fn\s+detect_file_type", "regex"),
    "ambiguous_symbol": ("run", "ambiguous"),
    "miss": ("tilth_absent_probe_" + "91c6a4", "miss"),
}


def setUpModule():
    harness.build_if_needed()


class AC04Routing(unittest.TestCase):
    def _call(self, query, glob=None):
        entry = {"query": query}
        if glob is not None:
            entry["glob"] = glob
        requests = [
            harness.initialize_request(1),
            harness.tools_call_request(
                2, "tilth_search_v2", {"queries": [entry], "cwd": CWD}
            ),
        ]
        res = harness.run_mcp(["--search-surface", "both"], requests)
        return res.response_by_id(2)

    def _assert_route(self, row):
        query, expected_route = ROUTE_EXPECTATIONS[row]
        response = self._call(query)
        self.assertIsNotNone(response)
        payload = json.loads(harness.tool_result_text(response))
        result = payload["results"][0]
        self.assertEqual(result.get("resolved_as"), expected_route)

    def test_exact_path(self):
        self._assert_route("exact_path")

    def test_unique_symbol(self):
        self._assert_route("unique_symbol")

    def test_usage_only_identifier_in_exact_file_ignores_external_definition(self):
        response = self._call(
            "SearchTelemetryRecord", "src/mcp/tools/search_v2.rs"
        )
        self.assertIsNotNone(response)
        result = json.loads(harness.tool_result_text(response))["results"][0]
        self.assertEqual(result["resolved_as"], "literal")
        self.assertEqual(result["status"], "ok")
        self.assertIn("src/mcp/tools/search_v2.rs:", result["preview"])
        self.assertNotIn("src/telemetry.rs:", result["preview"])
        self.assertIn(
            "use crate::telemetry::{SearchTelemetryRecord, TelemetrySink};",
            result["preview"],
        )

    def test_regex_in_exact_file_stays_bounded_to_requested_path(self):
        response = self._call(
            "SearchTelemetryRecord.*", "src/mcp/tools/search_v2.rs"
        )
        self.assertIsNotNone(response)
        result = json.loads(harness.tool_result_text(response))["results"][0]
        self.assertEqual(result["resolved_as"], "regex")
        self.assertEqual(result["status"], "ok")
        self.assertIn("src/mcp/tools/search_v2.rs:", result["preview"])
        self.assertNotIn("src/telemetry.rs:", result["preview"])
        self.assertIn(
            "use crate::telemetry::{SearchTelemetryRecord, TelemetrySink};",
            result["preview"],
        )

    def test_literal(self):
        self._assert_route("literal")

    def test_regex_fallback(self):
        self._assert_route("regex_fallback")

    def test_ambiguous_symbol(self):
        self._assert_route("ambiguous_symbol")

    def test_miss(self):
        self._assert_route("miss")


if __name__ == "__main__":
    unittest.main()
