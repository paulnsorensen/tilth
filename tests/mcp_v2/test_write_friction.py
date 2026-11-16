"""Four `tilth_write` failure round trips, checked through `tilth --mcp`.

1. Matches of an `all:true` op nested in another op's span are dropped.
2. A refusal that shows unseen lines holds the edit under a hold token
   (`H` plus 7 hex digits); an empty-ops section that sends the token as its
   `tag` confirms that edit, and only if the file still has the tag it was
   refused under.
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


HOLD = re.compile(r"holds your edit as (H[0-9A-F]{7})")
SINGLE = {"op": "replace", "start": CALL_LINES[5], "end": CALL_LINES[5], "content": "    bar(x) + y"}


class ConfirmHeldEdit(WriteCase):
    def setUp(self):
        super().setUp()
        (self.root / "f.rs").write_text(SOURCE)
        self.tag = self.read_tag("f.rs", "1-5")

    def hold(self, refusal):
        return HOLD.search(refusal).group(1)

    def line_op(self, line, content):
        return {"op": "replace", "start": line, "end": line, "content": content}

    def test_a_multi_match_refusal_is_confirmed_with_its_hold_token(self):
        refusal = self.refused(self.edit("f.rs", self.tag, REWRITE))
        self.assertEqual(self.text("f.rs"), SOURCE)
        token = self.hold(refusal)
        confirm = json.dumps({"path": "f.rs", "tag": token, "ops": []}, separators=(",", ":"))
        self.assertIn(confirm, refusal)
        self.applied(self.edit("f.rs", token))
        self.assertEqual(self.text("f.rs"), REWRITTEN)

    def test_the_file_tag_with_empty_ops_applies_nothing(self):
        self.refused(self.edit("f.rs", self.tag, REWRITE))
        reply = self.applied(self.edit("f.rs", self.tag))
        self.assertIn("no change", reply)
        self.assertEqual(self.text("f.rs"), SOURCE)

    def test_an_unchanged_resend_still_applies(self):
        self.refused(self.edit("f.rs", self.tag, REWRITE))
        self.applied(self.edit("f.rs", self.tag, REWRITE))
        self.assertEqual(self.text("f.rs"), REWRITTEN)

    def test_a_single_anchor_refusal_is_confirmed_with_its_hold_token(self):
        line = CALL_LINES[5]
        refusal = self.refused(self.edit("f.rs", self.tag, SINGLE))
        self.assertEqual(self.text("f.rs"), SOURCE)
        self.assertIn(f"{line}:    foo(x) + y", refusal)
        self.assertIn('"ops":[]', refusal)
        self.applied(self.edit("f.rs", self.hold(refusal)))
        rows = SOURCE.split("\n")
        rows[line - 1] = "    bar(x) + y"
        self.assertEqual(self.text("f.rs"), "\n".join(rows))

    def test_two_refused_edits_on_one_tag_each_confirm_their_own_section(self):
        first, second = CALL_LINES[3], CALL_LINES[8]
        one = self.hold(self.refused(self.edit("f.rs", self.tag, self.line_op(first, "    one(x) + y"))))
        two = self.hold(self.refused(self.edit("f.rs", self.tag, self.line_op(second, "    two(x) + y"))))
        self.assertNotEqual(one, two)
        self.applied(self.edit("f.rs", two))
        rows = SOURCE.split("\n")
        rows[second - 1] = "    two(x) + y"
        landed = "\n".join(rows)
        self.assertEqual(self.text("f.rs"), landed)
        stale = self.refused(self.edit("f.rs", one))
        self.assertIn("changed", stale)
        self.assertEqual(self.text("f.rs"), landed)

    def test_the_older_of_two_holds_confirms_its_own_section(self):
        first, second = CALL_LINES[3], CALL_LINES[8]
        one = self.hold(self.refused(self.edit("f.rs", self.tag, self.line_op(first, "    one(x) + y"))))
        two = self.hold(self.refused(self.edit("f.rs", self.tag, self.line_op(second, "    two(x) + y"))))
        self.applied(self.edit("f.rs", one))
        rows = SOURCE.split("\n")
        rows[first - 1] = "    one(x) + y"
        landed = "\n".join(rows)
        self.assertEqual(self.text("f.rs"), landed)
        self.assertIn("changed", self.refused(self.edit("f.rs", two)))
        self.assertEqual(self.text("f.rs"), landed)

    def test_a_hold_token_with_another_path_is_refused_and_stays_valid(self):
        (self.root / "g.rs").write_text(SOURCE)
        self.read_tag("g.rs", "1-5")
        token = self.hold(self.refused(self.edit("f.rs", self.tag, SINGLE)))
        wrong = self.refused(self.edit("g.rs", token))
        self.assertIn("held edit expired", wrong)
        self.assertEqual(self.text("g.rs"), SOURCE)
        self.assertEqual(self.text("f.rs"), SOURCE)
        self.applied(self.edit("f.rs", token))
        self.assertNotEqual(self.text("f.rs"), SOURCE)

    def test_a_confirm_after_an_external_change_writes_nothing(self):
        token = self.hold(self.refused(self.edit("f.rs", self.tag, REWRITE)))
        drifted = "// drift\n" + SOURCE
        (self.root / "f.rs").write_text(drifted)
        self.refused(self.edit("f.rs", token))
        self.assertEqual(self.text("f.rs"), drifted)

    def test_a_confirm_with_nothing_held_writes_nothing(self):
        refusal = self.refused(self.edit("f.rs", "H0000000"))
        self.assertIn("held edit expired", refusal)
        self.assertEqual(self.text("f.rs"), SOURCE)

    def test_a_held_edit_works_once(self):
        noop = {"op": "rewrite", "pattern": "foo($A)", "rewrite": "foo($A)"}
        token = self.hold(self.refused(self.edit("f.rs", self.tag, noop)))
        self.applied(self.edit("f.rs", token))
        self.assertEqual(self.text("f.rs"), SOURCE)
        # A no-op leaves the file tag as it was, so only a consumed hold
        # explains this refusal.
        self.assertIn("held edit expired", self.refused(self.edit("f.rs", token)))

    def test_a_second_confirm_after_the_edit_landed_fails(self):
        token = self.hold(self.refused(self.edit("f.rs", self.tag, REWRITE)))
        self.applied(self.edit("f.rs", token))
        self.assertIn("held edit expired", self.refused(self.edit("f.rs", token)))
        self.assertEqual(self.text("f.rs"), REWRITTEN)

    def refuse_files(self, count):
        """One refused edit on each of `count` files; the hold tokens in order."""
        names = [f"m{i}.rs" for i in range(count)]
        sections = []
        for name in names:
            (self.root / name).write_text(SOURCE)
            sections.append({"path": name, "tag": self.read_tag(name, "1-5"), "ops": [SINGLE]})
        tokens = []
        for at in range(0, count, 20):
            text = harness.tool_result_text(self.write(*sections[at : at + 20]))
            tokens += HOLD.findall(text)
        self.assertEqual(len(tokens), count)
        return names, tokens

    def test_more_than_sixteen_refusals_in_one_batch_all_confirm(self):
        names, tokens = self.refuse_files(20)
        sections = [{"path": n, "tag": t, "ops": []} for n, t in zip(names, tokens)]
        self.applied(self.write(*sections))
        for name in names:
            self.assertEqual(self.text(name).split("\n")[SINGLE["start"] - 1], "    bar(x) + y")

    def test_the_oldest_hold_expires_once_the_bound_is_passed(self):
        names, tokens = self.refuse_files(65)
        gone = self.refused(self.write({"path": names[0], "tag": tokens[0], "ops": []}))
        self.assertIn("held edit expired; resend the section", gone)
        self.assertEqual(self.text(names[0]), SOURCE)
        self.applied(self.write({"path": names[-1], "tag": tokens[-1], "ops": []}))

    def test_a_refusal_on_the_last_line_shows_only_lines_of_the_file(self):
        last = len(SOURCE.splitlines())
        refusal = self.refused(self.edit("f.rs", self.tag, self.line_op(last, "}")))
        shown = [int(n) for n in re.findall(r"^(\d+):", refusal, re.MULTILINE)]
        self.assertIn(last, shown)
        self.assertEqual(max(shown), last, refusal)
        self.applied(self.edit("f.rs", self.hold(refusal)))
        past = self.refused(self.edit("f.rs", self.read_tag("f.rs", "1-5"), {"op": "insert_before", "line": last + 1, "content": "x"}))
        self.assertIsNone(HOLD.search(past), past)
        self.assertEqual([n for n in re.findall(r"(?m)^(\d+):", past) if int(n) > last], [], past)

    def test_a_refusal_on_a_crlf_file_shows_rows_without_the_carriage_return(self):
        crlf = SOURCE.replace("\n", "\r\n")
        (self.root / "c.rs").write_bytes(crlf.encode())
        tag = self.read_tag("c.rs", "1-5")
        line = CALL_LINES[5]
        refusal = self.refused(self.edit("c.rs", tag, self.line_op(line, "    bar(x) + y")))
        self.assertNotIn("\r", refusal)
        self.assertIn(f"{line}:    foo(x) + y\n", refusal + "\n")
        self.applied(self.edit("c.rs", self.hold(refusal)))
        rows = (self.root / "c.rs").read_bytes().split(b"\n")
        self.assertEqual(rows[line - 1].rstrip(b"\r"), b"    bar(x) + y")
        self.assertTrue(all(row.endswith(b"\r") for row in rows[:5]))

    def test_anchors_that_need_over_400_lines_shown_are_not_held(self):
        (self.root / "big.txt").write_text("".join(f"row {i}\n" for i in range(1000)))
        tag = self.read_tag("big.txt", "1-5")
        far = lambda count: [self.line_op(100 * (i + 1), "X") for i in range(count)]
        over = self.refused(self.edit("big.txt", tag, *far(7)))
        self.assertNotIn("holds your edit", over)
        self.assertNotRegex(over, r"(?m)^\d+:")
        within = self.refused(self.edit("big.txt", tag, *far(5)))
        self.assertIn("holds your edit", within)
        self.assertEqual(len(self.text("big.txt").splitlines()), 1000)

    def test_a_normalized_single_match_replace_text_is_held_and_confirms(self):
        op = {"op": "replace_text", "old": "let x =  7;", "new": "let x = 70;"}
        refusal = self.refused(self.edit("f.rs", self.tag, op))
        self.assertIn("never displayed", refusal)
        self.assertIn("let x = 7;", refusal)
        self.assertEqual(self.text("f.rs"), SOURCE)
        receipt = self.applied(self.edit("f.rs", self.hold(refusal)))
        self.assertIn("whitespace normalization", receipt)
        self.assertEqual(self.text("f.rs"), SOURCE.replace("let x = 7;", "let x = 70;"))


class BatchCap(WriteCase):
    def sections(self, count):
        return [
            {"path": f"g{i}.txt", "ops": [{"op": "create_file", "content": f"{i}\n"}]}
            for i in range(count)
        ]

    def test_fifty_sections_apply(self):
        self.applied(self.write(*self.sections(50)))
        for i in range(50):
            self.assertEqual(self.text(f"g{i}.txt"), f"{i}\n")

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

    def test_create_over_its_own_file_after_a_merge_write_is_refused(self):
        body = "".join(f"row {i}\n" for i in range(30))
        tag = TAG.search(self.applied(self.create(body))).group(2)
        outside = body.replace("row 25\n", "outside 25\n")
        (self.root / "n.txt").write_text(outside)
        op = {"op": "replace", "start": 2, "end": 2, "content": "mine 1"}
        self.applied(self.edit("n.txt", tag, op))
        merged = self.text("n.txt")
        self.assertIn("outside 25", merged)
        self.assertIn("mine 1", merged)
        self.refused(self.create("clobber\n"))
        self.assertEqual(self.text("n.txt"), merged)

    def test_create_over_a_moved_file_is_refused_once_it_exists_again(self):
        self.applied(self.create("one\n"))
        self.applied(self.write({"path": "n.txt", "ops": [{"op": "move_file", "dest": "m.txt"}]}))
        (self.root / "n.txt").write_text("one\n")
        self.refused(self.create("two\n"))
        self.assertEqual(self.text("n.txt"), "one\n")

    def test_create_over_a_deleted_file_is_refused_once_it_exists_again(self):
        self.applied(self.create("one\n"))
        self.applied(self.write({"path": "n.txt", "ops": [{"op": "delete_file"}]}))
        (self.root / "n.txt").write_text("one\n")
        self.refused(self.create("two\n"))
        self.assertEqual(self.text("n.txt"), "one\n")


if __name__ == "__main__":
    unittest.main()
