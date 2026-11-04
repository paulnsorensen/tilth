"""A resumed agent session keeps the seen lines of an earlier tilth process.

`claude --resume` starts a new `tilth --mcp` process, which once forgot every
earlier read: a write to a line the agent had read was refused. Each case runs
one process per agent session. A first process reads a section and exits; a
later process in the same checkout edits a line that section displayed. Every
process saves state under a temporary XDG_STATE_HOME and HOME.
"""
import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

import harness


def setUpModule():
    harness.build_if_needed()


SOURCE = "".join(f"line_{i:02d} = {i}\n" for i in range(1, 41))
TAG = re.compile(r"\[[^\]]*#([0-9A-F]{4})\]")


def replace(path, tag, line, content):
    op = {"op": "replace", "start": line, "end": line, "content": content + "\n"}
    return ("tilth_write", {"edits": [{"path": str(path), "tag": tag, "ops": [op]}]})


class SeenPersist(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.state = self.base / "state"
        self.home = self.base / "home"
        self.home.mkdir()
        self.env = dict(os.environ, XDG_STATE_HOME=str(self.state), HOME=str(self.home))
        self.repo = self.checkout("repo")

    def checkout(self, name):
        root = self.base / name
        (root / "sub").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(root)], check=True, env=self.env)
        (root / "f.py").write_text(SOURCE)
        return root

    def session(self, cwd, *calls, env=None):
        """Run one tilth process; return its tool responses in call order."""
        requests = [harness.initialize_request()]
        for req_id, (name, args) in enumerate(calls, start=2):
            requests.append(harness.tools_call_request(req_id, name, {"cwd": str(cwd), **args}))
        mcp = harness.run_mcp([], requests, env=env or self.env, cwd=str(cwd))
        self.assertEqual(mcp.returncode, 0, mcp.stderr)
        responses = [mcp.response_by_id(req_id) for req_id in range(2, len(calls) + 2)]
        self.assertNotIn(None, responses, mcp.stderr)
        return responses

    def read(self, cwd, path):
        """Read `path` in its own process and return the whole-file tag."""
        (response,) = self.session(cwd, ("tilth_read", {"paths": [path]}))
        text = harness.tool_result_text(response)
        self.assertFalse(harness.tool_is_error(response), text)
        return TAG.search(text).group(1)

    def state_files(self):
        return sorted((self.state / "tilth" / "seen").glob("*.json"))

    def assert_applied(self, response):
        self.assertFalse(harness.tool_is_error(response), harness.tool_result_text(response))

    def assert_refused(self, response):
        self.assertTrue(harness.tool_is_error(response), harness.tool_result_text(response))

    def test_resumed_process_edits_a_line_an_earlier_process_displayed(self):
        tag = self.read(self.repo, "f.py#10-20")
        (written,) = self.session(self.repo, replace("f.py", tag, 15, "line_15 = 'resumed'"))
        self.assert_applied(written)
        self.assertEqual((self.repo / "f.py").read_text().splitlines()[14], "line_15 = 'resumed'")
        # One private state file under the override; it holds no file text.
        files = self.state_files()
        self.assertEqual(len(files), 1, files)
        self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
        self.assertNotIn("line_1", files[0].read_text())
        self.assertFalse((self.home / ".local" / "state" / "tilth").exists())

    def test_a_resumed_read_of_other_lines_keeps_the_earlier_lines(self):
        # The benchmark case: the resumed process reads a few lines itself, then
        # edits a line that only the earlier process displayed.
        tag = self.read(self.repo, "f.py#10-20")
        read, written = self.session(
            self.repo,
            ("tilth_read", {"paths": ["f.py#1-3"]}),
            replace("f.py", tag, 15, "line_15 = 'resumed'"),
        )
        self.assertEqual(TAG.search(harness.tool_result_text(read)).group(1), tag)
        self.assert_applied(written)

    def test_resumed_processes_chain_edits_on_displayed_lines(self):
        tag = self.read(self.repo, "f.py#10-20")
        (first,) = self.session(self.repo, replace("f.py", tag, 15, "line_15 = 'a'"))
        self.assert_applied(first)
        receipt_tag = TAG.search(harness.tool_result_text(first)).group(1)
        # A third process edits another line the first process displayed.
        (second,) = self.session(self.repo, replace("f.py", receipt_tag, 12, "line_12 = 'b'"))
        self.assert_applied(second)
        lines = (self.repo / "f.py").read_text().splitlines()
        self.assertEqual((lines[11], lines[14]), ("line_12 = 'b'", "line_15 = 'a'"))

    def test_a_line_no_process_displayed_is_still_refused(self):
        tag = self.read(self.repo, "f.py#10-20")
        (written,) = self.session(self.repo, replace("f.py", tag, 30, "x = 0"))
        self.assert_refused(written)
        self.assertEqual((self.repo / "f.py").read_text(), SOURCE)

    def test_changed_content_does_not_inherit_the_record(self):
        old_tag = self.read(self.repo, "f.py#10-20")
        changed = SOURCE.replace("line_01 = 1\n", "line_01 = 'changed'\n")
        (self.repo / "f.py").write_text(changed)
        (stale,) = self.session(self.repo, replace("f.py", old_tag, 15, "x = 0"))
        self.assert_refused(stale)
        # The new content's tag carries only what a process displayed for it.
        new_tag = self.read(self.repo, "f.py#1-2")
        self.assertNotEqual(new_tag, old_tag)
        (fresh,) = self.session(self.repo, replace("f.py", new_tag, 15, "x = 0"))
        self.assert_refused(fresh)
        self.assertEqual((self.repo / "f.py").read_text(), changed)

    def test_another_checkout_does_not_share_the_record(self):
        other = self.checkout("other")
        tag = self.read(self.repo, "f.py#10-20")
        # Same content in another checkout: same tag, no record.
        (copy,) = self.session(other, replace("f.py", tag, 15, "x = 0"))
        self.assert_refused(copy)
        # The first checkout's file through the other checkout's cwd: no record.
        (absolute,) = self.session(other, replace(self.repo / "f.py", tag, 15, "x = 0"))
        self.assert_refused(absolute)
        self.assertEqual((self.repo / "f.py").read_text(), SOURCE)
        self.assertEqual((other / "f.py").read_text(), SOURCE)
        # A subdirectory of the first checkout shares its record.
        (sub,) = self.session(self.repo / "sub",
                              replace(self.repo / "f.py", tag, 15, "line_15 = 'sub'"))
        self.assert_applied(sub)

    def test_a_corrupt_state_file_breaks_neither_reads_nor_writes(self):
        tag = self.read(self.repo, "f.py#10-20")
        (state_file,) = self.state_files()
        state_file.write_text("{not json")
        # The record is lost: the resumed write is refused, and nothing crashes.
        (lost,) = self.session(self.repo, replace("f.py", tag, 15, "x = 0"))
        self.assert_refused(lost)
        read, written = self.session(
            self.repo,
            ("tilth_read", {"paths": ["f.py#10-20"]}),
            replace("f.py", tag, 15, "line_15 = 'ok'"),
        )
        self.assertFalse(harness.tool_is_error(read), harness.tool_result_text(read))
        self.assert_applied(written)
        self.assertIsInstance(json.loads(state_file.read_text())["entries"], list)

    def test_an_unusable_state_location_breaks_neither_reads_nor_writes(self):
        blocker = self.base / "not-a-directory"
        blocker.write_text("")
        env = dict(self.env, XDG_STATE_HOME=str(blocker))
        (first,) = self.session(self.repo, ("tilth_read", {"paths": ["f.py#10-20"]}), env=env)
        self.assertFalse(harness.tool_is_error(first), harness.tool_result_text(first))
        tag = TAG.search(harness.tool_result_text(first)).group(1)
        read, written = self.session(
            self.repo,
            ("tilth_read", {"paths": ["f.py#10-20"]}),
            replace("f.py", tag, 15, "line_15 = 'ok'"),
            env=env,
        )
        self.assertFalse(harness.tool_is_error(read), harness.tool_result_text(read))
        self.assert_applied(written)
        self.assertEqual(blocker.read_text(), "")


if __name__ == "__main__":
    unittest.main()
