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
            2, "tilth_search", {"queries": [{"query": "detect_file_type"}], "cwd": CWD}
        )
    ]


def _call_v2(env=None):
    requests = [harness.initialize_request(1), *_v2_call_requests()]
    return harness.run_mcp([], requests, env=env)


class AC08Worktree(unittest.TestCase):
    def test_search_succeeds_with_isolated_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, XDG_CACHE_HOME=tmp)
            response = _call_v2(env=env).response_by_id(2)
            self.assertIsNotNone(response)
            self.assertFalse(harness.tool_is_error(response))
            payload = json.loads(harness.tool_result_text(response))
            self.assertTrue(payload["results"])
            self.assertEqual(list(Path(tmp).rglob("*.redb")), [])


if __name__ == "__main__":
    unittest.main()
