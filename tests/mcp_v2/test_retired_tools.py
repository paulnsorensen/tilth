"""Retired MCP tools stay out of the registry in both modes."""
import unittest

import harness


def setUpModule():
    harness.build_if_needed()


class RetiredTools(unittest.TestCase):
    def test_absent_from_registry(self):
        for flags in [[], ["--edit"]]:
            with self.subTest(flags=flags):
                result = harness.run_mcp(flags, [
                    harness.initialize_request(),
                    harness.tools_list_request(),
                ])
                self.assertEqual(result.returncode, 0)
                names = result.tool_names()
                for retired in ["tilth_diff", "tilth_grok", "tilth_files", "tilth_list"]:
                    self.assertNotIn(retired, names)
                self.assertIn("tilth_read", names)