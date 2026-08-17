import json
import unittest

import harness

import sys as _sys
_AC = "AC-10"
print(harness.WITNESS[_AC], file=_sys.stderr)


class AC10Spike(unittest.TestCase):
    def test_spike_verdict_pass(self):
        verdict_path = harness.REPO_ROOT / "spikes" / "redb-deps" / "verdict.json"
        self.assertTrue(verdict_path.exists())
        verdict = json.loads(verdict_path.read_text())
        self.assertEqual(verdict.get("gate"), "pass")


if __name__ == "__main__":
    unittest.main()
