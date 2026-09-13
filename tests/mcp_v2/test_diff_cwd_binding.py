"""Two-repository regression for tilth_diff cwd binding (D2).

Every git-backed diff source must run against the request's `cwd`, not the
server's frozen process directory. The server starts inside repository A; each
tilth_diff call targets repository B via `cwd`. The B change must appear and the
A change must not — the exact wrong-checkout hazard the cwd posture exists to
kill.

Before the fix, git-backed sources ran in the server's process dir (repo A), so
the diff reported A's changes for a request pointed at B; these assertions fail
on that original behavior.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

import harness


def setUpModule():
    harness.build_if_needed()


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", repo, *args],
        check=True,
        capture_output=True,
        text=True,
        env=dict(
            os.environ,
            GIT_AUTHOR_NAME="Test",
            GIT_AUTHOR_EMAIL="test@test.com",
            GIT_COMMITTER_NAME="Test",
            GIT_COMMITTER_EMAIL="test@test.com",
        ),
    )


def _write(repo, name, text):
    with open(os.path.join(repo, name), "w") as fh:
        fh.write(text)


def _build_repo(tag):
    """A git repo whose changed files/symbols are all prefixed with `tag`.

    Layout after build:
      - two commits (so `HEAD~1..HEAD` is a valid git-ref range),
      - one unstaged working-tree change,
      - one staged change.
    """
    repo = tempfile.mkdtemp(prefix=f"tilth-diff-{tag}-")
    _git(repo, "init", "-q")

    base = "def base():\n    return 1\n"
    _write(repo, f"{tag}_working.py", base)
    _write(repo, f"{tag}_staged.py", base)
    _write(repo, f"{tag}_ref.py", base)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "c1")

    # Second commit — introduces the git-ref-only symbol in {tag}_ref.py.
    _write(repo, f"{tag}_ref.py", base + f"\ndef only_{tag}_ref():\n    return 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "c2")

    # Unstaged working-tree change.
    _write(
        repo, f"{tag}_working.py", base + f"\ndef only_{tag}_working():\n    return 2\n"
    )

    # Staged change.
    _write(
        repo, f"{tag}_staged.py", base + f"\ndef only_{tag}_staged():\n    return 2\n"
    )
    _git(repo, "add", f"{tag}_staged.py")

    return repo


class DiffCwdBinding(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Repo A is the server's process directory; repo B is the request target.
        cls.repo_a = _build_repo("a")
        cls.repo_b = _build_repo("b")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.repo_a, ignore_errors=True)
        shutil.rmtree(cls.repo_b, ignore_errors=True)

    def _diff_b_from_a(self, source):
        """Start the server in repo A; run tilth_diff against repo B."""
        requests = [
            harness.initialize_request(1),
            harness.tools_call_request(
                2, "tilth_diff", {"cwd": self.repo_b, "source": source}
            ),
        ]
        mcp = harness.run_mcp([], requests, cwd=self.repo_a)
        response = mcp.response_by_id(2)
        self.assertIsNotNone(response, f"no diff response for source={source!r}")
        self.assertFalse(
            harness.tool_is_error(response),
            f"diff errored for source={source!r}: {response}",
        )
        return harness.tool_result_text(response)

    def test_working_source_binds_to_request_cwd(self):
        text = self._diff_b_from_a("working")
        self.assertIn("b_working.py", text, f"repo B change must appear:\n{text}")
        self.assertNotIn(
            "a_working.py", text, f"repo A change must not leak:\n{text}"
        )
        self.assertNotIn("only_a_", text, f"no repo A symbol may appear:\n{text}")

    def test_staged_source_binds_to_request_cwd(self):
        text = self._diff_b_from_a("staged")
        self.assertIn("b_staged.py", text, f"repo B staged change must appear:\n{text}")
        self.assertNotIn(
            "a_staged.py", text, f"repo A staged change must not leak:\n{text}"
        )

    def test_git_ref_source_binds_to_request_cwd(self):
        text = self._diff_b_from_a("HEAD~1..HEAD")
        self.assertIn("b_ref.py", text, f"repo B ref change must appear:\n{text}")
        self.assertNotIn(
            "a_ref.py", text, f"repo A ref change must not leak:\n{text}"
        )


if __name__ == "__main__":
    unittest.main()
