import json
import unittest

import harness

import sys as _sys
_AC = "AC-6"
print(harness.WITNESS[_AC], file=_sys.stderr)

CWD = str(harness.REPO_ROOT)


def setUpModule():
    harness.build_if_needed()


class AC06Enrichment(unittest.TestCase):
    def _call(self, query):
        requests = [
            harness.initialize_request(1),
            harness.tools_call_request(
                2, "tilth_search_v2", {"queries": [{"query": query}], "cwd": CWD}
            ),
        ]
        res = harness.run_mcp(["--search-surface", "both"], requests)
        response = res.response_by_id(2)
        self.assertIsNotNone(response)
        return json.loads(harness.tool_result_text(response))

    def _assert_hint(self, query, kind):
        payload = self._call(query)
        hints = payload.get("hints", [])
        self.assertTrue(any(h.get("kind") == kind for h in hints))

    def test_unique_core(self):
        payload = self._call("detect_file_type")
        result = payload["results"][0]
        self.assertIn("core", result)

    def test_callers_continuation(self):
        self._assert_hint("detect_file_type", "callers")

    def test_callees_continuation(self):
        self._assert_hint("detect_file_type", "callees")

    def test_siblings_continuation(self):
        self._assert_hint("detect_file_type", "siblings")

    def test_tests_continuation(self):
        self._assert_hint("detect_file_type", "tests")

    def test_ambiguous_disambiguation(self):
        payload = self._call("run")
        result = payload["results"][0]
        self.assertIn("candidates", result)
        self.assertGreater(len(result["candidates"]), 1)
        hints = payload.get("hints", [])
        self.assertTrue(any(h.get("kind") == "disambiguate" for h in hints))
        self.assertNotIn("core", result)


if __name__ == "__main__":
    unittest.main()
