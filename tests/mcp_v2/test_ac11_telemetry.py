import json
import os
import tempfile
import unittest
from pathlib import Path

import harness

import sys as _sys
_AC = "AC-11"
print(harness.WITNESS[_AC], file=_sys.stderr)

CWD = str(harness.REPO_ROOT)

REQUIRED_KEYS = {"verb", "version", "route", "latency_ms", "result_tokens", "client", "worktree"}


def setUpModule():
    harness.build_if_needed()


class AC11Telemetry(unittest.TestCase):
    def test_jsonl_record_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, XDG_STATE_HOME=tmp)
            requests = [
                harness.initialize_request(1),
                harness.tools_call_request(
                    2, "tilth_search_v2", {"queries": [{"query": "detect_file_type"}], "cwd": CWD}
                ),
            ]
            harness.run_mcp(["--search-surface", "both"], requests, env=env)

            telemetry_dir = Path(tmp) / "tilth" / "telemetry"
            self.assertTrue(telemetry_dir.exists())
            jsonl_files = list(telemetry_dir.glob("*.jsonl"))
            self.assertTrue(len(jsonl_files) > 0)

            records = []
            for f in jsonl_files:
                for line in f.read_text().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    records.append(json.loads(line))

            self.assertTrue(len(records) > 0)
            for record in records:
                self.assertTrue(REQUIRED_KEYS.issubset(record.keys()))
                self.assertNotIn("source", record)
                self.assertNotIn("content", record)


if __name__ == "__main__":
    unittest.main()
