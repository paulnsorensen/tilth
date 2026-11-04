"""Search keeps a batch alive past one bad entry and resolves kind-keyword queries.

Each test replays a cause from the stock A/B trajectory diagnosis: one bad
structural pattern lost every query in its batch, and `trait Matcher` came back
ambiguous with a large unrelated preview.
"""
import json
import tempfile
import unittest
from pathlib import Path

import harness


def setUpModule():
    harness.build_if_needed()


TRAIT_SOURCE = """pub trait Matcher {
    fn is_match(&self, line: &str) -> bool;
}
"""

IMPLEMENTORS = ["Regex", "Literal", "Glob", "Prefix", "Suffix", "Never"]


def impl_source(name):
    return (
        "use crate::matcher::Matcher;\n\n"
        f"pub struct {name};\n\n"
        f"impl Matcher for {name} {{\n"
        "    fn is_match(&self, _line: &str) -> bool {\n"
        "        true\n"
        "    }\n"
        "}\n"
    )


class SearchFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "matcher.rs").write_text(TRAIT_SOURCE)
        for name in IMPLEMENTORS:
            (self.root / f"{name.lower()}.rs").write_text(impl_source(name))
        (self.root / "notes.md").write_text("A Matcher decides. Use Matcher here.\n" * 20)

    def call(self, queries):
        request = harness.tools_call_request(
            2, "tilth_search", {"cwd": str(self.root), "queries": queries})
        run = harness.run_mcp([], [harness.initialize_request(), request])
        response = run.response_by_id(2)
        self.assertIsNotNone(response)
        return response

    def search(self, queries):
        response = self.call(queries)
        self.assertFalse(harness.tool_is_error(response), harness.tool_result_text(response))
        return json.loads(harness.tool_result_text(response))


class BadStructuralEntry(SearchFixture):
    def test_invalid_pattern_returns_an_entry_error_and_keeps_siblings(self):
        payload = self.search([
            {"query": "impl.* Matcher for"},
            {"pattern": "fn broken( {", "language": "rust"},
            {"query": "is_match"},
        ])
        first, bad, last = payload["results"]
        self.assertEqual(first["resolved_as"], "regex", first)
        self.assertEqual(first["total_found"], len(IMPLEMENTORS), first)
        self.assertEqual(bad["status"], "error", bad)
        self.assertEqual(bad["query"], "fn broken( {", bad)
        self.assertIn("invalid structural pattern", bad["error"])
        self.assertNotEqual(last["status"], "error", last)
        self.assertIn(last["resolved_as"], ("symbol", "ambiguous"), last)

    def test_unsupported_language_returns_an_entry_error(self):
        payload = self.search([
            {"pattern": "wrap($A)", "language": "make"},
            {"pattern": "pub struct $NAME;", "language": "rust"},
        ])
        bad, good = payload["results"]
        self.assertEqual(bad["status"], "error", bad)
        self.assertIn("unsupported structural language", bad["error"])
        self.assertEqual(good["resolved_as"], "structural", good)
        self.assertEqual(good["total_matches"], len(IMPLEMENTORS), good)

    def test_one_bad_entry_alone_is_still_a_tool_error(self):
        response = self.call([{"pattern": "fn broken( {", "language": "rust"}])
        self.assertTrue(harness.tool_is_error(response), response)
        self.assertIn("invalid structural pattern", harness.tool_result_text(response))


class KindKeywordQuery(SearchFixture):
    def test_trait_keyword_resolves_the_trait_not_its_impls(self):
        payload = self.search([{"query": "trait Matcher"}])
        result = payload["results"][0]
        self.assertEqual(result["resolved_as"], "symbol", result)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["target"]["path"], "matcher.rs", result)
        self.assertIn("pub trait Matcher", result["core"])
        self.assertNotIn("preview", result)
        self.assertIn(
            {"query": "trait Matcher", "normalized_to": "Matcher", "stripped_keyword": "trait"},
            payload["diagnostics"]["normalizations"],
        )

    def test_bare_name_skips_trait_impls(self):
        result = self.search([{"query": "Matcher"}])["results"][0]
        self.assertEqual(result["resolved_as"], "symbol", result)
        self.assertEqual(result["target"]["path"], "matcher.rs", result)

    def test_impl_keyword_lists_the_impl_blocks_without_a_preview(self):
        result = self.search([{"query": "impl Matcher"}])["results"][0]
        self.assertEqual(result["resolved_as"], "ambiguous", result)
        self.assertEqual(result["status"], "ambiguous", result)
        self.assertEqual(result["completeness"], "complete", result)
        paths = sorted(candidate["path"] for candidate in result["candidates"])
        self.assertEqual(paths, sorted(f"{name.lower()}.rs" for name in IMPLEMENTORS))
        self.assertNotIn("preview", result)


if __name__ == "__main__":
    unittest.main()
