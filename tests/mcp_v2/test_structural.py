"""Structural search contracts through the real MCP server."""
import json
import tempfile
import unittest
from pathlib import Path

import harness


def setUpModule():
    harness.build_if_needed()


class StructuralSearch(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def write(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return target

    def call(self, queries, budget=10000):
        response = harness.run_mcp([], [
            harness.initialize_request(),
            harness.tools_call_request(2, "tilth_search", {
                "cwd": str(self.root), "queries": queries, "budget": budget,
            }),
        ]).response_by_id(2)
        self.assertIsNotNone(response)
        return response

    def result(self, queries, budget=10000):
        response = self.call(queries, budget)
        self.assertFalse(harness.tool_is_error(response), harness.tool_result_text(response))
        return json.loads(harness.tool_result_text(response))

    @staticmethod
    def location(source, text):
        start = source.encode().index(text.encode())
        end = start + len(text.encode())
        return {
            "start_byte": start, "end_byte": end,
            "start_line": source.encode()[:start].count(b"\n") + 1,
            "end_line": source.encode()[:end].count(b"\n") + 1,
        }

    def test_exact_multiline_utf8_ranges_and_captures(self):
        fixtures = [
            ("rust", "source.rs", '// é\nfn main() {\n    Some(\n        café\n    );\n    // Some(other)\n    let text = "Some(other)";\n}\n', "Some($A)", "Some(\n        café\n    )", "café"),
            ("typescript", "source.ts", '// é\nconst answer = wrap(\n    café\n);\n// wrap(other)\nconst text = "wrap(other)";\n', "wrap($A)", "wrap(\n    café\n)", "café"),
            ("python", "source.py", '# é\nanswer = wrap(\n    café\n)\n# wrap(other)\ntext = "wrap(other)"\n', "wrap($A)", "wrap(\n    café\n)", "café"),
        ]
        for language, path, source, pattern, match, capture in fixtures:
            with self.subTest(language=language):
                self.write(path, source)
                payload = self.result([{"pattern": pattern, "language": language}])
                self.assertEqual(set(payload), {"results", "hints", "diagnostics"})
                result = payload["results"][0]
                self.assertEqual(result["resolved_as"], "structural")
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["completeness"], "complete")
                self.assertEqual(result["items"], [{
                    "path": path, "range": self.location(source, match),
                    "captures": {"A": [self.location(source, capture)]},
                }])

    def test_multicapture_and_no_match(self):
        source = "wrap(first, second)\n"
        self.write("source.py", source)
        result = self.result([{"pattern": "wrap($$$ARGS)", "language": "python"}])["results"][0]
        self.assertEqual(result["items"][0]["captures"]["ARGS"], [
            self.location(source, "first"), self.location(source, ","), self.location(source, "second"),
        ])
        empty = self.result([{"pattern": "absent($A)", "language": "python"}])["results"][0]
        self.assertEqual(empty["status"], "no_match")
        self.assertEqual(empty["items"], [])

    def test_invalid_requests_fail_without_fallback(self):
        invalid = [
            {"pattern": "wrap($A)", "language": "go"},
            {"pattern": "wrap($A)"},
            {"pattern": "wrap($A)", "language": "tsx"},
            {"pattern": "wrap($A)", "language": "python", "query": "wrap"},
            {"pattern": "wrap($A)", "language": "python", "follow": {}},
            {"pattern": "wrap($A)", "language": "python", "unknown": True},
            {"pattern": "", "language": "python"},
            {"pattern": "$$$ARGS", "language": "python"},
            {"pattern": "foo(); bar();", "language": "typescript"},
            {"pattern": "wrap(!!!)", "language": "python"},
            {"pattern": "fn broken( {", "language": "rust"},
            {"pattern": "wrap(!!!)", "language": "typescript"},
            {"pattern": 7, "language": "python"},
            {"pattern": "wrap($A)", "language": "python", "glob": 7},
        ]
        for entry in invalid:
            with self.subTest(entry=entry):
                response = self.call([entry])
                self.assertTrue(harness.tool_is_error(response), response)
                self.assertTrue(harness.tool_result_text(response))

    def test_shared_walker_filters_and_typescript_not_tsx(self):
        for path in ("keep/source.ts", "other.ts", "ignored.ts", "node_modules/vendor.ts", "source.tsx"):
            self.write(path, "wrap(value);\n")
        self.write(".tilthignore", "ignored.ts\n")
        entry = {"pattern": "wrap($A)", "language": "typescript"}
        result = self.result([entry])["results"][0]
        self.assertEqual({item["path"] for item in result["items"]}, {"keep/source.ts", "other.ts"})
        result = self.result([{**entry, "glob": "keep/*.ts"}])["results"][0]
        self.assertEqual([item["path"] for item in result["items"]], ["keep/source.ts"])

    def test_cwd_scope_excludes_sibling_files(self):
        self.write("inside/source.py", "wrap(value)\n")
        self.write("outside.py", "wrap(value)\n")
        self.root = self.root / "inside"
        result = self.result([{"pattern": "wrap($A)", "language": "python"}])["results"][0]
        self.assertEqual([item["path"] for item in result["items"]], ["source.py"])

    def test_secret_paths_and_source_text_are_not_exposed(self):
        source = 'wrap("private-value-not-for-output")\n'
        self.write("credentials.py", source)
        self.write("source.py", source)
        payload = self.result([{"pattern": "wrap($A)", "language": "python"}])
        items = payload["results"][0]["items"]
        self.assertEqual([item["path"] for item in items], ["source.py"])
        self.assertEqual(items[0]["captures"]["A"], [
            self.location(source, '"private-value-not-for-output"'),
        ])
        self.assertNotIn("private-value-not-for-output", json.dumps(payload))

    def test_exact_match_limit_boundary_is_complete(self):
        source = "".join(f"wrap(value_{index})\n" for index in range(1000))
        self.write("source.py", source)
        result = self.result(
            [{"pattern": "wrap($A)", "language": "python"}], budget=1000000,
        )["results"][0]
        self.assertEqual(len(result["items"]), 1000)
        self.assertNotIn("match_limited", result)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["completeness"], "complete")
        self.assertEqual(result["items"][-1], {
            "path": "source.py",
            "range": self.location(source, "wrap(value_999)"),
            "captures": {"A": [self.location(source, "value_999")]},
        })

    def test_match_limit_marks_scan_partial(self):
        self.write("source.py", "wrap(value)\n" * 1001)
        result = self.result(
            [{"pattern": "wrap($A)", "language": "python"}], budget=1000000,
        )["results"][0]
        self.assertEqual(len(result["items"]), 1000)
        self.assertTrue(result["match_limited"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["completeness"], "partial")

    def test_budget_trims_items_honestly(self):
        self.write("source.py", "wrap(value)\n" * 100)
        entry = {"pattern": "wrap($A)", "language": "python"}
        payload = self.result([entry], budget=200)
        result = payload["results"][0]
        self.assertEqual(result["completeness"], "partial")
        self.assertTrue(result["budget_limited"])
        self.assertNotIn("items", result)
        self.assertTrue(harness.tool_is_error(self.call([entry], budget=1)))

    def test_unparsed_file_marks_scan_partial(self):
        self.write("large.py", "# large\n" * 70000 + "wrap(value)\n")
        result = self.result([{"pattern": "wrap($A)", "language": "python"}])["results"][0]
        self.assertEqual(result["completeness"], "partial")
        self.assertEqual(result["skipped_files"], 1)

    def test_mixed_batch_preserves_entry_order(self):
        self.write("source.py", "wrap(value)\n")
        entries = [{"pattern": "wrap($A)", "language": "python"}, {"query": "absent_literal_phrase"},
                   {"pattern": "wrap($A)", "language": "python"}]
        results = self.result(entries)["results"]
        self.assertEqual([r["resolved_as"] for r in results], ["structural", "miss", "structural"])
        self.assertEqual(results[0], results[2])


if __name__ == "__main__":
    unittest.main()
