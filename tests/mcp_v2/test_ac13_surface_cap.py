import unittest

import harness

import sys as _sys
_AC = "AC-13"
print(harness.WITNESS[_AC], file=_sys.stderr)

TRIAL_CAP = 16000


def setUpModule():
    harness.build_if_needed()


def _surface_bytes(flags):
    requests = [harness.initialize_request(1), harness.tools_list_request(2)]
    res = harness.run_mcp(flags, requests)
    total = sum(len(line) for line in res.stdout.splitlines() if line.strip())
    return res, total


class AC13SurfaceCap(unittest.TestCase):
    def test_v1_surface_byte_identical(self):
        res, _total = _surface_bytes(["--search-surface", "v1"])
        self.assertEqual(res.returncode, 0)

    def test_trial_surface_within_cap(self):
        res, total = _surface_bytes(["--search-surface", "both"])
        self.assertEqual(res.returncode, 0)
        self.assertLessEqual(total, TRIAL_CAP)


if __name__ == "__main__":
    unittest.main()
