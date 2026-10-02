"""Retired MCP tools stay out of the registry; the edit surface is the default.

`--edit` is a retired no-op flag. The server must still accept it.
"""
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

    def test_edit_surface_without_flag(self):
        for flags in [[], ["--edit"]]:
            with self.subTest(flags=flags):
                result = harness.run_mcp(flags, [
                    harness.initialize_request(),
                    harness.tools_list_request(),
                ])
                self.assertEqual(result.returncode, 0)
                self.assertEqual(
                    sorted(result.tool_names()),
                    ["tilth_deps", "tilth_read", "tilth_search", "tilth_write"],
                )
