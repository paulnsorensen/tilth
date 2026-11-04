"""A mixed batch read marks seen only the lines the response shows.

A range part followed by an outline part (`big.rs` is over the auto-outline
size) once leaked the outline's `N:` lines into the range part's file, so a
write to a never-shown line applied and a re-read of it returned a stub.
"""
import re
import tempfile
import unittest
from pathlib import Path

import harness


def setUpModule():
    harness.build_if_needed()


SMALL = "".join(f"let value_{i} = {i};\n" for i in range(1, 61))
BIG = "".join(f"let big_{i} = \"{'x' * 30}\";\n" for i in range(1, 3001))


class ReadProvenance(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cwd = self.directory.name
        self.small = Path(self.cwd) / "a.rs"
        self.small.write_text(SMALL)
        (Path(self.cwd) / "big.rs").write_text(BIG)
        self.mixed = harness.tools_call_request(
            2, "tilth_read", {"cwd": self.cwd, "paths": ["a.rs#10-20", "big.rs"]})

    def session(self, *calls):
        result = harness.run_mcp([], [harness.initialize_request(), self.mixed, *calls])
        return result

    def tag(self):
        text = harness.tool_result_text(self.session().response_by_id(2))
        return re.search(r"\[a\.rs#([0-9A-F]{4})\]", text).group(1)

    def test_write_to_a_never_shown_line_is_refused(self):
        edit = {"cwd": self.cwd, "edits": [{"path": "a.rs", "tag": self.tag(), "ops": [
            {"op": "replace", "start": 31, "end": 31, "content": "let value_31 = 0;\n"}]}]}
        write = harness.tools_call_request(3, "tilth_write", edit)
        refused = self.session(write).response_by_id(3)
        self.assertTrue(harness.tool_is_error(refused), harness.tool_result_text(refused))
        self.assertEqual(self.small.read_text(), SMALL, "a refused edit must not write")

    def test_reread_of_an_unseen_line_returns_content(self):
        reread = harness.tools_call_request(
            3, "tilth_read", {"cwd": self.cwd, "paths": ["a.rs#43-43"]})
        text = harness.tool_result_text(self.session(reread).response_by_id(3))
        self.assertNotIn("unchanged since", text)
        self.assertIn("value_43 = 43", text)


if __name__ == "__main__":
    unittest.main()
