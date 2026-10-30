"""A search hit shows its complete small construct, and those lines are editable with no read.

In a large file a usage hit used to print only its matched line. An edit that
completes the statement on that line (a closing argument, the body of a short
`if`) then needed a read first. A hit now prints the smallest bounded syntax
node around it, and the write gate accepts exactly those lines.
"""
import re
import tempfile
import unittest
from pathlib import Path

import harness


def setUpModule():
    harness.build_if_needed()


def filler(count):
    return "".join(f"    value_{i} = compute_something({i}, 'padding')\n" for i in range(count))


class SearchConstructContext(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.cwd = str(self.root)

    def search(self, name, source):
        (self.root / name).write_text(source)
        request = harness.tools_call_request(
            2, "tilth_search", {"cwd": self.cwd, "queries": [{"query": "marker_call"}]})
        found = harness.run_mcp([], [harness.initialize_request(), request])
        text = harness.tool_result_text(found.response_by_id(2))
        tag = re.search(rf"{re.escape(name)}#([0-9A-F]{{4}})", text).group(1)
        return request, text, tag

    def write(self, request, name, tag, op):
        edit = {"cwd": self.cwd, "edits": [{"path": name, "tag": tag, "ops": [op]}]}
        return harness.run_mcp([], [
            harness.initialize_request(), request,
            harness.tools_call_request(3, "tilth_write", edit),
        ]).response_by_id(3)

    def test_hit_prints_its_multi_line_statement_and_edits_it_without_a_read(self):
        source = ("def host():\n    marker_call(\n        first,\n        second,\n    )\n"
                  f"{filler(80)}    return last_line\n")
        request, text, tag = self.search("big.py", source)
        self.assertRegex(text, r"\\n4:        second,")
        written = self.write(request, "big.py", tag, {"op": "replace", "start": 4, "end": 4, "content": "        third,"})
        self.assertFalse(harness.tool_is_error(written), harness.tool_result_text(written))
        self.assertEqual((self.root / "big.py").read_text(), source.replace("second,", "third,"))

    def test_line_outside_the_shown_construct_is_still_refused(self):
        source = ("def host():\n    marker_call(\n        first,\n        second,\n    )\n"
                  f"{filler(80)}    return last_line\n")
        request, _text, tag = self.search("big.py", source)
        written = self.write(request, "big.py", tag, {"op": "replace_text", "old": "return last_line", "new": "return done"})
        self.assertTrue(harness.tool_is_error(written), harness.tool_result_text(written))
        self.assertEqual((self.root / "big.py").read_text(), source, "a refused edit must not write")

    def test_grouped_hits_print_each_statement(self):
        source = ("def host():\n    marker_call(\n        first,\n    )\n    marker_call(\n        second,\n    )\n"
                  f"{filler(80)}    return last_line\n")
        request, text, tag = self.search("group.py", source)
        self.assertRegex(text, r"\\n6:        second,")
        written = self.write(request, "group.py", tag, {"op": "replace_text", "old": "        second,", "new": "        third,"})
        self.assertFalse(harness.tool_is_error(written), harness.tool_result_text(written))

    def test_construct_over_the_bound_prints_only_the_matched_line(self):
        arguments = "".join(f"        arg_{i},\n" for i in range(40))
        source = f"def host():\n    marker_call(\n{arguments}    )\n{filler(80)}    return last_line\n"
        request, text, tag = self.search("wide.py", source)
        self.assertNotIn("arg_20", text)
        written = self.write(request, "wide.py", tag, {"op": "replace_text", "old": "arg_20,", "new": "arg_x,"})
        self.assertTrue(harness.tool_is_error(written), harness.tool_result_text(written))


if __name__ == "__main__":
    unittest.main()
