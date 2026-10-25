"""A tilth_write receipt carries what the next edit of the same file needs.

One live MCP session: read -> write -> write again using only the first
receipt's tag and lines, with no second read. Also checks the fixable errors.
"""
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import harness


def setUpModule():
    harness.build_if_needed()


BODY = "".join(f"row-{i:03d}\n" for i in range(1, 101))
TAG = re.compile(r"\[f\.txt#([0-9A-F]{4})\]")


class Live:
    """A request/response MCP session over one server process."""

    def __init__(self):
        self.proc = subprocess.Popen(
            [str(harness.BIN), "--mcp"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, text=True)
        self.send(harness.initialize_request())
        self.next_id = 2

    def send_line(self, line):
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def send(self, request):
        return self.send_line(json.dumps(request))

    def call(self, name, arguments):
        self.next_id += 1
        return self.send(harness.tools_call_request(self.next_id, name, arguments))

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)
        self.proc.stdout.close()


class WriteReceipts(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cwd = self.directory.name
        self.target = Path(self.cwd) / "f.txt"
        self.target.write_text(BODY)
        self.live = Live()
        self.addCleanup(self.live.close)

    def read_tag(self):
        shown = self.live.call("tilth_read", {"cwd": self.cwd, "paths": ["f.txt#1-100"]})
        return TAG.search(harness.tool_result_text(shown)).group(1)

    def write(self, tag, ops):
        section = {"path": "f.txt", "ops": ops}
        if tag:
            section["tag"] = tag
        return self.live.call("tilth_write", {"cwd": self.cwd, "edits": [section]})

    def test_receipt_is_slim_and_authored_line_is_editable_without_a_read(self):
        tag = self.read_tag()
        first = self.write(tag, [{"op": "replace_text", "old": "row-050", "new": "WRITTEN-TEXT"}])
        receipt = harness.tool_result_text(first)
        self.assertFalse(harness.tool_is_error(first), receipt)
        self.assertIn("changed 50", receipt)
        self.assertNotIn("WRITTEN-TEXT", receipt)
        self.assertNotRegex(receipt, r"(?m)^\d+:")
        self.assertLess(len(receipt), 300, receipt)
        new_tag = TAG.search(receipt).group(1)
        second = self.write(new_tag, [{"op": "replace_text", "old": "WRITTEN-TEXT", "new": "again"}])
        self.assertFalse(harness.tool_is_error(second), harness.tool_result_text(second))
        self.assertIn("again", self.target.read_text())

    def test_unshown_neighbour_is_refused_from_the_receipt_tag(self):
        tag = self.read_tag()
        receipt = harness.tool_result_text(
            self.write(tag, [{"op": "replace_text", "old": "row-050", "new": "WRITTEN-TEXT"}]))
        rejected = self.write(TAG.search(receipt).group(1), [
            {"op": "replace_text", "old": "row-051", "new": "X"}])
        self.assertTrue(harness.tool_is_error(rejected))
        self.assertIn("never displayed", harness.tool_result_text(rejected))
        self.assertNotIn("X\n", self.target.read_text())

    def test_pure_deletion_reports_deleted_at(self):
        tag = self.read_tag()
        receipt = harness.tool_result_text(
            self.write(tag, [{"op": "delete", "start": 50, "end": 50}]))
        self.assertIn("deleted at 50", receipt)
        self.assertNotIn("changed", receipt)

    def test_missing_tag_error_states_current_tag(self):
        tag = self.read_tag()
        rejected = self.write(None, [{"op": "replace_text", "old": "row-050", "new": "X"}])
        message = harness.tool_result_text(rejected)
        self.assertIn(f'tag "{tag}"', message)
        self.assertEqual(self.target.read_text(), BODY, "a rejected edit must not write")

    def test_control_character_error_names_field_and_character(self):
        raw = (
            '{"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"tilth_write",'
            '"arguments":{"cwd":"/x","edits":[{"path":"f.txt","ops":[{"op":"replace_text",'
            '"old":"a\tb","new":"c"}]}]}}}'
        )
        message = self.live.send_line(raw)["error"]["message"]
        self.assertIn("TAB", message)
        self.assertIn('field "old"', message)


if __name__ == "__main__":
    unittest.main()
