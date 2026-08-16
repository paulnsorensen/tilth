import unittest

import harness

import sys as _sys
_AC = "AC-14"
print(harness.WITNESS[_AC], file=_sys.stderr)

CWD = str(harness.REPO_ROOT)


def setUpModule():
    harness.build_if_needed()


class AC14Preserve(unittest.TestCase):
    def test_list_and_grok_unchanged(self):
        # Under the trial `both` surface, the v1 registry (tilth_list/grok/deps)
        # must be preserved unchanged. RED today: the flag does not exist yet
        # (exit 2); GREEN once `--search-surface both` lands and preserves them.
        requests = [
            harness.initialize_request(1),
            harness.tools_call_request(2, "tilth_list", {"cwd": CWD}),
            harness.tools_call_request(3, "tilth_grok", {"target": "detect_file_type", "cwd": CWD}),
        ]
        res = harness.run_mcp(["--search-surface", "both"], requests)
        self.assertEqual(res.returncode, 0, msg=harness.WITNESS[_AC])

        list_response = res.response_by_id(2)
        grok_response = res.response_by_id(3)
        self.assertIsNotNone(list_response, msg=harness.WITNESS[_AC])
        self.assertIsNotNone(grok_response, msg=harness.WITNESS[_AC])
        self.assertFalse(harness.tool_is_error(list_response), msg=harness.WITNESS[_AC])
        self.assertFalse(harness.tool_is_error(grok_response), msg=harness.WITNESS[_AC])


if __name__ == "__main__":
    unittest.main()
