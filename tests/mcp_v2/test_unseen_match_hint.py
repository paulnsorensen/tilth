"""Attack: an unseen-match rejection names one call that unblocks the same retry.

Each case reads a small section, sends a multi-match edit that touches unseen
lines, follows the rejection's hint in the same agent session, and retries the
same edit with the same tag. The retry must apply. For `replace_text all` and
`rewrite`, the hint is one batched `tilth_read` of small ranges, one per unseen
match. A search hint is not used: auto-routing and walker skips can hide matches.
"""
import json
import os
import re
import tempfile
import unittest
import subprocess
from pathlib import Path

import harness


def setUpModule():
    harness.build_if_needed()


READS = re.compile(r"tilth_read paths (\[.*\]) shows every match range in the current file")
REPLACE_ALL = {"op": "replace_text", "old": "token", "new": "marker", "all": True}
WRAP = {"op": "rewrite", "pattern": "wrap($A)", "rewrite": "wrapped($A)"}
BODY = "".join(f"x{i} = {i}\n" for i in range(80))
WIDE = "y" * 120


class UnseenMatchHint(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.cwd = str(self.root)

    def write(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return target

    def attack(self, path, source, op, *, target=None):
        """Return (read paths, file text) after reject -> hinted read -> retry."""
        if target is None:
            target = self.write(path, source)
        read = harness.tools_call_request(
            2, "tilth_read", {"cwd": self.cwd, "paths": [f"{path}#1-2"]})
        first = harness.run_mcp([], [harness.initialize_request(), read])
        text = harness.tool_result_text(first.response_by_id(2))
        tag = re.search(r"#([0-9A-F]{4})\]", text).group(1)
        edit = {"cwd": self.cwd, "edits": [{"path": path, "tag": tag, "ops": [op]}]}
        rejected = harness.run_mcp([], [
            harness.initialize_request(), read,
            harness.tools_call_request(3, "tilth_write", edit),
        ]).response_by_id(3)
        message = harness.tool_result_text(rejected)
        self.assertEqual(target.read_text(), source, "a rejected edit must not write")
        found = READS.search(message)
        self.assertIsNotNone(found, f"rejection names no batched read: {message}")
        paths = json.loads(found.group(1))
        self.assertLessEqual(len(paths), 20, "tilth_read takes at most 20 paths")
        # One agent session: the first read, then the hinted read, then the retry.
        followed = harness.run_mcp([], [
            harness.initialize_request(), read,
            harness.tools_call_request(4, "tilth_read", {"cwd": self.cwd, "paths": paths}),
            harness.tools_call_request(3, "tilth_write", edit),
        ])
        shown = harness.tool_result_text(followed.response_by_id(4))
        # Single reads flag `"truncated":true`; batched reads print `... truncated`.
        self.assertNotIn("truncated", shown, "the hinted read must fit its budget")
        written = followed.response_by_id(3)
        self.assertFalse(
            harness.tool_is_error(written),
            f"hint {paths} did not unblock the retry:\n"
            f"{harness.tool_result_text(written)}\nhinted read said:\n{shown[:1500]}",
        )
        return paths, target.read_text()

    def outside_cwd(self, name, source):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        target = Path(outside.name) / name
        target.write_text(source)
        return target

    def symlinked_cwd(self):
        parent = tempfile.TemporaryDirectory()
        self.addCleanup(parent.cleanup)
        link = Path(parent.name) / "link"
        os.symlink(self.root, link)
        self.cwd = str(link)

    def go_calls(self, name, calls, *, define):
        """A Go file with `calls` calls of `name`, each inside its own func."""
        head = f"package p\n\nfunc {name}(i int) int {{\n\treturn i\n}}\n" if define else "package p\n"
        funcs = "".join(
            f"\nfunc use{i}() int {{\n\tx := {i}\n\ty := x + 1\n\treturn {name}(y)\n}}\n"
            for i in range(calls))
        return head + funcs

    # replace_text all: one read line per unseen occurrence.

    def test_replace_all_identifier_defined_in_the_same_file(self):
        source = self.go_calls("oldName", 8, define=True)
        op = {"op": "replace_text", "old": "oldName", "new": "newName", "all": True}
        _, text = self.attack("p.go", source, op)
        self.assertEqual(text, source.replace("oldName", "newName"))

    def test_drifted_read_hint_retry_uses_current_tag(self):
        source = f"a = token\n{BODY}b = token\n"
        target = self.write("drift.py", source)
        with subprocess.Popen([str(harness.BIN), "--mcp"], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True, bufsize=1) as proc:
            def call(request):
                proc.stdin.write(json.dumps(request) + "\n")
                proc.stdin.flush()
                return json.loads(proc.stdout.readline())

            call(harness.initialize_request())
            read = harness.tools_call_request(
                2, "tilth_read", {"cwd": self.cwd, "paths": ["drift.py#1-2"]})
            tag = re.search(r"#([0-9A-F]{4})\]",
                            harness.tool_result_text(call(read))).group(1)
            target.write_text("# drift\n" + source)
            edit = {"cwd": self.cwd, "edits": [{"path": "drift.py", "tag": tag,
                                                  "ops": [REPLACE_ALL]}]}
            rejected = harness.tool_result_text(call(
                harness.tools_call_request(3, "tilth_write", edit)))
            paths = json.loads(READS.search(rejected).group(1))
            self.assertEqual(paths, ["drift.py#2-2", "drift.py#83-83"])
            shown = harness.tool_result_text(call(harness.tools_call_request(
                4, "tilth_read", {"cwd": self.cwd, "paths": paths})))
            current_tag = re.search(r"#([0-9A-F]{4})\]", shown).group(1)
            self.assertNotEqual(current_tag, tag)
            edit["edits"][0]["tag"] = current_tag
            written = call(harness.tools_call_request(5, "tilth_write", edit))
            self.assertFalse(harness.tool_is_error(written),
                             harness.tool_result_text(written))
            proc.stdin.close()
        self.assertEqual(target.read_text(), "# drift\n" + source.replace("token", "marker"))

    def test_replace_all_regex_metacharacters(self):
        source = f"a = render(w)\n{BODY}b = render(w)\n{BODY}c = m[k] + render(w)\n"
        op = {"op": "replace_text", "old": "render(w)", "new": "draw(w)", "all": True}
        _, text = self.attack("meta.py", source, op)
        self.assertEqual(text, source.replace("render(w)", "draw(w)"))

    def test_replace_all_substring_inside_identifier(self):
        source = f"a = old_name\n{BODY}b = my_old_name_x\n"
        op = {"op": "replace_text", "old": "old_name", "new": "new_name", "all": True}
        _, text = self.attack("sub.py", source, op)
        self.assertEqual(text, source.replace("old_name", "new_name"))

    def test_replace_all_inside_functions_of_a_long_test_file(self):
        source = self.go_calls("legacyCall", 45, define=False)
        op = {"op": "replace_text", "old": "legacyCall", "new": "modernCall", "all": True}
        _, text = self.attack("render/render_test.go", source, op)
        self.assertEqual(text.count("modernCall("), 45)

    def test_replace_all_far_apart_occurrences(self):
        body = "".join(f"x{i} = {i}\n" for i in range(200))
        source = f"a = old_name(1)\n{body}b = old_name(2)\n{body}c = old_name(3)\n"
        op = {"op": "replace_text", "old": "old_name", "new": "new_name", "all": True}
        paths, text = self.attack("mod.py", source, op)
        self.assertEqual(paths, ["mod.py#202-202", "mod.py#403-403"])
        self.assertEqual(text, source.replace("old_name", "new_name"))

    def test_replace_all_long_lines_stay_under_the_read_budget(self):
        # Two unseen occurrences 3000 wide lines apart: one covering span would truncate.
        wide = "".join(f"x{i} = '{WIDE}'\n" for i in range(3000))
        source = f"x = 0\nx = 1\na = token\n{wide}b = token\n"
        paths, text = self.attack("wide.py", source, REPLACE_ALL)
        self.assertEqual(paths, ["wide.py#3-3", "wide.py#3004-3004"])
        self.assertEqual(text.count("marker"), 2)

    @unittest.expectedFailure
    def test_replace_all_more_spread_occurrences_than_read_paths(self):
        """Known residual (ADR-009 open issue): over 20 spread occurrences merge
        into wide ranges, and a truncated read still marks them seen. Remove
        this decorator when tilth_read records only displayed lines."""
        block = "".join(f"x = '{WIDE}'\n" for _ in range(100))
        source = "x = 0\nx = 1\n" + "".join(f"a = token\n{block}" for _ in range(32))
        self.attack("spread.py", source, REPLACE_ALL)

    def test_replace_all_multiline_old(self):
        body = "".join(f"x{i} = {i}\n" for i in range(100))
        block = "if ready:\n    go()\n"
        source = f"{block}{body}{block}"
        op = {"op": "replace_text", "old": block, "new": "if ready:\n    run()\n", "all": True}
        _, text = self.attack("flow.py", source, op)
        self.assertEqual(text.count("run()"), 2, text)

    def test_replace_all_many_occurrences(self):
        source = "".join(f"v{i} = legacy_call({i})\n" + "pad = 0\n" * 4 for i in range(150))
        op = {"op": "replace_text", "old": "legacy_call", "new": "modern_call", "all": True}
        _, text = self.attack("many.py", source, op)
        self.assertNotIn("legacy_call", text)

    def test_replace_all_in_glob_special_path(self):
        source = f"a = token\n{BODY}b = token\n"
        _, text = self.attack("routes/{slug}*.py", source, REPLACE_ALL)
        self.assertEqual(text.count("marker"), 2, text)

    def test_replace_all_under_a_symlinked_cwd(self):
        self.symlinked_cwd()
        source = f"a = token\n{BODY}b = token\n"
        _, text = self.attack("linked.py", source, REPLACE_ALL)
        self.assertEqual(text.count("marker"), 2, text)

    def test_replace_all_outside_cwd(self):
        source = f"a = token\n{BODY}b = token\n"
        target = self.outside_cwd("far.py", source)
        _, text = self.attack(str(target), source, REPLACE_ALL, target=target)
        self.assertEqual(text.count("marker"), 2, text)

    # rewrite: one full-span read per unseen match.

    def test_rewrite_multiline_capture(self):
        body = "".join(f"    x{i} = {i}\n" for i in range(60))
        source = f"answer = wrap(\n    1,\n)\n{body}other = wrap(\n    2,\n)\n"
        op = {"op": "rewrite", "pattern": "wrap($$$A)", "rewrite": "wrapped($$$A)"}
        paths, text = self.attack("source.py", source, op)
        # A rewrite must see each whole match, so the read covers lines 1-3.
        self.assertEqual(paths, ["source.py#1-3", "source.py#64-66"])
        self.assertEqual(text.count("wrapped("), 2, text)
        self.assertIn(body, text)

    def test_rewrite_json_hostile_pattern(self):
        source = f'a = wrap("q\\"t", 1)\n{BODY}b = wrap("q\\"t", 2)\n'
        op = {"op": "rewrite", "pattern": 'wrap("q\\"t", $A)', "rewrite": "wrapped($A)"}
        _, text = self.attack("quote.py", source, op)
        self.assertEqual(text.count("wrapped("), 2, text)

    def test_rewrite_in_glob_special_paths(self):
        for path in ("pages/[id].py", "[id].py"):
            with self.subTest(path=path):
                source = f"a = wrap(1)\n{BODY}b = wrap(2)\n"
                _, text = self.attack(path, source, WRAP)
                self.assertEqual(text.count("wrapped("), 2, text)

    def test_rewrite_in_directories_search_skips(self):
        for path in ("src/build/f.py", "pkg/vendor/g.py", "out/gen.py", "target/t.py"):
            with self.subTest(path=path):
                source = f"a = wrap(1)\n{BODY}b = wrap(2)\n"
                _, text = self.attack(path, source, WRAP)
                self.assertEqual(text.count("wrapped("), 2, text)

    def test_rewrite_root_file_with_nested_namesakes(self):
        for i in range(30):
            self.write(f"d{i}/f.py", "".join(f"v{j} = wrap({j})\n" for j in range(60)))
        source = f"a = wrap(1)\n{BODY}b = wrap(2)\n"
        paths, text = self.attack("f.py", source, WRAP)
        self.assertEqual(paths, ["f.py#82-82"])
        self.assertEqual(text.count("wrapped("), 2, text)

    def test_rewrite_with_many_matches(self):
        source = "".join(f"v{i} = wrap({i})\n" for i in range(1100))
        _, text = self.attack("lots.py", source, WRAP)
        self.assertEqual(text.count("wrapped("), 1100)

    def test_rewrite_under_a_symlinked_cwd(self):
        self.symlinked_cwd()
        source = f"a = wrap(1)\n{BODY}b = wrap(2)\n"
        paths, text = self.attack("linked.py", source, WRAP)
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].endswith("linked.py#82-82"), paths)
        self.assertEqual(text.count("wrapped("), 2, text)

    def test_rewrite_outside_cwd(self):
        source = f"a = wrap(1)\n{BODY}b = wrap(2)\n"
        target = self.outside_cwd("far.py", source)
        _, text = self.attack(str(target), source, WRAP, target=target)
        self.assertEqual(text.count("wrapped("), 2, text)


if __name__ == "__main__":
    unittest.main()
