"""Served tool descriptions are the bytes of their prompts/tools/*.md files.

The guard compares against the files, not frozen bytes, so a rewrite of a
prompt file still passes as long as the binary serves what the file holds.
"""
import unittest

import harness

PROMPT_FILES = {
    "tilth_read": "read.md",
    "tilth_search": "search.md",
    "tilth_write": "write.md",
}


def setUpModule():
    harness.build_if_needed()


class ToolDescriptionsStable(unittest.TestCase):
    def test_descriptions_match_prompt_files(self):
        result = harness.run_mcp([], [
            harness.initialize_request(),
            harness.tools_list_request(),
        ])
        self.assertEqual(result.returncode, 0)
        response = result.response_by_id(2)
        self.assertIsNotNone(response)
        served = {t["name"]: t["description"] for t in response["result"]["tools"]}
        for name, filename in PROMPT_FILES.items():
            with self.subTest(tool=name):
                expected = (harness.REPO_ROOT / "prompts" / "tools" / filename).read_bytes()
                self.assertIn(name, served)
                self.assertEqual(served[name].encode("utf-8"), expected)
