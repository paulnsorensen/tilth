import json
import unittest

import harness

import sys as _sys
_AC = "AC-3"
print(harness.WITNESS[_AC], file=_sys.stderr)

CWD = str(harness.REPO_ROOT)


def setUpModule():
    harness.build_if_needed()


class AC03Request(unittest.TestCase):
    def _call(self, queries):
        requests = [
            harness.initialize_request(1),
            harness.tools_call_request(2, "tilth_search_v2", {"queries": queries, "cwd": CWD}),
        ]
        res = harness.run_mcp(["--search-surface", "both"], requests)
        return res.response_by_id(2)

    def _assert_valid_batch(self, queries):
        response = self._call(queries)
        self.assertIsNotNone(response)
        payload = json.loads(harness.tool_result_text(response))
        self.assertIn("results", payload)
        self.assertEqual(len(payload["results"]), len(queries))
        for query, result in zip(queries, payload["results"]):
            self.assertEqual(result.get("query"), query["query"])

    def test_file_path(self):
        self._assert_valid_batch([{"query": "src/mcp/mod.rs"}])

    def test_directory_path(self):
        self._assert_valid_batch([{"query": "src/search"}])

    def test_glob_path(self):
        self._assert_valid_batch([{"query": "*.rs", "glob": "src/**/*.rs"}])

    def test_mixed_batch(self):
        self._assert_valid_batch(
            [
                {"query": "src/mcp/mod.rs"},
                {"query": "detect_file_type"},
                {"query": "unknown tool"},
            ]
        )

    def test_invalid_empty_batch(self):
        response = self._call([])
        self.assertIsNotNone(response)
        self.assertTrue(harness.tool_is_error(response))

    def test_invalid_oversize_batch(self):
        queries = [{"query": f"term_{i}"} for i in range(11)]
        response = self._call(queries)
        self.assertIsNotNone(response)
        self.assertTrue(harness.tool_is_error(response))


if __name__ == "__main__":
    unittest.main()
