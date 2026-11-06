"""Two approved relaxations of the tilth_write seen-lines gate (2026-11-04).

1. A tagless `replace_text` takes the live tag when this session displayed the
   live content.
2. A tagged `replace_text` whose `old` matches exactly once applies on a line
   that no read displayed.

`all`/`count` on an unread line is refused; that refusal shows the match (see
test_unseen_match_refusal.py). An ambiguous `old` and a whitespace-normalized
match on an unread line stay rejected.
"""
import re
import tempfile
import unittest
from pathlib import Path

import harness


def setUpModule():
    harness.build_if_needed()


BODY = "".join(f"row-{i:03d}\n" for i in range(1, 101))
TAG = re.compile(r"\[f\.txt#([0-9A-F]{4})\]")


class WriteTagFillExactMatch(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cwd = self.directory.name
        self.target = Path(self.cwd) / "f.txt"

    def read(self, spec):
        return harness.tools_call_request(2, "tilth_read", {"cwd": self.cwd, "paths": [spec]})

    def tag_after(self, read):
        first = harness.run_mcp([], [harness.initialize_request(), read])
        return TAG.search(harness.tool_result_text(first.response_by_id(2))).group(1)

    def write_after(self, requests, section):
        result = harness.run_mcp([], [
            harness.initialize_request(), *requests,
            harness.tools_call_request(3, "tilth_write", {"cwd": self.cwd, "edits": [section]}),
        ])
        return result.response_by_id(3)

    def assert_rejected(self, written, source):
        self.assertTrue(harness.tool_is_error(written), harness.tool_result_text(written))
        self.assertEqual(self.target.read_text(), source, "a rejected edit must not write")

    def test_tagless_replace_text_on_a_displayed_line_applies(self):
        self.target.write_text(BODY)
        written = self.write_after([self.read("f.txt#10-20")], {
            "path": "f.txt",
            "ops": [{"op": "replace_text", "old": "row-015", "new": "ROW-015"}]})
        receipt = harness.tool_result_text(written)
        self.assertFalse(harness.tool_is_error(written), receipt)
        self.assertIn("changed 15", receipt)
        self.assertRegex(receipt, TAG)
        self.assertEqual(self.target.read_text(), BODY.replace("row-015", "ROW-015"))

    def test_tagless_replace_text_without_a_read_stays_rejected(self):
        self.target.write_text(BODY)
        written = self.write_after([], {
            "path": "f.txt",
            "ops": [{"op": "replace_text", "old": "row-015", "new": "ROW-015"}]})
        self.assert_rejected(written, BODY)
        self.assertIn("requires the [path#TAG]", harness.tool_result_text(written))

    def test_unique_replace_text_on_an_unread_line_applies(self):
        self.target.write_text(BODY)
        read = self.read("f.txt#1-5")
        tag = self.tag_after(read)
        written = self.write_after([read], {
            "path": "f.txt", "tag": tag,
            "ops": [{"op": "replace_text", "old": "row-040", "new": "ROW-040"}]})
        receipt = harness.tool_result_text(written)
        self.assertFalse(harness.tool_is_error(written), receipt)
        self.assertIn("changed 40", receipt)
        self.assertNotEqual(TAG.search(receipt).group(1), tag, "new content, new tag")
        self.assertEqual(self.target.read_text(), BODY.replace("row-040", "ROW-040"))

    def test_all_or_count_on_an_unread_occurrence_is_rejected(self):
        for extra in ({"all": True}, {"count": 1}):
            with self.subTest(extra=extra):
                # Distinct content per case: a refusal records the lines it shows.
                source = BODY + f"end-{next(iter(extra))}\n"
                self.target.write_text(source)
                read = self.read("f.txt#1-5")
                op = {"op": "replace_text", "old": "row-040", "new": "ROW-040", **extra}
                written = self.write_after(
                    [read], {"path": "f.txt", "tag": self.tag_after(read), "ops": [op]})
                self.assert_rejected(written, source)
                self.assertIn("never displayed", harness.tool_result_text(written))

    def test_ambiguous_old_stays_rejected(self):
        source = BODY + "row-040\n"
        self.target.write_text(source)
        read = self.read("f.txt#1-5")
        written = self.write_after([read], {
            "path": "f.txt", "tag": self.tag_after(read),
            "ops": [{"op": "replace_text", "old": "row-040", "new": "ROW-040"}]})
        self.assert_rejected(written, source)

    def test_whitespace_normalized_match_on_an_unread_line_stays_rejected(self):
        source = BODY.replace("row-040", "row  040")
        self.target.write_text(source)
        read = self.read("f.txt#1-5")
        written = self.write_after([read], {
            "path": "f.txt", "tag": self.tag_after(read),
            "ops": [{"op": "replace_text", "old": "row 040", "new": "ROW-040"}]})
        self.assert_rejected(written, source)
        self.assertIn("never displayed", harness.tool_result_text(written))


if __name__ == "__main__":
    unittest.main()
