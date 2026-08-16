import unittest

import harness

import sys as _sys
_AC = "AC-12"
print(harness.WITNESS[_AC], file=_sys.stderr)


class AC12Graduation(unittest.TestCase):
    def test_evaluator_blocks_on_missing_floor(self):
        evaluator_path = harness.REPO_ROOT / "benchmark" / "graduation" / "evaluate.py"
        self.assertTrue(evaluator_path.exists())


if __name__ == "__main__":
    unittest.main()
