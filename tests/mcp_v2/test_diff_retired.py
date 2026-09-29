"""The retired MCP diff tool directs callers to Git in both modes."""
import unittest

import harness


def setUpModule():
    harness.build_if_needed()


class DiffRetired(unittest.TestCase):
    def test_absent_from_registry(self):
        for flags in [[], ["--edit"]]:
            with self.subTest(flags=flags):
                result = harness.run_mcp(flags, [
                    harness.initialize_request(),
                    harness.tools_list_request(),
                ])
                self.assertEqual(result.returncode, 0)
                self.assertNotIn("tilth_diff", result.tool_names())
                self.assertIn("tilth_read", result.tool_names())

    def test_calls_retire_before_argument_validation(self):
        for flags in [[], ["--edit"]]:
            for arguments in [{}, {"cwd": str(harness.REPO_ROOT)},
                              {"cwd": 42, "budget": 0, "source": []}]:
                with self.subTest(flags=flags, arguments=arguments):
                    result = harness.run_mcp(flags, [
                        harness.initialize_request(),
                        harness.tools_call_request(2, "tilth_diff", arguments),
                    ])
                    self.assertEqual(result.returncode, 0)
                    response = result.response_by_id(2)
                    self.assertTrue(harness.tool_is_error(response))
                    text = harness.tool_result_text(response)
                    self.assertIn("retired tool", text)
                    self.assertIn("git diff", text)
                    self.assertIn("git log", text)
