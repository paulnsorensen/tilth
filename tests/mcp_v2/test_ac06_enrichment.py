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
                2, "tilth_search", {"queries": [{"query": query}], "cwd": CWD}
            ),
        ]
        res = harness.run_mcp([], requests)
        response = res.response_by_id(2)
        self.assertIsNotNone(response)
        return json.loads(harness.tool_result_text(response))

    def _assert_follows(self, query, kind):
        payload = self._call(query)
        self.assertNotIn("hints", payload)
        target = payload["results"][0]["target"]
        requests = [
            harness.initialize_request(1),
            harness.tools_call_request(
                2,
                "tilth_search",
                {"queries": [{"follow": {"kind": kind, "target": target}}], "cwd": CWD},
            ),
        ]
        response = harness.run_mcp([], requests).response_by_id(2)
        self.assertFalse(harness.tool_is_error(response), response)
        followed = json.loads(harness.tool_result_text(response))
        self.assertEqual(followed["results"][0]["resolved_as"], kind)

    def test_unique_core(self):
        payload = self._call("detect_file_type")
        result = payload["results"][0]
        self.assertIn("core", result)

    def test_callers_continuation(self):
        self._assert_follows("detect_file_type", "fetch_callers")

    def test_callees_continuation(self):
        self._assert_follows("detect_file_type", "fetch_callees")

    def test_siblings_continuation(self):
        self._assert_follows("detect_file_type", "fetch_siblings")

    def test_tests_continuation(self):
        self._assert_follows("detect_file_type", "fetch_tests")

    def test_ambiguous_disambiguation(self):
        payload = self._call("run")
        result = payload["results"][0]
        self.assertIn("candidates", result)
        self.assertGreater(len(result["candidates"]), 1)
        self.assertNotIn("hints", payload)
        self.assertNotIn("core", result)


if __name__ == "__main__":
    unittest.main()
