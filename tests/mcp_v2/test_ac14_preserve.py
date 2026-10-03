import unittest

import harness

import sys as _sys
_AC = "AC-14"
print(harness.WITNESS[_AC], file=_sys.stderr)

CWD = str(harness.REPO_ROOT)


def setUpModule():
    harness.build_if_needed()


class AC14Preserve(unittest.TestCase):
    def test_read_unchanged(self):
        requests = [
            harness.initialize_request(1),
            harness.tools_call_request(2, "tilth_read", {"paths": ["src/types.rs"], "cwd": CWD}),
        ]
        res = harness.run_mcp([], requests)
        self.assertEqual(res.returncode, 0, msg=harness.WITNESS[_AC])
        for request_id in [2]:
            response = res.response_by_id(request_id)
            self.assertIsNotNone(response, msg=harness.WITNESS[_AC])
            self.assertFalse(harness.tool_is_error(response), msg=harness.WITNESS[_AC])


if __name__ == "__main__":
    unittest.main()
