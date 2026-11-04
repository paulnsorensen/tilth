"""One tilth_read returns whole, unfragmented code.

A batch merges sections of one file that overlap or lie close together into
one section with one `[path#TAG]` header. A range that ends inside a function
grows to the end of the function when the response stays within budget;
otherwise it stops before the function and names the exact continuation. A
host-limit cut inside a function moves back to the line before it. Every
case marks seen exactly the lines the response shows.
"""
import json
import re
import tempfile
import unittest
from pathlib import Path

import harness


def setUpModule():
    harness.build_if_needed()


NUMBERED = re.compile(r"^(\d+):", re.M)
PAD = "x" * 80


def flat(count):
    """Plain assignments: no construct, so no range grows."""
    return "".join(f"value_{i} = {i}\n" for i in range(1, count + 1))


def function(name, body_lines):
    """A function of `body_lines + 2` lines, each body line about 100 bytes."""
    body = "".join(f'    {name}_{i} = "{PAD}"\n' for i in range(body_lines))
    return f"def {name}():\n{body}    return 0\n"


def shown(text):
    return [int(n) for n in NUMBERED.findall(text)]


def header(text):
    first = text.split("\n", 1)[0]
    return json.loads(first) if first.startswith("{") else {}


class ReadCoalesce(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cwd = self.directory.name

    def write_file(self, name, text):
        (Path(self.cwd) / name).write_text(text)

    def read(self, req_id, paths, **extra):
        return harness.tools_call_request(
            req_id, "tilth_read", {"cwd": self.cwd, "paths": paths, **extra})

    def replace(self, req_id, name, tag, lines):
        ops = [{"op": "replace", "start": n, "end": n, "content": f"edited_{n} = 0\n"}
               for n in lines]
        return harness.tools_call_request(
            req_id, "tilth_write",
            {"cwd": self.cwd, "edits": [{"path": name, "tag": tag, "ops": ops}]})

    def run_session(self, *calls):
        return harness.run_mcp([], [harness.initialize_request(), *calls])

    @staticmethod
    def tag(text, name):
        return re.search(rf"\[{re.escape(name)}#([0-9A-F]{{4}})\]", text).group(1)

    # (a) overlapping ranges in one batch merge into one section.
    def test_overlapping_batch_ranges_merge_into_one_section(self):
        self.write_file("f.py", flat(120))
        result = self.run_session(self.read(2, ["f.py#10-40", "f.py#35-80"]))
        text = harness.tool_result_text(result.response_by_id(2))
        self.assertEqual(text.count("[f.py#"), 1, text)
        self.assertEqual(shown(text), list(range(10, 81)), text)

    def test_writes_on_merged_lines_pass_the_gate(self):
        self.write_file("f.py", flat(120))
        first = self.run_session(self.read(2, ["f.py#10-40", "f.py#35-80"]))
        tag = self.tag(harness.tool_result_text(first.response_by_id(2)), "f.py")
        result = self.run_session(
            self.read(2, ["f.py#10-40", "f.py#35-80"]),
            self.replace(3, "f.py", tag, [10, 38, 80]))
        write = result.response_by_id(3)
        self.assertFalse(harness.tool_is_error(write), harness.tool_result_text(write))
        lines = (Path(self.cwd) / "f.py").read_text().splitlines()
        self.assertEqual([lines[9], lines[37], lines[79]],
                         ["edited_10 = 0", "edited_38 = 0", "edited_80 = 0"])

    def test_merged_section_marks_no_line_outside_it_seen(self):
        self.write_file("f.py", flat(120))
        first = self.run_session(self.read(2, ["f.py#10-40", "f.py#35-80"]))
        tag = self.tag(harness.tool_result_text(first.response_by_id(2)), "f.py")
        for line in (9, 81):
            result = self.run_session(
                self.read(2, ["f.py#10-40", "f.py#35-80"]),
                self.replace(3, "f.py", tag, [line]))
            write = result.response_by_id(3)
            self.assertTrue(harness.tool_is_error(write), harness.tool_result_text(write))
            self.assertIn("never displayed", harness.tool_result_text(write))
        self.assertEqual((Path(self.cwd) / "f.py").read_text(), flat(120))

    def test_adjacent_ranges_within_the_gap_merge_and_far_ones_do_not(self):
        self.write_file("f.py", flat(120))
        result = self.run_session(
            self.read(2, ["f.py#10-20", "f.py#24-30", "f.py#60-70"]))
        text = harness.tool_result_text(result.response_by_id(2))
        self.assertEqual(text.count("[f.py#"), 2, text)
        self.assertEqual(shown(text), list(range(10, 31)) + list(range(60, 71)), text)

    def test_unchanged_reread_of_a_merged_batch_is_a_stub(self):
        self.write_file("f.py", flat(120))
        result = self.run_session(
            self.read(2, ["f.py#10-40", "f.py#35-80"]),
            self.read(3, ["f.py#10-40", "f.py#35-80"]))
        text = harness.tool_result_text(result.response_by_id(3))
        self.assertIn("unchanged since your last read", text)
        self.assertEqual(shown(text), [], text)

    def test_batch_merged_to_one_section_keeps_the_batch_header(self):
        self.write_file("f.py", flat(120))
        result = self.run_session(self.read(2, ["f.py#10-40", "f.py#35-80"]))
        text = harness.tool_result_text(result.response_by_id(2))
        self.assertEqual(text.count("[f.py#"), 1, text)
        self.assertIn("if_modified_since", header(text), text[:200])

    # (b) a range that ends inside a function grows to the function's end.
    def test_range_ending_mid_function_extends_to_its_end(self):
        source = flat(4) + "\n" + function("beta", 20) + "\n" + function("gamma", 5)
        self.write_file("g.py", source)
        beta_end = 6 + 20 + 1
        result = self.run_session(self.read(2, ["g.py#1-12"]))
        text = harness.tool_result_text(result.response_by_id(2))
        self.assertEqual(shown(text), list(range(1, beta_end + 1)), text)
        self.assertNotIn("Next:", text)

    def test_extended_lines_are_writable(self):
        source = flat(4) + "\n" + function("beta", 20) + "\n" + function("gamma", 5)
        self.write_file("g.py", source)
        first = self.run_session(self.read(2, ["g.py#1-12"]))
        tag = self.tag(harness.tool_result_text(first.response_by_id(2)), "g.py")
        result = self.run_session(self.read(2, ["g.py#1-12"]),
                                  self.replace(3, "g.py", tag, [20]))
        write = result.response_by_id(3)
        self.assertFalse(harness.tool_is_error(write), harness.tool_result_text(write))
        # Restore the file so the refused write carries a valid tag and only
        # the seen-lines gate can refuse it.
        self.write_file("g.py", source)
        refused = self.run_session(self.read(2, ["g.py#1-12"]),
                                   self.replace(3, "g.py", tag, [28]))
        write = refused.response_by_id(3)
        self.assertTrue(harness.tool_is_error(write))
        self.assertIn("never displayed", harness.tool_result_text(write))

    # (c) an extension over budget stops at a construct boundary.
    def over_budget_source(self):
        fillers = "".join(function(f"fill_{k}", 17) + "\n" for k in range(10))
        return fillers + function("big", 248)

    def test_over_budget_extension_stops_before_the_construct(self):
        self.write_file("h.py", self.over_budget_source())
        big_start, big_end = 201, 450
        result = self.run_session(self.read(2, ["h.py#1-210"]))
        text = harness.tool_result_text(result.response_by_id(2))
        self.assertEqual(shown(text), list(range(1, big_start)), text[-400:])
        spec = f"h.py#{big_start}-{big_end}"
        self.assertIn(f'Next: tilth_read(paths: ["{spec}"])', text)
        self.assertEqual(header(text).get("next_view"), spec)

    def test_over_budget_continuation_is_exact_and_gated(self):
        self.write_file("h.py", self.over_budget_source())
        first = self.run_session(self.read(2, ["h.py#1-210"]))
        tag = self.tag(harness.tool_result_text(first.response_by_id(2)), "h.py")
        refused = self.run_session(self.read(2, ["h.py#1-210"]),
                                   self.replace(3, "h.py", tag, [205]))
        write = refused.response_by_id(3)
        self.assertTrue(harness.tool_is_error(write))
        self.assertIn("never displayed", harness.tool_result_text(write))
        result = self.run_session(self.read(2, ["h.py#1-210"]),
                                  self.read(3, ["h.py#201-450"]))
        text = harness.tool_result_text(result.response_by_id(3))
        self.assertEqual(shown(text), list(range(201, 451)), text[-400:])
        self.assertNotIn("Next:", text)

    # A batch names each shortened section's continuation in its header too.
    def test_batch_header_names_the_shortened_section(self):
        self.write_file("h.py", self.over_budget_source())
        self.write_file("g.py", flat(3))
        result = self.run_session(self.read(2, ["g.py#1-3", "h.py#1-210"]))
        text = harness.tool_result_text(result.response_by_id(2))
        spec = "h.py#201-450"
        self.assertIn(f'Next: tilth_read(paths: ["{spec}"])', text)
        self.assertEqual(header(text).get("next_view"), [spec], text[:200])

    def test_batch_header_drops_next_view_when_nothing_is_shortened(self):
        self.write_file("h.py", self.over_budget_source())
        self.write_file("f.py", flat(120))
        self.write_file("g.py", flat(3))
        result = self.run_session(
            self.read(2, ["g.py#1-3", "h.py#1-210"]),
            self.read(3, ["f.py#10-20", "g.py#1-3"]))
        shortened = harness.tool_result_text(result.response_by_id(2))
        self.assertEqual(header(shortened).get("next_view"), ["h.py#201-450"],
                         shortened[:200])
        text = harness.tool_result_text(result.response_by_id(3))
        self.assertNotIn("Next:", text)
        meta = header(text)
        self.assertIn("if_modified_since", meta, text[:200])
        self.assertNotIn("next_view", meta, text[:200])

    # A shortened section that shows no line (here an unchanged-reread stub)
    # adds no entry, the same as it gets no `Next:` note.
    def test_batch_header_skips_a_shortened_section_that_shows_no_line(self):
        self.write_file("h.py", self.over_budget_source())
        self.write_file("g.py", flat(3))
        result = self.run_session(
            self.read(2, ["h.py#1-210"]),
            self.read(3, ["g.py#1-3", "h.py#1-210"]))
        text = harness.tool_result_text(result.response_by_id(3))
        self.assertIn("unchanged since your last read", text)
        self.assertNotIn("Next: tilth_read", text)
        self.assertNotIn("next_view", header(text), text[:200])

    # A range larger than one response keeps its request. The host cut then
    # continues it up to the requested end, past the construct it cuts.
    def oversize_source(self):
        filler = "".join(f'filler_{i} = "{PAD}"\n' for i in range(1, 1990))
        return filler + function("mid", 19) + flat(1000)

    def test_oversize_batch_range_continues_to_its_requested_end(self):
        self.write_file("big.py", self.oversize_source())
        self.write_file("g.py", flat(3))
        result = self.run_session(
            self.read(2, ["big.py#1-2000", "g.py#1-3"], budget=200000))
        text = harness.tool_result_text(result.response_by_id(2))
        resume = shown(text)[-1] + 1
        self.assertLess(resume, 1990, text[-400:])
        self.assertIn(
            f'Next: tilth_read(paths: ["big.py#{resume}-2000","g.py#1-3"])', text)

    def test_oversize_single_range_points_next_view_at_the_cut(self):
        self.write_file("big.py", self.oversize_source())
        result = self.run_session(self.read(2, ["big.py#1-2000"], budget=200000))
        text = harness.tool_result_text(result.response_by_id(2))
        resume = shown(text)[-1] + 1
        self.assertLess(resume, 1990, text[-400:])
        self.assertIn(f'Next: tilth_read(paths: ["big.py#{resume}-2000"', text)
        self.assertNotIn("big.py#1990-2010", text, text[-400:])

    # A host-limit cut inside a function moves back to the function's start.
    def test_host_cut_moves_back_to_a_construct_start(self):
        source = "".join(function(f"part_{k}", 28) + "\n" for k in range(30))
        self.write_file("k.py", source)
        result = self.run_session(self.read(2, ["k.py"], mode="full"))
        text = harness.tool_result_text(result.response_by_id(2))
        lines = shown(text)
        self.assertEqual(lines, list(range(1, lines[-1] + 1)))
        nxt = re.search(r'Next: tilth_read\(paths: \["k\.py#(\d+)"\]', text)
        self.assertIsNotNone(nxt, text[-400:])
        resume = int(nxt.group(1))
        self.assertEqual(resume, lines[-1] + 1)
        self.assertTrue(source.splitlines()[resume - 1].startswith("def part_"),
                        source.splitlines()[resume - 1])


if __name__ == "__main__":
    unittest.main()
