"""Zero-result CLI hints must not name a `kind` parameter.

Neither `tilth_search` nor the CLI takes `kind`; routing is automatic.
A hint that says "try kind: content" sends the caller to a dead end.
MCP misses use the `Merged` hint or no preview, so the CLI owns this check.
"""
import subprocess
import unittest

import harness

CWD = str(harness.REPO_ROOT)

# Each query shape reaches its own EmptyHint branch. The literals are split
# so a search cannot match this file.
CLI_MISSES = [
    ("ZzNoSuch" + "SymbolQq", "no symbols matched; check spelling or widen the scope"),
    ("zz no such " + "phrase qq", "no content matches; try an identifier or a broader pattern"),
    ("/zz[0-9]+" + "noSuchQq/", "regex matched zero content; try an identifier or a broader pattern"),
]


def setUpModule():
    harness.build_if_needed()


def hint_line(stdout):
    return next(
        (line.strip() for line in stdout.splitlines() if line.strip().startswith("Hint:")),
        None,
    )


class ZeroResultHints(unittest.TestCase):
    def test_cli_miss_hints_never_name_kind(self):
        for query, expected in CLI_MISSES:
            with self.subTest(query=query):
                proc = subprocess.run(
                    [str(harness.BIN), query, "--scope", "src"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    cwd=CWD,
                )
                self.assertIn("0 matches", proc.stdout)
                hint = hint_line(proc.stdout)
                self.assertEqual(hint, f"Hint: {expected}", proc.stdout)


if __name__ == "__main__":
    unittest.main()
