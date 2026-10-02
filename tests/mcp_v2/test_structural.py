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
        arguments = {"cwd": str(self.root), "queries": queries}
        if budget is not None:
            arguments["budget"] = budget
        response = harness.run_mcp([], [
            harness.initialize_request(),
            harness.tools_call_request(2, "tilth_search", arguments),
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

    @classmethod
    def span(cls, source, text):
        found = cls.location(source, text)
        return [found["start_line"], found["end_line"], found["start_byte"], found["end_byte"]]

    def many(self, files, per_file, name="wrap"):
        for path in files:
            self.write(path, f"{name}(value)\n" * per_file)

    @staticmethod
    def flat(result):
        return [(group["path"], match) for group in result["items"] for match in group["matches"]]

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
                self.assertEqual(result["view"], "matches")
                self.assertEqual(result["total_matches"], 1)
                self.assertEqual(result["files_matched"], 1)
                self.assertEqual(result["items"], [{
                    "path": path,
                    "matches": [self.span(source, match) + [{"A": [self.span(source, capture)]}]],
                }])

    def test_multicapture_and_no_match(self):
        source = "wrap(first, second)\n"
        self.write("source.py", source)
        result = self.result([{"pattern": "wrap($$$ARGS)", "language": "python"}])["results"][0]
        self.assertEqual(result["items"][0]["matches"][0][4]["ARGS"], [
            self.span(source, "first"), self.span(source, ","), self.span(source, "second"),
        ])
        self.write("plain.py", "plain(1)\n")
        plain = self.result([{"pattern": "plain(1)", "language": "python"}])["results"][0]
        self.assertEqual(len(plain["items"][0]["matches"][0]), 4)
        empty = self.result([{"pattern": "absent($A)", "language": "python"}])["results"][0]
        self.assertEqual(empty["status"], "no_match")
        self.assertEqual(empty["items"], [])
        self.assertEqual(empty["view"], "matches")
        self.assertEqual((empty["total_matches"], empty["files_matched"]), (0, 0))

    def test_invalid_requests_fail_without_fallback(self):
        invalid = [
            ({"pattern": "wrap($A)", "language": "go"}, "unsupported structural language"),
            ({"pattern": "wrap($A)"}, "language must be"),
            ({"pattern": "wrap($A)", "language": "tsx"}, "unsupported structural language"),
            ({"pattern": "wrap($A)", "language": "python", "query": "wrap"}, "exactly one of query, follow, or pattern"),
            ({"pattern": "wrap($A)", "language": "python", "follow": {}}, "exactly one of query, follow, or pattern"),
            ({"pattern": "wrap($A)", "language": "python", "unknown": True}, "accept only pattern, language, and glob"),
            ({"pattern": "", "language": "python"}, "invalid structural pattern"),
            ({"pattern": "$$$ARGS", "language": "python"}, "invalid structural pattern"),
            ({"pattern": "foo(); bar();", "language": "typescript"}, "Multiple AST nodes"),
            ({"pattern": "wrap(!!!)", "language": "python"}, "invalid structural pattern"),
            ({"pattern": "fn broken( {", "language": "rust"}, "invalid structural pattern"),
            ({"pattern": "wrap(!!!)", "language": "typescript"}, "invalid structural pattern"),
            ({"pattern": 7, "language": "python"}, "pattern must be a string"),
            ({"pattern": "wrap($A)", "language": "python", "glob": 7}, "glob must be a string"),
            ({"pattern": "wrap($A)", "language": "python", "glob": "["}, "invalid glob"),
        ]
        for entry, fragment in invalid:
            with self.subTest(entry=entry):
                response = self.call([entry])
                self.assertTrue(harness.tool_is_error(response), response)
                self.assertIn(fragment, harness.tool_result_text(response))

    def test_shared_walker_filters_and_typescript_not_tsx(self):
        for path in ("keep/source.ts", "other.ts", "ignored.ts", "node_modules/vendor.ts", "source.tsx"):
            self.write(path, "wrap(value);\n")
        self.write(".tilthignore", "ignored.ts\n")
        entry = {"pattern": "wrap($A)", "language": "typescript"}
        result = self.result([entry])["results"][0]
        self.assertEqual([item["path"] for item in result["items"]], ["keep/source.ts", "other.ts"])
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
        self.assertEqual(items[0]["matches"][0][4]["A"], [
            self.span(source, '"private-value-not-for-output"'),
        ])
        self.assertNotIn("private-value-not-for-output", json.dumps(payload))

    def test_exact_match_limit_boundary_is_complete(self):
        source = "".join(f"wrap(value_{index})\n" for index in range(1000))
        self.write("source.py", source)
        result = self.result(
            [{"pattern": "wrap($A)", "language": "python"}], budget=1000000,
        )["results"][0]
        self.assertEqual(len(self.flat(result)), 1000)
        self.assertEqual((result["total_matches"], result["files_matched"]), (1000, 1))
        self.assertNotIn("match_limited", result)
        self.assertNotIn("note", result)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["completeness"], "complete")
        self.assertEqual(result["items"][0]["matches"][-1], self.span(source, "wrap(value_999)") + [
            {"A": [self.span(source, "value_999")]},
        ])

    def test_match_limit_marks_scan_partial(self):
        self.write("source.py", "wrap(value)\n" * 1001)
        result = self.result(
            [{"pattern": "wrap($A)", "language": "python"}], budget=1000000,
        )["results"][0]
        self.assertEqual(len(self.flat(result)), 1000)
        self.assertEqual((result["total_matches"], result["files_matched"]), (1001, 1))
        self.assertEqual(result["note"], "Showing the first 1000 of 1001 matches. Narrow with glob.")
        self.assertEqual(result["view"], "matches")
        self.assertTrue(result["match_limited"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["completeness"], "partial")

    def test_single_file_budget_keeps_longest_fitting_prefix(self):
        self.write("source.py", "wrap(value)\n" * 100)
        entry = {"pattern": "wrap($A)", "language": "python"}
        result = self.result([entry], budget=200)["results"][0]
        self.assertEqual(result["completeness"], "partial")
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["budget_limited"])
        self.assertEqual(result["view"], "matches")
        shown = result["shown"]
        self.assertEqual(shown, 12)
        self.assertEqual(result["total_matches"], 100)
        self.assertEqual(
            result["note"],
            f"Showing the first {shown} of 100 matches in source.py. Raise budget to see more.",
        )
        expected = [
            [index + 1, index + 1, 12 * index, 12 * index + 11, {"A": [[index + 1, index + 1, 12 * index + 5, 12 * index + 10]]}]
            for index in range(shown)
        ]
        self.assertEqual(result["items"], [{"path": "source.py", "matches": expected}])
        self.assertTrue(harness.tool_is_error(self.call([entry], budget=1)))

    def test_default_budget_returns_tier_one_for_many_matches(self):
        self.many(["a/one.py", "a/two.py", "b/three.py"], 200)
        result = self.result([{"pattern": "wrap($A)", "language": "python"}], budget=None)["results"][0]
        self.assertEqual(result["view"], "matches")
        self.assertEqual((result["total_matches"], result["files_matched"]), (600, 3))
        self.assertEqual(len(self.flat(result)), 600)
        self.assertEqual(result["completeness"], "complete")
        self.assertNotIn("budget_limited", result)

    def test_ordering_is_path_then_byte_range(self):
        self.many(["z.py", "b/y.py", "a.py"], 3)
        result = self.result([{"pattern": "wrap($A)", "language": "python"}])["results"][0]
        self.assertEqual([g["path"] for g in result["items"]], ["a.py", "b/y.py", "z.py"])
        for group in result["items"]:
            starts = [m[2] for m in group["matches"]]
            self.assertEqual(starts, sorted(starts))

    def test_tier_two_lists_files_with_exact_counts_and_glob_round_trips(self):
        counts = {"pkg/a.py": 100, "pkg/b.py": 134, "pkg/c.py": 150, "d.py": 50}
        for path, count in counts.items():
            self.many([path], count)
        entry = {"pattern": "wrap($A)", "language": "python"}
        result = self.result([entry], budget=1000)["results"][0]
        self.assertEqual(result["view"], "files")
        self.assertNotIn("items", result)
        self.assertEqual(result["files"], [["d.py", 50], ["pkg/a.py", 100], ["pkg/b.py", 134], ["pkg/c.py", 150]])
        self.assertEqual((result["total_matches"], result["files_matched"]), (434, 4))
        self.assertEqual(result["note"], "Too many matches (434 in 4 files) for the budget. Rerun this entry with glob set to a listed path. Prefix a top-level file with / to match only that file.")
        self.assertEqual(result["completeness"], "partial")
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["budget_limited"])
        narrowed = self.result([{**entry, "glob": "pkg/b.py"}], budget=None)["results"][0]
        self.assertEqual([g["path"] for g in narrowed["items"]], ["pkg/b.py"])
        self.assertEqual(len(self.flat(narrowed)), 134)

    def test_tier_two_counts_include_matches_beyond_the_cap(self):
        self.many(["a.py"], 1000)
        self.many(["b.py"], 5)
        entry = {"pattern": "wrap($A)", "language": "python"}
        result = self.result([entry], budget=1000)["results"][0]
        self.assertEqual(result["view"], "files")
        self.assertEqual(result["files"], [["a.py", 1000], ["b.py", 5]])
        self.assertEqual((result["total_matches"], result["files_matched"]), (1005, 2))
        self.assertTrue(result["match_limited"])
        self.assertTrue(result["note"].startswith("Too many matches (1005 in 2 files)"))

    def test_tier_three_groups_directories_and_glob_round_trips(self):
        for directory in ("alpha", "beta", "gamma"):
            for index in range(20):
                self.many([f"{directory}/module_number_{index:02}.py"], 1 + index % 3)
        self.many(["root_file.py"], 4)
        entry = {"pattern": "wrap($A)", "language": "python"}
        result = self.result([entry], budget=400)["results"][0]
        per_dir = sum(1 + i % 3 for i in range(20))
        self.assertEqual(result["view"], "directories")
        self.assertNotIn("items", result)
        self.assertNotIn("files", result)
        self.assertEqual(result["directories"], [
            [".", 4, 1], ["alpha", per_dir, 20], ["beta", per_dir, 20], ["gamma", per_dir, 20],
        ])
        total = 3 * per_dir + 4
        self.assertEqual((result["total_matches"], result["files_matched"]), (total, 61))
        top = result["top_files"]
        self.assertEqual(len(top), 10)
        self.assertEqual(top[0], ["root_file.py", 4])
        self.assertEqual(top[1:], sorted(top[1:], key=lambda f: (-f[1], f[0])))
        self.assertTrue(all(count == 3 for _, count in top[1:]))
        self.assertEqual(top[1][0], "alpha/module_number_02.py")
        self.assertEqual(
            result["note"],
            f"Too many matches ({total} in 61 files across 4 directories) for the budget. "
            'Rerun this entry with glob set to a directory, e.g. "alpha/**".',
        )
        self.assertTrue(result["budget_limited"])
        self.assertEqual(result["completeness"], "partial")
        narrowed = self.result([{**entry, "glob": "beta/**"}], budget=None)["results"][0]
        self.assertEqual({g["path"].split("/")[0] for g in narrowed["items"]}, {"beta"})
        self.assertEqual(len(self.flat(narrowed)), per_dir)

    def test_tier_three_uses_smallest_depth_with_two_groups(self):
        for directory in ("x", "y"):
            for index in range(20):
                self.many([f"src/{directory}/module_number_{index:02}.py"], 1)
        result = self.result([{"pattern": "wrap($A)", "language": "python"}], budget=350)["results"][0]
        self.assertEqual(result["view"], "directories")
        self.assertEqual(result["directories"], [["src/x", 20, 20], ["src/y", 20, 20]])
        self.assertIn("across 2 directories", result["note"])
        self.assertTrue(result["note"].endswith('e.g. "src/x/**".'))

    def test_tier_two_note_omits_slash_hint_without_top_level_files(self):
        self.many(["pkg/a.py", "pkg/b.py"], 300)
        result = self.result([{"pattern": "wrap($A)", "language": "python"}], budget=1000)["results"][0]
        self.assertEqual(result["view"], "files")
        self.assertEqual(result["note"], "Too many matches (600 in 2 files) for the budget. Rerun this entry with glob set to a listed path.")

    def test_more_than_100_files_skip_the_file_list(self):
        for directory, count in (("a", 51), ("b", 50)):
            for index in range(count):
                self.many([f"{directory}/f{index:02}.py"], 1)
        entry = {"pattern": "wrap($A)", "language": "python"}
        result = self.result([entry], budget=1200)["results"][0]
        self.assertEqual(result["view"], "directories")
        self.assertEqual(result["directories"], [["a", 51, 51], ["b", 50, 50]])
        self.assertNotIn("files", result)
        for index in range(51):
            (self.root / f"b/f{index:02}.py").unlink(missing_ok=True)
        self.many(["b/only.py"], 1)
        self.assertEqual(self.result([entry], budget=1200)["results"][0]["view"], "matches")
        for index in range(50):
            self.many([f"b/f{index:02}.py"], 1)
        self.assertEqual(self.result([entry], budget=1200)["results"][0]["view"], "directories")

    def test_exactly_100_files_still_list(self):
        for directory in ("a", "b"):
            for index in range(50):
                self.many([f"{directory}/f{index:02}.py"], 1)
        result = self.result([{"pattern": "wrap($A)", "language": "python"}], budget=1200)["results"][0]
        self.assertEqual(result["view"], "files")
        self.assertEqual(result["files_matched"], 100)

    def test_tier_four_removes_listing_but_budget_one_still_errors(self):
        for directory in ("alpha", "beta"):
            for index in range(30):
                self.many([f"{directory}/module_number_{index:02}.py"], 2)
        entry = {"pattern": "wrap($A)", "language": "python"}
        result = self.result([entry], budget=150)["results"][0]
        for field in ("items", "files", "directories", "top_files"):
            self.assertNotIn(field, result)
        self.assertTrue(result["budget_limited"])
        self.assertEqual(result["completeness"], "partial")
        self.assertEqual(result["view"], "none")
        self.assertEqual(result["note"], "Too many matches (120 in 60 files) for the budget. Raise budget or narrow with glob.")
        self.assertTrue(harness.tool_is_error(self.call([entry], budget=1)))

    def test_unparsed_file_marks_scan_partial(self):
        self.write("large.py", "# large\n" * 70000 + "wrap(value)\n")
        for pattern in ("wrap($A)", "$F($A)"):  # literal and no-literal branches
            with self.subTest(pattern=pattern):
                result = self.result([{"pattern": pattern, "language": "python"}])["results"][0]
                self.assertEqual(result["completeness"], "partial")
                self.assertEqual(result["skipped_files"], 1)

    def test_file_at_size_limit_is_scanned(self):
        head = "wrap(value)\n#\n#\n"
        content = head + "#" * (500000 - len(head) - 1) + "\n"
        self.assertEqual(len(content.encode()), 500000)
        self.write("limit.py", content)
        result = self.result([{"pattern": "wrap($A)", "language": "python"}])["results"][0]
        self.assertEqual(result["completeness"], "complete")
        self.assertEqual(len(result["items"]), 1)
        self.assertNotIn("skipped_files", result)

    def test_listed_paths_narrow_exactly_with_root_files_needing_a_slash(self):
        self.many(["d.py", "sub/d.py", "sub/deep/e.py"], 2)
        entry = {"pattern": "wrap($A)", "language": "python"}
        def paths(glob):
            result = self.result([{**entry, "glob": glob}], budget=None)["results"][0]
            return [group["path"] for group in result["items"]]
        self.assertEqual(paths("sub/d.py"), ["sub/d.py"])
        self.assertEqual(paths("sub/deep/e.py"), ["sub/deep/e.py"])
        self.assertEqual(paths("/d.py"), ["d.py"])
        self.assertEqual(paths("d.py"), ["d.py", "sub/d.py"])

    @staticmethod
    def suggested_glob(note):
        import re
        found = re.search(r'e\.g\. "(.*?)"\.', note)
        assert found, note
        return found.group(1)

    def test_larger_budget_shows_a_longer_prefix(self):
        self.write("source.py", "wrap(value)\n" * 100)
        entry = {"pattern": "wrap($A)", "language": "python"}
        small = self.result([entry], budget=200)["results"][0]["shown"]
        large = self.result([entry], budget=400)["results"][0]["shown"]
        self.assertGreater(large, small)

    def test_directory_rows_match_their_suggested_globs(self):
        self.many([f"src/a{index:02}.py" for index in range(30)], 1)
        self.many([f"src/x/b{index:02}.py" for index in range(80)], 1)
        entry = {"pattern": "wrap($A)", "language": "python"}
        result = self.result([entry], budget=400)["results"][0]
        self.assertEqual(result["view"], "directories")
        self.assertEqual(result["directories"], [["src/*", 30, 30], ["src/x", 80, 80]])
        glob = self.suggested_glob(result["note"])
        self.assertEqual(glob, "src/*")
        narrowed = self.result([{**entry, "glob": glob}], budget=None)["results"][0]
        self.assertEqual(narrowed["total_matches"], 30)
        subtree = self.result([{**entry, "glob": "src/x/**"}], budget=None)["results"][0]
        self.assertEqual(subtree["total_matches"], 80)

    def test_bracket_directories_round_trip_through_suggested_globs(self):
        self.many([f"app/[id]/f{index:02}.py" for index in range(60)], 1)
        self.many([f"app/[slug]/g{index:02}.py" for index in range(60)], 1)
        entry = {"pattern": "wrap($A)", "language": "python"}
        result = self.result([entry], budget=400)["results"][0]
        self.assertEqual(result["view"], "directories")
        glob = self.suggested_glob(result["note"])
        self.assertEqual(glob, r"app/\[id\]/**")
        self.assertIn("Escape", result["note"])
        narrowed = self.result([{**entry, "glob": glob}], budget=None)["results"][0]
        self.assertEqual(narrowed["total_matches"], 60)
        self.assertEqual({g["path"].split("/")[1] for g in narrowed["items"]}, {"[id]"})

    def test_bracket_listed_paths_rerun_as_literal_files(self):
        self.many(["app/[id]/a.py", "app/[id]/b.py"], 300)
        entry = {"pattern": "wrap($A)", "language": "python"}
        result = self.result([entry], budget=1000)["results"][0]
        self.assertEqual(result["view"], "files")
        for path, count in result["files"]:
            narrowed = self.result([{**entry, "glob": path}], budget=None)["results"][0]
            self.assertEqual([g["path"] for g in narrowed["items"]], [path])
            self.assertEqual(narrowed["total_matches"], count)

    def test_root_only_matches_suggest_a_glob_that_matches(self):
        self.many([f"f{index:03}.py" for index in range(101)], 1)
        entry = {"pattern": "wrap($A)", "language": "python"}
        result = self.result([entry], budget=400)["results"][0]
        self.assertEqual(result["view"], "directories")
        self.assertEqual(result["directories"], [[".", 101, 101]])
        glob = self.suggested_glob(result["note"])
        self.assertEqual(glob, "/f000.py")
        narrowed = self.result([{**entry, "glob": glob}], budget=None)["results"][0]
        self.assertNotEqual(narrowed["status"], "no_match")
        self.assertEqual([g["path"] for g in narrowed["items"]], ["f000.py"])

    def test_single_directory_note_is_singular(self):
        self.many([f"only/f{index:03}.py" for index in range(101)], 1)
        result = self.result([{"pattern": "wrap($A)", "language": "python"}], budget=400)["results"][0]
        self.assertEqual(result["view"], "directories")
        self.assertIn("across 1 directory)", result["note"])

    def test_tight_batch_budget_keeps_a_structural_tier(self):
        self.many([f"d/f{index:02}.py" for index in range(30)], 20)
        entries = [
            {"query": "wrap(value)"},
            {"pattern": "wrap($A)", "language": "python"},
        ]
        results = self.result(entries, budget=700)["results"]
        self.assertEqual(len(results), 2)
        self.assertIn(results[1]["view"], ("files", "directories"))

    def test_two_structural_entries_renarrow_against_the_final_length(self):
        self.many([f"a/f{index:02}.py" for index in range(40)], 20)
        self.many([f"b/f{index:02}.py" for index in range(30)], 20, name="other")
        entries = [
            {"pattern": "wrap($A)", "language": "python"},
            {"pattern": "other($A)", "language": "python"},
        ]
        results = self.result(entries, budget=400)["results"]
        self.assertEqual(len(results), 2)
        self.assertIn(results[0]["view"], ("files", "directories"))
        self.assertTrue(results[0][results[0]["view"]])

    def test_mixed_batch_preserves_entry_order(self):
        self.write("source.py", "wrap(value)\n")
        entries = [{"pattern": "wrap($A)", "language": "python"}, {"query": "absent_literal_phrase"},
                   {"pattern": "wrap($A)", "language": "python"}]
        results = self.result(entries)["results"]
        self.assertEqual([r["resolved_as"] for r in results], ["structural", "miss", "structural"])
        self.assertEqual(results[0], results[2])


if __name__ == "__main__":
    unittest.main()
