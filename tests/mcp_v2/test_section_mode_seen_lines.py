"""A section read in mode=signature or mode=stripped marks its lines seen.

An explicit `path#a-b` suffix wins over a whole-file shape mode, so the
response prints the numbered range under `[path#TAG]`. The read once recorded
no seen lines for it, so a `tilth_write` inside the range failed with
"line N was never displayed under this tag". A whole-file signature view shows
no numbered lines, so it must still mark nothing seen.
"""
import re
import tempfile
import unittest
from pathlib import Path

import harness


def setUpModule():
    harness.build_if_needed()


SMALL = "".join(f"let value_{i} = {i};\n" for i in range(1, 61))
OTHER = "".join(f"let other_{i} = {i};\n" for i in range(1, 11))
EDITED = SMALL.replace("let value_15 = 15;", "let value_15 = 0;")


class SectionModeSeenLines(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cwd = self.directory.name
        self.small = Path(self.cwd) / "a.rs"
        self.small.write_text(SMALL)
        (Path(self.cwd) / "b.rs").write_text(OTHER)

    def read(self, paths, mode=None):
        arguments = {"cwd": self.cwd, "paths": paths}
        if mode is not None:
            arguments["mode"] = mode
        return harness.tools_call_request(2, "tilth_read", arguments)

    def tag(self):
        plain = self.read(["a.rs#1-1"])
        result = harness.run_mcp([], [harness.initialize_request(), plain])
        text = harness.tool_result_text(result.response_by_id(2))
        return re.search(r"\[a\.rs#([0-9A-F]{4})\]", text).group(1)

    def read_then_write(self, read, old, new):
        edit = {"cwd": self.cwd, "edits": [{"path": "a.rs", "tag": self.tag(), "ops": [
            {"op": "replace_text", "old": old, "new": new}]}]}
        write = harness.tools_call_request(3, "tilth_write", edit)
        result = harness.run_mcp([], [harness.initialize_request(), read, write])
        return result.response_by_id(2), result.response_by_id(3)

    def assert_section_edit_applies(self, read):
        shown, written = self.read_then_write(
            read, "let value_15 = 15;", "let value_15 = 0;")
        text = harness.tool_result_text(shown)
        self.assertRegex(text, r"\[a\.rs#[0-9A-F]{4}\]")
        self.assertIn("15:let value_15 = 15;", text, "the section shows numbered lines")
        self.assertFalse(harness.tool_is_error(written), harness.tool_result_text(written))
        self.assertEqual(self.small.read_text(), EDITED)

    def assert_edit_refused(self, read, old, new):
        _, written = self.read_then_write(read, old, new)
        self.assertTrue(harness.tool_is_error(written), harness.tool_result_text(written))
        self.assertEqual(self.small.read_text(), SMALL, "a refused edit must not write")

    def test_signature_section_lines_are_editable(self):
        self.assert_section_edit_applies(self.read(["a.rs#10-20"], "signature"))

    def test_stripped_section_lines_are_editable(self):
        self.assert_section_edit_applies(self.read(["a.rs#10-20"], "stripped"))

    def test_signature_section_lines_in_a_batch_are_editable(self):
        self.assert_section_edit_applies(self.read(["a.rs#10-20", "b.rs#1-5"], "signature"))

    def test_stripped_section_lines_in_a_batch_are_editable(self):
        self.assert_section_edit_applies(self.read(["a.rs#10-20", "b.rs#1-5"], "stripped"))

    def test_signature_section_does_not_mark_lines_outside_it(self):
        self.assert_edit_refused(
            self.read(["a.rs#10-20"], "signature"), "let value_31 = 31;", "let value_31 = 0;")

    def test_whole_file_signature_marks_nothing_seen(self):
        self.assert_edit_refused(
            self.read(["a.rs"], "signature"), "let value_15 = 15;", "let value_15 = 0;")

    def test_whole_file_signature_in_a_batch_marks_nothing_seen(self):
        self.assert_edit_refused(
            self.read(["a.rs", "b.rs"], "signature"), "let value_15 = 15;", "let value_15 = 0;")


if __name__ == "__main__":
    unittest.main()
