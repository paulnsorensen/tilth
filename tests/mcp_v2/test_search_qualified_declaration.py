"""Search resolves qualified names and pasted declaration lines to definitions.

A qualified name (`Owner::name`, `Owner.name`) used to miss or fall to a
literal search, and a pasted declaration line with a parameter list routed to
regex and returned 0 matches. Each miss cost the agent a retry.
"""
import json
import tempfile
import unittest
from pathlib import Path

import harness


def setUpModule():
    harness.build_if_needed()


RUST_SOURCE = """pub struct Alpha;
pub struct Beta;

impl Alpha {
    pub fn run(&self) -> u8 {
        1 // alpha
    }
}

impl Beta {
    pub fn run(&self) -> u8 {
        2 // beta
    }
}
"""

PYTHON_SOURCE = """import os


class Store:
    def get(self, key):
        return os.path.join("cache", key)
"""


class QualifiedDeclarationQuery(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "lib.rs").write_text(RUST_SOURCE)
        (root / "store.py").write_text(PYTHON_SOURCE)
        self.cwd = str(root)

    def search(self, query):
        request = harness.tools_call_request(
            2, "tilth_search", {"cwd": self.cwd, "queries": [{"query": query}]})
        run = harness.run_mcp([], [harness.initialize_request(), request])
        response = run.response_by_id(2)
        self.assertFalse(harness.tool_is_error(response), harness.tool_result_text(response))
        return json.loads(harness.tool_result_text(response))

    def normalization(self, payload, query):
        for entry in payload["diagnostics"].get("normalizations", []):
            if entry["query"] == query:
                return entry
        self.fail(f"no normalization for {query!r}: {payload['diagnostics']}")

    def assert_run_candidates(self, result):
        self.assertEqual(result["resolved_as"], "ambiguous", result)
        lines = sorted(candidate["line"] for candidate in result["candidates"])
        self.assertEqual(lines, [5, 11], result)
        for candidate in result["candidates"]:
            self.assertEqual(candidate["path"], "lib.rs", candidate)

    def test_rust_qualifier_selects_its_owner(self):
        payload = self.search("Beta::run")
        result = payload["results"][0]
        self.assertEqual(result["resolved_as"], "symbol", result)
        self.assertEqual(result["target"]["path"], "lib.rs", result)
        self.assertEqual(result["target"]["line"], 11, result)
        self.assertIn("// beta", result["core"])
        self.assertNotIn("// alpha", result["core"])
        diag = self.normalization(payload, "Beta::run")
        self.assertEqual(diag["normalized_to"], "run", diag)
        self.assertEqual(diag["owner"], "Beta", diag)

    def test_pasted_declaration_line_returns_both_definitions(self):
        payload = self.search("pub fn run(&self)")
        self.assert_run_candidates(payload["results"][0])
        diag = self.normalization(payload, "pub fn run(&self)")
        self.assertEqual(diag["normalized_to"], "run", diag)
        self.assertEqual(diag["stripped_keyword"], "fn", diag)

    def test_unknown_owner_returns_every_definition_and_names_the_owners(self):
        payload = self.search("Gamma::run")
        self.assert_run_candidates(payload["results"][0])
        diag = self.normalization(payload, "Gamma::run")
        self.assertEqual(diag["owner"], "Gamma", diag)
        self.assertIs(diag["owner_matched"], False, diag)
        self.assertEqual(diag["owners_found"], ["Alpha", "Beta"], diag)

    def test_python_dotted_name_and_declaration_line_find_the_method(self):
        for query in ("Store.get", "def get(self, key):"):
            with self.subTest(query=query):
                payload = self.search(query)
                result = payload["results"][0]
                self.assertEqual(result["resolved_as"], "symbol", result)
                self.assertEqual(result["target"]["path"], "store.py", result)
                self.assertIn("def get(self, key):", result["core"])
                self.assertEqual(self.normalization(payload, query)["normalized_to"], "get")

    def test_intentional_regex_stays_a_regex(self):
        payload = self.search("foo|bar")
        self.assertEqual(payload["results"][0]["resolved_as"], "regex", payload)
        self.assertEqual(payload["diagnostics"], {}, payload)

    def test_dotted_text_without_a_definition_stays_literal(self):
        payload = self.search("os.path.join")
        result = payload["results"][0]
        self.assertEqual(result["resolved_as"], "literal", result)
        self.assertIn("os.path.join(", result["preview"])
        self.assertEqual(payload["diagnostics"], {}, payload)

    def test_bare_name_is_unchanged(self):
        payload = self.search("run")
        self.assert_run_candidates(payload["results"][0])
        self.assertEqual(payload["diagnostics"], {}, payload)


if __name__ == "__main__":
    unittest.main()
