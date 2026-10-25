"""Attack: the same capped tilth_search on the same tree returns the same bytes.

A parallel walker that stops at a match cap kept whichever matches its threads
found first. Search output decides which lines count as displayed for the edit
gate, so run-to-run drift changed edit refusals. The tree below holds far more
matching files than the cap, spread over directories.
"""
import tempfile
import unittest
from pathlib import Path

import harness

RUNS = 15


def setUpModule():
    harness.build_if_needed()


class SearchDeterminism(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for d in range(8):
            folder = self.root / f"d{d}"
            folder.mkdir()
            for f in range(60):
                (folder / f"f{f:02}.rs").write_text(
                    f"fn helper_{d}_{f}() {{}}\n// capsule_marker {d} {f}\n"
                )

    def search(self, query):
        call = harness.tools_call_request(
            2, "tilth_search", {"cwd": str(self.root), "queries": [{"query": query}]})
        out = harness.run_mcp([], [harness.initialize_request(), call], cwd=str(self.root))
        return harness.tool_result_text(out.response_by_id(2))

    def assert_stable(self, query):
        first = self.search(query)
        self.assertIn(query, first)
        for run in range(RUNS):
            self.assertEqual(self.search(query), first, f"{query!r} output changed on run {run}")

    def test_capped_content_search_is_byte_identical(self):
        self.assert_stable("capsule_marker")

    def test_capped_prefix_query_is_byte_identical(self):
        self.assert_stable("fn helper")


if __name__ == "__main__":
    unittest.main()
