"""An unchanged re-read in one session returns a one-line stub, not the text."""
import json
import re
import tempfile
import unittest
from pathlib import Path

import harness


def setUpModule():
    harness.build_if_needed()


SOURCE = "".join(f"let value_{i} = {i};\n" for i in range(30))


def stub(target):
    return re.compile(
        r"^\[a\.rs#[0-9A-F]{4}\] unchanged since your last read\. "
        r"To re-send: tilth_read\(paths: \[\"" + re.escape(target) + r"\"\], mode: \"full\"\)(\n|$)")


STUB = stub("a.rs")
SECTION_STUB = stub("a.rs#3-4")


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

    def test_section_stub_names_the_section_call(self):
        _, second = self.reads({"paths": ["a.rs#2-5"]}, {"paths": ["a.rs#3-4"]})
        self.assertRegex(second, SECTION_STUB)

    def test_named_call_resends_only_the_section(self):
        _, _, third = self.reads(
            {"paths": ["a.rs#2-5"]},
            {"paths": ["a.rs#3-4"]},
            {"paths": ["a.rs#3-4"], "mode": "full"})
        self.assertIn("value_3 = 3", third)
        self.assertNotIn("value_29", third)
        self.assertNotIn("unchanged since", third)

    def test_second_section_read_is_a_stub(self):
        first, second = self.reads({"paths": ["a.rs#2-5"]}, {"paths": ["a.rs#3-4"]})
        self.assertIn("value_2", first)
        self.assertRegex(second, SECTION_STUB)

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
        responses = harness.run_mcp([], requests)
        third = harness.tool_result_text(responses.response_by_id(4))
        self.assertIn("value_3 = 33", third)
        self.assertNotIn("unchanged since", third)

    def test_oversized_read_is_not_a_stub_later(self):
        big = Path(self.cwd) / "big.rs"
        big.write_text("".join(f"let value_{i} = {i};\n" for i in range(2500)))
        first, second = self.reads(
            {"paths": ["big.rs"], "mode": "full", "budget": 100000},
            {"paths": ["big.rs#2400-2410"]})
        self.assertLessEqual(len(json.dumps(first)) - 2, 40000)
        self.assertRegex(first, r'Next: tilth_read\(paths: \["big\.rs#\d+"\]\)')
        self.assertIn("value_2400 = 2400", second)
        self.assertNotIn("unchanged since", second)

    def test_lines_shown_before_the_cut_stay_a_stub(self):
        big = Path(self.cwd) / "big.rs"
        big.write_text("".join(f"let value_{i} = {i};\n" for i in range(2500)))
        _, second = self.reads(
            {"paths": ["big.rs"], "mode": "full", "budget": 100000},
            {"paths": ["big.rs#10-20"]})
        self.assertIn("unchanged since", second)


class ClippedSearchThenRead(unittest.TestCase):
    """A search that the budget clipped must not make a later read a stub."""

    NAMES = [f"f{i:02d}" for i in range(30)]

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cwd = self.directory.name
        body = "".join(f'let value_{i} = "{"x" * 300}";\n' for i in range(70))
        for name in self.NAMES:
            (Path(self.cwd) / f"{name}.rs").write_text(body)

    def run_calls(self, calls):
        requests = [harness.initialize_request()]
        for i, (tool, args) in enumerate(calls, start=2):
            requests.append(harness.tools_call_request(
                i, tool, {"cwd": self.cwd, **args}))
        result = harness.run_mcp([], requests)
        return [harness.tool_result_text(result.response_by_id(i))
                for i in range(2, 2 + len(calls))]

    def test_only_lines_the_clipped_search_showed_become_stubs(self):
        search = ("tilth_search", {"queries": [{"query": "value_3"}], "budget": 380})
        reads = [("tilth_read", {"paths": [f"{name}.rs#4-4"]}) for name in self.NAMES]
        first, *texts = self.run_calls([search, *reads])
        preview = json.loads(first)["results"][0]["preview"]
        shown = {name for name in self.NAMES
                 if re.search(rf"### {name}\.rs#[0-9A-F]{{4}}:4 \[usage\]\n-> \[4\]", preview)}
        stubbed = {name for name, text in zip(self.NAMES, texts)
                   if text.startswith(f"[{name}.rs#") and "unchanged since" in text[:120]}
        self.assertTrue(shown, "the clipped search should still show some matches")
        self.assertLess(len(shown), 10, "the budget should clip some of the 10 matches")
        self.assertEqual(stubbed, shown)

if __name__ == "__main__":
    unittest.main()
