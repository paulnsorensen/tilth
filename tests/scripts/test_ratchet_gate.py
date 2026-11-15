"""scripts/ratchet_gate.py fails a regressed or unmeasurable metric and passes a held one."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "scripts" / "ratchet_gate.py"


def ratchet(command, threshold):
    entry = {"direction": "lower", "threshold": threshold, "tolerance": 0.0, "unit": "bytes"}
    if command is not None:
        entry["command"] = command
    return {"metrics": {"probe.bytes": entry}, "version": 1}


class RatchetGateTest(unittest.TestCase):
    def run_gate(self, document):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "probe.json").write_text(json.dumps(document))
            return subprocess.run([sys.executable, str(GATE), directory], capture_output=True, text=True)

    def test_value_at_threshold_passes(self):
        self.assertEqual(self.run_gate(ratchet("printf 'detail\\n5\\n'", 5)).returncode, 0)

    def test_value_above_lower_threshold_fails(self):
        self.assertEqual(self.run_gate(ratchet("printf '6\\n'", 5)).returncode, 1)

    def test_failing_command_fails(self):
        self.assertEqual(self.run_gate(ratchet("echo 1; exit 3", 5)).returncode, 1)

    def test_metric_without_command_fails(self):
        result = self.run_gate(ratchet(None, 5))
        self.assertEqual(result.returncode, 1)
        self.assertIn("no stored command", result.stderr)


if __name__ == "__main__":
    unittest.main()
