import os
import tempfile
import unittest
from pathlib import Path

import harness

import sys as _sys
_AC = "AC-9"
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


class AC09ClientInfo(unittest.TestCase):
    def test_named_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, XDG_CACHE_HOME=tmp)
            requests = [
                harness.initialize_request(1, client_info={"name": "Claude Code"}),
                *_v2_call_requests(),
            ]
            res = harness.run_mcp(["--search-surface", "both"], requests, env=env)
            self.assertEqual(res.returncode, 0)
            cache_paths = list(Path(tmp).rglob("*"))
            self.assertTrue(any("claude-code" in str(p) for p in cache_paths))

    def test_absent_clientinfo(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, XDG_CACHE_HOME=tmp)
            requests = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                *_v2_call_requests(),
            ]
            res = harness.run_mcp(["--search-surface", "both"], requests, env=env)
            self.assertEqual(res.returncode, 0)
            cache_paths = [p for p in Path(tmp).rglob("*") if p.is_file()]
            self.assertTrue(len(cache_paths) > 0)

    def test_normalization_stability(self):
        with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
            client_info = {"name": "Claude Code"}
            requests = [
                harness.initialize_request(1, client_info=client_info),
                *_v2_call_requests(),
            ]
            env_a = dict(os.environ, XDG_CACHE_HOME=tmp_a)
            env_b = dict(os.environ, XDG_CACHE_HOME=tmp_b)
            harness.run_mcp(["--search-surface", "both"], requests, env=env_a)
            harness.run_mcp(["--search-surface", "both"], requests, env=env_b)

            def relative_paths(base):
                return sorted(str(p.relative_to(base)) for p in Path(base).rglob("*"))

            self.assertEqual(relative_paths(tmp_a), relative_paths(tmp_b))
            self.assertTrue(len(relative_paths(tmp_a)) > 0)


if __name__ == "__main__":
    unittest.main()
