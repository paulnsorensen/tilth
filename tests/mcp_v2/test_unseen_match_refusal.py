"""A refusal for unseen matches displays them, so the same call then applies.

`rewrite` and `replace_text` with `all` or `count` need every match on a line
that a response displayed. When unseen matches are the only reason to refuse,
the refusal applies nothing, prints each match as `N:content` lines under
`[path#TAG]`, and records the printed lines as displayed. The identical second
call then applies, also from a resumed process. Every other refusal keeps its
message and shows no lines, so its identical retry stays refused.
"""
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import harness


def setUpModule():
    harness.build_if_needed()


CALLS = 14
# A section read expands to its enclosing construct, so `foo` and a comment
# header keep the first call below what a read of lines 1-5 displays. Each
# `useN` function is 6 lines long and calls `foo` on its 4th line.
CALL_LINES = [11 + 6 * i for i in range(CALLS)]
SOURCE = "// Call sites of foo.\n// Each useN calls foo once.\n\nfn foo(x: i32) -> i32 {\n    x\n}\n\n" + "".join(
    f"fn use{i}() -> i32 {{\n    let x = {i};\n    let y = x + 1;\n    foo(x) + y\n}}\n\n"
    for i in range(CALLS)
)
CALL = "    foo(x) + y"
REWRITE = {"op": "rewrite", "pattern": "foo($A)", "rewrite": "bar($A)"}
REWRITTEN = SOURCE.replace(CALL, "    bar(x) + y")
NUMBERED = re.compile(r"^(\d+):(.*)$", re.MULTILINE)


class UnseenMatchRefusal(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.checkout("repo")

    def checkout(self, name):
        """A fresh directory: the seen store keeps lines per checkout."""
        self.root = self.base / name
        self.root.mkdir()
        self.cwd = str(self.root)
        self.target = self.root / "f.rs"
        self.target.write_text(SOURCE)

    def server(self, env=None):
        server = harness.Server(env=env, cwd=self.cwd)
        self.addCleanup(server.close)
        return server

    def tag(self, server):
        """Read lines 1-5 only and return the whole-file tag."""
        response = server.call("tilth_read", {"cwd": self.cwd, "paths": ["f.rs#1-5"]})
        text = harness.tool_result_text(response)
        self.assertFalse(harness.tool_is_error(response), text)
        self.assertNotIn("foo(x) + y", text, "the read must not display a call site")
        return re.search(r"\[f\.rs#([0-9A-F]{4})\]", text).group(1)

    def write(self, server, tag, *ops):
        edit = {"path": "f.rs", "tag": tag, "ops": list(ops)}
        return server.call("tilth_write", {"cwd": self.cwd, "edits": [edit]})

    def assert_refused(self, response, source=SOURCE):
        text = harness.tool_result_text(response)
        self.assertTrue(harness.tool_is_error(response), text)
        self.assertEqual(self.target.read_text(), source, "a refused edit must not write")
        return text

    def assert_applied(self, response, expected):
        self.assertFalse(harness.tool_is_error(response), harness.tool_result_text(response))
        self.assertEqual(self.target.read_text(), expected)

    def shown(self, refusal, tag):
        """The `N:content` lines that follow the `[f.rs#TAG]` header."""
        header = f"[f.rs#{tag}]"
        self.assertIn(header, refusal)
        return [(int(n), line) for n, line in NUMBERED.findall(refusal.split(header, 1)[1])]

    def test_refusal_shows_unseen_rewrite_matches_and_the_same_call_applies(self):
        server = self.server()
        tag = self.tag(server)
        refusal = self.assert_refused(self.write(server, tag, REWRITE))
        retry = self.write(server, tag, REWRITE)
        self.assert_applied(retry, REWRITTEN)
        self.assertEqual(self.target.read_text().count("bar(x)"), CALLS)
        self.assertIn("never displayed", refusal)
        self.assertIn("same call", refusal)
        self.assertEqual(self.shown(refusal, tag), [(n, CALL) for n in CALL_LINES])

    def test_refusal_shows_unseen_replace_text_matches_and_the_same_call_applies(self):
        for extra in ({"all": True}, {"count": CALLS}):
            with self.subTest(extra=extra):
                self.checkout(next(iter(extra)))
                server = self.server()
                tag = self.tag(server)
                op = {"op": "replace_text", "old": "foo(x) + y", "new": "bar(x) + y", **extra}
                refusal = self.assert_refused(self.write(server, tag, op))
                self.assertEqual(self.shown(refusal, tag), [(n, CALL) for n in CALL_LINES])
                self.assert_applied(self.write(server, tag, op), REWRITTEN)

    def test_a_resumed_process_applies_the_same_call(self):
        subprocess.run(["git", "init", "-q", self.cwd], check=True)
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        env = dict(os.environ, XDG_STATE_HOME=state.name)
        first = self.server(env)
        tag = self.tag(first)
        self.assert_refused(self.write(first, tag, REWRITE))
        first.close()
        # `claude --resume` starts a new tilth process with no read of its own.
        self.assert_applied(self.write(self.server(env), tag, REWRITE), REWRITTEN)

    def assert_stays_refused(self, server, tag, op, source, expected):
        for attempt in ("first call", "identical retry"):
            refusal = self.assert_refused(self.write(server, tag, op), source)
            self.assertIn(expected, refusal, attempt)
            self.assertEqual(NUMBERED.findall(refusal), [], f"{attempt} showed lines")

    def test_a_drifted_tag_rewrite_keeps_its_refusal(self):
        server = self.server()
        tag = self.tag(server)
        drifted = "// drift\n" + SOURCE
        self.target.write_text(drifted)
        self.assert_stays_refused(server, tag, REWRITE, drifted, "file changed since the read")

    def test_a_line_op_on_an_undisplayed_line_keeps_its_refusal(self):
        server = self.server()
        op = {"op": "replace", "start": CALL_LINES[5], "end": CALL_LINES[5], "content": "    bar(x) + y\n"}
        self.assert_stays_refused(server, self.tag(server), op, SOURCE, "never displayed")

    def test_a_single_match_replace_text_keeps_its_refusal(self):
        # A whitespace-normalized single match on an undisplayed line.
        server = self.server()
        op = {"op": "replace_text", "old": "let x =  7;", "new": "let x = 70;"}
        self.assert_stays_refused(server, self.tag(server), op, SOURCE, "never displayed")

    def test_a_count_mismatch_keeps_its_refusal(self):
        server = self.server()
        op = {"op": "replace_text", "old": "foo(x) + y", "new": "bar(x) + y", "count": 5}
        self.assert_stays_refused(server, self.tag(server), op, SOURCE, "expected 5 matches")


if __name__ == "__main__":
    unittest.main()
