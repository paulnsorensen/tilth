import json
import unittest

import harness

import sys as _sys
_AC = "AC-5"
print(harness.WITNESS[_AC], file=_sys.stderr)

CWD = str(harness.REPO_ROOT)


def setUpModule():
    harness.build_if_needed()


def _contains_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return True
        return any(_contains_key(v, key) for v in obj.values())
    if isinstance(obj, list):
        return any(_contains_key(item, key) for item in obj)
    return False


class AC05Envelope(unittest.TestCase):
    def test_envelope_fields_and_no_route_leak(self):
        requests = [
            harness.initialize_request(1),
            harness.tools_call_request(
                2, "tilth_search_v2", {"queries": [{"query": "detect_file_type"}], "cwd": CWD}
            ),
        ]
        res = harness.run_mcp(["--search-surface", "both"], requests)
        response = res.response_by_id(2)
        self.assertIsNotNone(response)
        payload = json.loads(harness.tool_result_text(response))
        self.assertIn("results", payload)
        self.assertIn("hints", payload)
        self.assertIn("diagnostics", payload)
        self.assertFalse(_contains_key(payload, "routes_tried"))


if __name__ == "__main__":
    unittest.main()
