"""An unchanged re-read in one session returns a one-line stub, not the text."""
import re
import tempfile
import unittest
from pathlib import Path

import harness


def setUpModule():
    harness.build_if_needed()


SOURCE = "".join(f"let value_{i} = {i};\n" for i in range(30))
STUB = re.compile(r"^\[a\.rs#[0-9A-F]{4}\] unchanged since your last read \(mode: \"full\"")


class UnchangedReread(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cwd = self.directory.name
        self.file = Path(self.cwd) / "a.rs"
        self.file.write_text(SOURCE)

    def reads(self, *calls):
        requests = [harness.initialize_request()]
        for i, args in enumerate(calls, start=2):
            requests.append(harness.tools_call_request(
                i, "tilth_read", {"cwd": self.cwd, **args}))
        result = harness.run_mcp([], requests)
        return [harness.tool_result_text(result.response_by_id(i))
                for i in range(2, 2 + len(calls))]

    def test_second_whole_read_is_a_stub(self):
        first, second = self.reads({"paths": ["a.rs"]}, {"paths": ["a.rs"]})
        self.assertIn("value_29", first)
        self.assertRegex(second, STUB)
        self.assertNotIn("value_29", second)

    def test_second_section_read_is_a_stub(self):
        first, second = self.reads({"paths": ["a.rs#2-5"]}, {"paths": ["a.rs#3-4"]})
        self.assertIn("value_2", first)
        self.assertRegex(second, STUB)

    def test_unseen_lines_are_still_sent(self):
        _, second = self.reads({"paths": ["a.rs#2-5"]}, {"paths": ["a.rs#2-9"]})
        self.assertIn("value_8", second)

    def test_mode_full_resends_the_text(self):
        _, second = self.reads(
            {"paths": ["a.rs"]}, {"paths": ["a.rs"], "mode": "full"})
        self.assertIn("value_29", second)

    def test_batched_reread_is_a_stub(self):
        first, second = self.reads({"paths": ["a.rs"]}, {"paths": ["a.rs", "a.rs#2-3"]})
        self.assertIn("value_29", first)
        self.assertNotIn("value_29", second)
        self.assertIn("unchanged since your last read", second)

    def test_truncated_read_is_not_a_stub_later(self):
        _, second = self.reads(
            {"paths": ["a.rs#1-30"], "budget": 1}, {"paths": ["a.rs#1-30"]})
        self.assertIn("value_29", second)
        self.assertNotIn("unchanged since", second)

    def test_changed_file_sends_the_text(self):
        requests = [
            harness.initialize_request(),
            harness.tools_call_request(2, "tilth_read", {"cwd": self.cwd, "paths": ["a.rs"]}),
        ]
        first = harness.tool_result_text(harness.run_mcp([], requests).response_by_id(2))
        tag = re.search(r"#([0-9A-F]{4})\]", first).group(1)
        edit = {"cwd": self.cwd, "edits": [{"path": "a.rs", "tag": tag, "ops": [
            {"op": "replace_text", "old": "value_3 = 3", "new": "value_3 = 33"}]}]}
        requests.append(harness.tools_call_request(3, "tilth_write", edit))
        requests.append(harness.tools_call_request(
            4, "tilth_read", {"cwd": self.cwd, "paths": ["a.rs"]}))
        third = harness.tool_result_text(harness.run_mcp([], requests).response_by_id(4))
        self.assertIn("value_3 = 33", third)
        self.assertNotIn("unchanged since", third)


if __name__ == "__main__":
    unittest.main()
