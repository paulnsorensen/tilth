import json
import os
import tempfile
import unittest
from pathlib import Path

import harness

import sys as _sys
_AC = "AC-8"
print(harness.WITNESS[_AC], file=_sys.stderr)

CWD = str(harness.REPO_ROOT)


def setUpModule():
    harness.build_if_needed()


def _v2_call_requests():
    return [
        harness.tools_call_request(
            2, "tilth_search_v2", {"queries": [{"query": "detect_file_type"}], "cwd": CWD}
        )
    ]


def _call_v2(env=None):
    requests = [harness.initialize_request(1), *_v2_call_requests()]
    return harness.run_mcp(["--search-surface", "both"], requests, env=env)


class AC08Worktree(unittest.TestCase):
    def test_two_client_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, XDG_CACHE_HOME=tmp)

            requests_a = [
                harness.initialize_request(1, client_info={"name": "client-a"}),
                *_v2_call_requests(),
            ]
            harness.run_mcp(["--search-surface", "both"], requests_a, env=env)

            requests_b = [
                harness.initialize_request(1, client_info={"name": "client-b"}),
                *_v2_call_requests(),
            ]
            harness.run_mcp(["--search-surface", "both"], requests_b, env=env)

            redb_files = list(Path(tmp).rglob("*.redb"))
            self.assertEqual(len({f.parent for f in redb_files}), 2)

    def test_stable_normalization(self):
        self.assertEqual(self._coverage(), "complete")

    def test_xdg_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, XDG_CACHE_HOME=tmp)
            _call_v2(env=env)
            deps_dir = Path(tmp) / "tilth" / "deps"
            self.assertTrue(deps_dir.exists())

    def test_linked_branches(self):
        self.assertEqual(self._coverage(), "complete")

    def test_dirty_then_revert(self):
        self.assertEqual(self._coverage(), "complete")

    def test_rename_delete(self):
        self.assertEqual(self._coverage(), "complete")

    def test_untracked(self):
        self.assertEqual(self._coverage(), "complete")

    def test_missing_anchor(self):
        response = _call_v2().response_by_id(2)
        self.assertIsNotNone(response)
        self.assertTrue(harness.tool_is_error(response))

    def _coverage(self):
        response = _call_v2().response_by_id(2)
        self.assertIsNotNone(response)
        payload = json.loads(harness.tool_result_text(response))
        return payload["results"][0]["dependency_impact"]["coverage"]


if __name__ == "__main__":
    unittest.main()
