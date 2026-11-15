"""Four `tilth_write` failure round trips, checked through `tilth --mcp`.

1. Matches of an `all:true` op nested in another op's span are dropped.
2. A refusal that shows unseen lines holds the edit; an empty-ops section
   with the tag confirms it.
3. A batch takes up to 50 sections.
4. A file that this session created is its own: `create_file` overwrites it
   until something else changes it, and its lines count as displayed.
"""
import json
import re
import tempfile
import unittest
from pathlib import Path

import harness
from test_unseen_match_refusal import CALL_LINES, REWRITE, REWRITTEN, SOURCE


def setUpModule():
    harness.build_if_needed()


TAG = re.compile(r"\[([^\]#\s]+)#([0-9A-F]{4})\]")


class WriteCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cwd = str(self.root)
        self.server = harness.Server(cwd=self.cwd)
        self.addCleanup(self.server.close)

    def read_tag(self, name, section=None):
        target = name if section is None else f"{name}#{section}"
        response = self.server.call("tilth_read", {"cwd": self.cwd, "paths": [target]})
        text = harness.tool_result_text(response)
        self.assertFalse(harness.tool_is_error(response), text)
        return TAG.search(text).group(2)

    def write(self, *sections):
        return self.server.call("tilth_write", {"cwd": self.cwd, "edits": list(sections)})

    def edit(self, name, tag, *ops):
        return self.write({"path": name, "tag": tag, "ops": list(ops)})

    def text(self, name):
        return (self.root / name).read_text()

    def applied(self, response):
        text = harness.tool_result_text(response)
        self.assertFalse(harness.tool_is_error(response), text)
        return text

    def refused(self, response):
        text = harness.tool_result_text(response)
        self.assertTrue(harness.tool_is_error(response), text)
        return text


class NestedMatches(WriteCase):
    def run_nested(self, source, ops, expected):
        (self.root / "f.rs").write_text(source)
        tag = self.read_tag("f.rs")
        self.applied(self.edit("f.rs", tag, *ops))
        self.assertEqual(self.text("f.rs"), expected)

    def test_an_all_match_inside_a_longer_all_match_is_dropped(self):
        source = (
            "fn a() -> Result<bool, io::Error> {\n    todo!()\n}\n"
            "fn b(f: impl FnMut(u8) -> Result<bool, io::Error>) {}\n"
        )
        short = {"op": "replace_text", "old": "Result<bool, io::Error>", "new": "io::Result<bool>", "all": True}
        long = {
            "op": "replace_text",
            "old": "FnMut(u8) -> Result<bool, io::Error>",
            "new": "FnMut(u8) -> Outcome",
            "all": True,
        }
        self.run_nested(
            source,
            [short, long],
            "fn a() -> io::Result<bool> {\n    todo!()\n}\nfn b(f: impl FnMut(u8) -> Outcome) {}\n",
        )

    def test_an_all_match_inside_a_specific_multi_line_replace_is_dropped(self):
        source = "fn f(x: bool) -> Result<bool, E> {\n    if x {\n        Ok(true)\n    } else {\n        Err(E)\n    }\n}\nfn g() -> Result<bool, E> {\n    Ok(true)\n}\n"
        every = {"op": "replace_text", "old": "Ok(true)", "new": "Ok(false)", "all": True}
        specific = {
            "op": "replace_text",
            "old": "    if x {\n        Ok(true)\n    } else {\n",
            "new": "    if !x {\n        Ok(true)\n    } else {\n",
        }
        self.run_nested(
            source,
            [every, specific],
            "fn f(x: bool) -> Result<bool, E> {\n    if !x {\n        Ok(true)\n    } else {\n        Err(E)\n    }\n}\nfn g() -> Result<bool, E> {\n    Ok(false)\n}\n",
        )

    def test_a_line_all_match_inside_a_specific_replace_is_dropped(self):
        source = (
            "class A:\n    def __init__(self, auto_error):\n        self.auto_error = auto_error\n        self.x = 1\n"
            "class B:\n    def __init__(self, auto_error):\n        self.auto_error = auto_error\n"
        )
        every = {"op": "replace_text", "old": "        self.auto_error = auto_error\n", "new": "        self.auto_error = bool(auto_error)\n", "all": True}
        specific = {
            "op": "replace_text",
            "old": "        self.auto_error = auto_error\n        self.x = 1\n",
            "new": "        self.auto_error = None\n        self.x = 2\n",
        }
        self.run_nested(
            source,
            [every, specific],
            "class A:\n    def __init__(self, auto_error):\n        self.auto_error = None\n        self.x = 2\n"
            "class B:\n    def __init__(self, auto_error):\n        self.auto_error = bool(auto_error)\n",
        )

    def test_a_partial_overlap_is_still_rejected(self):
        source = "foo bar baz\n"
        (self.root / "f.rs").write_text(source)
        tag = self.read_tag("f.rs")
        first = {"op": "replace_text", "old": "foo bar", "new": "X", "all": True}
        second = {"op": "replace_text", "old": "bar baz", "new": "Y", "all": True}
        self.refused(self.edit("f.rs", tag, first, second))
        self.assertEqual(self.text("f.rs"), source)

    def test_a_single_match_swap_inside_another_op_is_still_rejected(self):
        source = "let a = aaa XYZ bbb;\n"
        (self.root / "f.rs").write_text(source)
        tag = self.read_tag("f.rs")
        outer = {"op": "replace_text", "old": "aaa XYZ", "new": "Q", "all": True}
        inner = {"op": "replace_text", "old": "XYZ", "new": "R"}
        self.refused(self.edit("f.rs", tag, outer, inner))
        self.assertEqual(self.text("f.rs"), source)


class ConfirmHeldEdit(WriteCase):
    def setUp(self):
        super().setUp()
        (self.root / "f.rs").write_text(SOURCE)
        self.tag = self.read_tag("f.rs", "1-5")

    def confirm(self, tag=None):
        return self.edit("f.rs", tag or self.tag)

    def test_a_multi_match_refusal_is_confirmed_with_empty_ops(self):
        refusal = self.refused(self.edit("f.rs", self.tag, REWRITE))
        self.assertEqual(self.text("f.rs"), SOURCE)
        confirm = json.dumps({"path": "f.rs", "tag": self.tag, "ops": []}, separators=(",", ":"))
        self.assertIn(confirm, refusal)
        self.applied(self.confirm())
        self.assertEqual(self.text("f.rs"), REWRITTEN)

    def test_an_unchanged_resend_still_applies(self):
        self.refused(self.edit("f.rs", self.tag, REWRITE))
        self.applied(self.edit("f.rs", self.tag, REWRITE))
        self.assertEqual(self.text("f.rs"), REWRITTEN)

    def test_a_single_anchor_refusal_is_confirmed_with_empty_ops(self):
        line = CALL_LINES[5]
        op = {"op": "replace", "start": line, "end": line, "content": "    bar(x) + y"}
        refusal = self.refused(self.edit("f.rs", self.tag, op))
        self.assertEqual(self.text("f.rs"), SOURCE)
        self.assertIn(f"{line}:    foo(x) + y", refusal)
        self.assertIn('"ops":[]', refusal)
        self.applied(self.confirm())
        rows = SOURCE.split("\n")
        rows[line - 1] = "    bar(x) + y"
        self.assertEqual(self.text("f.rs"), "\n".join(rows))

    def test_a_confirm_after_an_external_change_writes_nothing(self):
        self.refused(self.edit("f.rs", self.tag, REWRITE))
        drifted = "// drift\n" + SOURCE
        (self.root / "f.rs").write_text(drifted)
        self.refused(self.confirm())
        self.assertEqual(self.text("f.rs"), drifted)

    def test_a_confirm_with_nothing_held_writes_nothing(self):
        self.refused(self.confirm())
        self.assertEqual(self.text("f.rs"), SOURCE)

    def test_a_held_edit_works_once(self):
        noop = {"op": "rewrite", "pattern": "foo($A)", "rewrite": "foo($A)"}
        self.refused(self.edit("f.rs", self.tag, noop))
        self.applied(self.confirm())
        self.assertEqual(self.text("f.rs"), SOURCE)
        self.refused(self.confirm())

    def test_a_second_confirm_after_the_edit_landed_fails(self):
        self.refused(self.edit("f.rs", self.tag, REWRITE))
        self.applied(self.confirm())
        self.refused(self.confirm())
        self.assertEqual(self.text("f.rs"), REWRITTEN)


class BatchCap(WriteCase):
    def sections(self, count):
        return [
            {"path": f"g{i}.txt", "ops": [{"op": "create_file", "content": f"{i}\n"}]}
            for i in range(count)
        ]

    def test_fifty_sections_apply(self):
        self.applied(self.write(*self.sections(50)))
        self.assertEqual(self.text("g49.txt"), "49\n")

    def test_fifty_one_sections_are_rejected(self):
        refusal = self.refused(self.write(*self.sections(51)))
        self.assertIn("50 sections", refusal)
        self.assertFalse((self.root / "g0.txt").exists())


class OwnFileEdits(WriteCase):
    def create(self, content):
        return self.write({"path": "n.txt", "ops": [{"op": "create_file", "content": content}]})

    def test_create_over_its_own_file_applies_and_the_new_lines_are_editable(self):
        self.applied(self.create("one\ntwo\n"))
        receipt = self.applied(self.create("alpha\nbeta\ngamma\n"))
        self.assertEqual(self.text("n.txt"), "alpha\nbeta\ngamma\n")
        tag = TAG.search(receipt).group(2)
        op = {"op": "replace", "start": 2, "end": 2, "content": "BETA"}
        self.applied(self.edit("n.txt", tag, op))
        self.assertEqual(self.text("n.txt"), "alpha\nBETA\ngamma\n")

    def test_create_over_its_own_file_after_an_edit_of_it_applies(self):
        receipt = self.applied(self.create("alpha\nbeta\n"))
        tag = TAG.search(receipt).group(2)
        op = {"op": "replace", "start": 1, "end": 1, "content": "ALPHA"}
        self.applied(self.edit("n.txt", tag, op))
        self.applied(self.create("fresh\n"))
        self.assertEqual(self.text("n.txt"), "fresh\n")

    def test_create_over_a_pre_existing_file_is_refused(self):
        (self.root / "n.txt").write_text("mine\n")
        self.refused(self.create("theirs\n"))
        self.assertEqual(self.text("n.txt"), "mine\n")

    def test_create_over_a_file_changed_outside_tilth_is_refused(self):
        self.applied(self.create("one\n"))
        (self.root / "n.txt").write_text("outside\n")
        self.refused(self.create("two\n"))
        self.assertEqual(self.text("n.txt"), "outside\n")

    def test_create_over_a_pre_existing_file_a_second_time_is_refused(self):
        # A refused create must not make the file count as created here.
        (self.root / "n.txt").write_text("mine\n")
        self.refused(self.create("a\n"))
        self.refused(self.create("b\n"))
        self.assertEqual(self.text("n.txt"), "mine\n")


if __name__ == "__main__":
    unittest.main()
