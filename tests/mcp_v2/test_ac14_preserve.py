import unittest

import harness

import sys as _sys
_AC = "AC-14"
print(harness.WITNESS[_AC], file=_sys.stderr)

CWD = str(harness.REPO_ROOT)


def setUpModule():
    harness.build_if_needed()


class AC14Preserve(unittest.TestCase):
    def test_grok_and_deps_unchanged(self):
        requests = [
            harness.initialize_request(1),
            harness.tools_call_request(2, "tilth_deps", {"path": "src/types.rs", "cwd": CWD}),
            harness.tools_call_request(3, "tilth_grok", {"target": "detect_file_type", "cwd": CWD}),
        ]
        res = harness.run_mcp([], requests)
        self.assertEqual(res.returncode, 0, msg=harness.WITNESS[_AC])
        for request_id in [2, 3]:
            response = res.response_by_id(request_id)
            self.assertIsNotNone(response, msg=harness.WITNESS[_AC])
            self.assertFalse(harness.tool_is_error(response), msg=harness.WITNESS[_AC])

    def test_retired_directory_tools(self):
        for flags in [[], ["--edit"]]:
            with self.subTest(flags=flags):
                requests = [
                    harness.initialize_request(1),
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                    harness.tools_call_request(3, "tilth_list", {"cwd": CWD}),
                    harness.tools_call_request(4, "tilth_files", {"cwd": CWD}),
                ]
                res = harness.run_mcp(flags, requests)
                self.assertEqual(res.returncode, 0)
                names = {tool["name"] for tool in res.response_by_id(2)["result"]["tools"]}
                self.assertNotIn("tilth_list", names)
                self.assertNotIn("tilth_files", names)
                for request_id in [3, 4]:
                    response = res.response_by_id(request_id)
                    self.assertTrue(harness.tool_is_error(response))
                    text = response["result"]["content"][0]["text"]
                    self.assertIn("retired tool", text)
                    self.assertIn("shell ls/find", text)


if __name__ == "__main__":
    unittest.main()
