"""A search hit is edit-ready: its tag edits any line the search printed.

A small file prints whole, so any line of it is editable straight from the
search tag. A large file prints only the matched line, so an edit on an
unprinted line is rejected.
"""
import re
import tempfile
import unittest
from pathlib import Path

import harness


def setUpModule():
    harness.build_if_needed()


def filler(count):
    return "".join(f"value_{i} = compute_something({i}, 'padding')\n" for i in range(count))


class EditReadySearch(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.cwd = str(self.root)

    def search_then_write(self, name, source, op):
        target = self.root / name
        target.write_text(source)
        search = harness.tools_call_request(
            2, "tilth_search", {"cwd": self.cwd, "queries": [{"query": "marker_call"}]})
        found = harness.run_mcp([], [harness.initialize_request(), search])
        text = harness.tool_result_text(found.response_by_id(2))
        tag = re.search(rf"{re.escape(name)}#([0-9A-F]{{4}})", text).group(1)
        edit = {"cwd": self.cwd, "edits": [{"path": name, "tag": tag, "ops": [op]}]}
        written = harness.run_mcp([], [
            harness.initialize_request(), search,
            harness.tools_call_request(3, "tilth_write", edit),
        ]).response_by_id(3)
        return target, written

    def test_small_file_hit_edits_any_line_without_a_read(self):
        source = f"def host():\n    marker_call(1)\n{filler(20)}    return last_line\n"
        target, written = self.search_then_write(
            "small.py", source,
            {"op": "replace_text", "old": "return last_line", "new": "return done"})
        self.assertFalse(harness.tool_is_error(written), harness.tool_result_text(written))
        self.assertEqual(target.read_text(), source.replace("last_line", "done"))

    def test_large_file_edit_on_an_unprinted_line_is_rejected(self):
        source = f"def host():\n    marker_call(1)\n{filler(80)}    return last_line\n"
        target, written = self.search_then_write(
            "large.py", source,
            {"op": "replace_text", "old": "return last_line", "new": "return done"})
        self.assertTrue(harness.tool_is_error(written), harness.tool_result_text(written))
        self.assertEqual(target.read_text(), source, "a rejected edit must not write")

    def test_press_exactly_sixty_line_crlf_file_edits_last_line_and_keeps_crlf(self):
        lines = ["def host():", "    marker_call(1)"] + [f"    v{i} = {i}" for i in range(57)] + ["    return last_line"]
        self.assertEqual(len(lines), 60)
        source = "\r\n".join(lines) + "\r\n"
        target = self.root / "crlf.py"
        target.write_bytes(source.encode())
        search = harness.tools_call_request(
            2, "tilth_search", {"cwd": self.cwd, "queries": [{"query": "marker_call"}]})
        text = harness.tool_result_text(harness.run_mcp([], [harness.initialize_request(), search]).response_by_id(2))
        tag = re.search(r"crlf\.py#([0-9A-F]{4})", text).group(1)
        edit = {"cwd": self.cwd, "edits": [{"path": "crlf.py", "tag": tag, "ops": [
            {"op": "replace", "start": 60, "end": 60, "content": "    return done"}]}]}
        written = harness.run_mcp([], [
            harness.initialize_request(), search,
            harness.tools_call_request(3, "tilth_write", edit),
        ]).response_by_id(3)
        self.assertFalse(harness.tool_is_error(written), harness.tool_result_text(written))
        expected = source.replace("    return last_line", "    return done").encode()
        self.assertEqual(target.read_bytes(), expected)

    def test_press_top_level_glob_is_rejected_with_a_hint_and_other_keys_stay_generic(self):
        glob = harness.tools_call_request(
            2, "tilth_search", {"cwd": self.cwd, "glob": "*.py", "queries": [{"query": "x"}]})
        other = harness.tools_call_request(
            3, "tilth_search", {"cwd": self.cwd, "scope": "s", "queries": [{"query": "x"}]})
        run = harness.run_mcp([], [harness.initialize_request(), glob, other])
        first = run.response_by_id(2)
        second = run.response_by_id(3)
        self.assertTrue(harness.tool_is_error(first))
        self.assertIn("glob goes inside each query entry", harness.tool_result_text(first))
        self.assertTrue(harness.tool_is_error(second))
        self.assertNotIn("glob", harness.tool_result_text(second))


if __name__ == "__main__":
    unittest.main()
