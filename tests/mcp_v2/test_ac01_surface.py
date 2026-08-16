import unittest

import harness

import sys as _sys
_AC = "AC-1"
print(harness.WITNESS[_AC], file=_sys.stderr)


def setUpModule():
    harness.build_if_needed()


class AC01Surface(unittest.TestCase):
    def _tools(self, flags):
        requests = [harness.initialize_request(1), harness.tools_list_request(2)]
        res = harness.run_mcp(flags, requests)
        self.assertEqual(res.returncode, 0)
        return res.tool_names()

    def test_v1_default(self):
        default_names = self._tools([])
        v1_names = self._tools(["--search-surface", "v1"])
        self.assertEqual(default_names, v1_names)
        self.assertNotIn("tilth_search_v2", default_names)

    def test_v1_explicit(self):
        names = self._tools(["--search-surface", "v1"])
        self.assertIn("tilth_search", names)
        self.assertNotIn("tilth_search_v2", names)

    def test_v2(self):
        names = self._tools(["--search-surface", "v2"])
        self.assertIn("tilth_search_v2", names)
        self.assertNotIn("tilth_search", names)

    def test_both(self):
        names = self._tools(["--search-surface", "both"])
        self.assertIn("tilth_search_v2", names)
        self.assertIn("tilth_search", names)


if __name__ == "__main__":
    unittest.main()
