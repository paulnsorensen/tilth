import json
import unittest

import harness

import sys as _sys
_AC = "AC-7"
print(harness.WITNESS[_AC], file=_sys.stderr)

CWD = str(harness.REPO_ROOT)

PARTIAL_ROWS = {"cold_partial", "stale_partial", "lock_partial", "deadline_partial"}


def setUpModule():
    harness.build_if_needed()


class AC07Dependency(unittest.TestCase):
    def _call(self):
        requests = [
            harness.initialize_request(1),
            harness.tools_call_request(
                2, "tilth_search_v2", {"queries": [{"query": "detect_file_type"}], "cwd": CWD}
            ),
        ]
        res = harness.run_mcp(["--search-surface", "both"], requests)
        response = res.response_by_id(2)
        self.assertIsNotNone(response)
        return json.loads(harness.tool_result_text(response))

    def _assert_row(self, row):
        payload = self._call()
        result = payload["results"][0]
        dependency = result["dependency_impact"]
        self.assertIn("coverage", dependency)
        self.assertNotEqual(dependency.get("coverage"), "stale")
        if row in PARTIAL_ROWS:
            hints = payload.get("hints", [])
            self.assertTrue(any(h.get("kind") == "dependency_continuation" for h in hints))
        else:
            self.assertEqual(dependency["coverage"], "complete")

    def test_cold_partial(self):
        self._assert_row("cold_partial")

    def test_stale_partial(self):
        self._assert_row("stale_partial")

    def test_lock_partial(self):
        self._assert_row("lock_partial")

    def test_corrupt_rebuild(self):
        self._assert_row("corrupt_rebuild")

    def test_deadline_partial(self):
        self._assert_row("deadline_partial")

    def test_fresh_complete(self):
        self._assert_row("fresh_complete")

if __name__ == "__main__":
    unittest.main()
