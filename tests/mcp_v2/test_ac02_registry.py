import unittest

import harness

import sys as _sys
_AC = "AC-2"
print(harness.WITNESS[_AC], file=_sys.stderr)

APPROVED_V2_EXTRA_VERBS = {"tilth_search_v2"}
BASE_VERBS = {
    "tilth_search",
    "tilth_read",
    "tilth_list",
    "tilth_deps",
    "tilth_grok",
    "tilth_diff",
}


def setUpModule():
    harness.build_if_needed()


class AC02Registry(unittest.TestCase):
    def test_both_registry_shape(self):
        requests = [harness.initialize_request(1), harness.tools_list_request(2)]
        res = harness.run_mcp(["--search-surface", "both"], requests)
        names = res.tool_names()
        self.assertIn("tilth_search_v2", names)
        self.assertIn("tilth_list", names)
        extra = set(names) - BASE_VERBS
        self.assertEqual(extra, APPROVED_V2_EXTRA_VERBS)


if __name__ == "__main__":
    unittest.main()
