"""Search resolves pasted Go signatures and returns whole constructs.

Each test replays a retry chain from the call-gap diagnosis: a search that
misses or truncates forces a dependent follow-up turn.
"""
import json
import re
import tempfile
import unittest
from pathlib import Path

import harness


def setUpModule():
    harness.build_if_needed()


GO_SOURCE = """package caddy

type MatchExpression struct{}

func (m *MatchExpression) UnmarshalCaddyfile(d *Dispenser) error {
	return nil
}

func Standalone(a int) int {
	return a
}
"""


class SearchFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.cwd = str(self.root)

    def search(self, queries, budget=None):
        args = {"cwd": self.cwd, "queries": queries}
        if budget is not None:
            args["budget"] = budget
        request = harness.tools_call_request(2, "tilth_search", args)
        run = harness.run_mcp([], [harness.initialize_request(), request])
        return json.loads(harness.tool_result_text(run.response_by_id(2)))


class GoSignatureQuery(SearchFixture):
    def setUp(self):
        super().setUp()
        (self.root / "matcher.go").write_text(GO_SOURCE)

    def assert_method_found(self, query):
        payload = self.search([{"query": query}])
        result = payload["results"][0]
        self.assertEqual(result["resolved_as"], "symbol", result)
        self.assertIn("UnmarshalCaddyfile(d *Dispenser) error", result["core"])
        normalizations = payload["diagnostics"]["normalizations"]
        self.assertTrue(
            any(n["query"] == query and n["normalized_to"] == "UnmarshalCaddyfile"
                for n in normalizations),
            normalizations,
        )

    def test_receiver_without_name_finds_the_method(self):
        self.assert_method_found("func (MatchExpression) UnmarshalCaddyfile")

    def test_named_pointer_receiver_with_params_finds_the_method(self):
        self.assert_method_found("func (m *MatchExpression) UnmarshalCaddyfile(d *Dispenser) error")

    def test_plain_function_signature_finds_the_function(self):
        result = self.search([{"query": "func Standalone(a int) int"}])["results"][0]
        self.assertEqual(result["resolved_as"], "symbol", result)
        self.assertIn("return a", result["core"])

    def test_intentional_regex_stays_a_regex(self):
        payload = self.search([{"query": r"func \(\w+ \*MatchExpression\) Unmarshal\w+"}])
        result = payload["results"][0]
        self.assertEqual(result["resolved_as"], "regex", result)
        self.assertEqual(payload["diagnostics"].get("normalizations", []), [])


def long_function(name, body_lines):
    body = "".join(f"    step_{i} = compute({i}, 'padding padding')\n" for i in range(body_lines))
    return f"def {name}():\n{body}    return step_last\n"


class UniqueHitConstruct(SearchFixture):
    def test_construct_longer_than_sixty_lines_returns_whole(self):
        (self.root / "long.py").write_text(long_function("big_routine", 100))
        result = self.search([{"query": "big_routine"}])["results"][0]
        self.assertEqual(result["resolved_as"], "symbol", result)
        self.assertIn("return step_last", result["core"])
        self.assertEqual(result["completeness"], "complete", result)

    def test_construct_over_budget_names_end_line_and_exact_continuation(self):
        (self.root / "long.py").write_text(long_function("big_routine", 400))
        result = self.search([{"query": "big_routine"}], budget=1500)["results"][0]
        self.assertEqual(result["completeness"], "partial", result)
        self.assertNotIn("return step_last", result["core"])
        shown = len(result["core"].splitlines())
        self.assertEqual(result["core_end"], 402)
        self.assertEqual(result["continue"], f"long.py#{shown + 1}-402")


class ContentHitCap(SearchFixture):
    def preview_files(self, budget):
        for index in range(45):
            (self.root / f"mod_{index:02}.py").write_text(f"value = 1  # cap_probe {index}\n")
        result = self.search([{"query": "cap_probe"}], budget=budget)["results"][0]
        self.assertEqual(result["total_found"], 45, result)
        return set(re.findall(r"mod_\d\d\.py", result["preview"]))

    def test_cap_scales_with_budget(self):
        small = self.preview_files(2000)
        self.assertEqual(len(small), 10, small)
        for path in self.root.glob("mod_*.py"):
            path.unlink()
        large = self.preview_files(20000)
        self.assertEqual(len(large), 45, large)


class TestFacetEditable(SearchFixture):
    def setUp(self):
        super().setUp()
        (self.root / "tests").mkdir()
        for name in ("alpha", "beta", "gamma", "delta"):
            (self.root / "tests" / f"test_{name}.py").write_text(
                "def test_it():\n    tagged_marker(1)\n    return unrelated_tail\n")
        for name in ("one", "two", "three"):
            (self.root / f"src_{name}.py").write_text(f"def run_{name}():\n    tagged_marker(2)\n")

    def search_preview(self):
        result = self.search([{"query": "tagged_marker"}])["results"][0]
        self.assertIn("## Tests (4)", result["preview"])
        return result["preview"]

    def write(self, edit):
        request = harness.tools_call_request(3, "tilth_write", {"cwd": self.cwd, "edits": [edit]})
        run = harness.run_mcp([], [
            harness.initialize_request(),
            harness.tools_call_request(
                2, "tilth_search", {"cwd": self.cwd, "queries": [{"query": "tagged_marker"}]}),
            request,
        ])
        return run.response_by_id(3)

    def test_test_hit_carries_tag_and_original_whitespace(self):
        preview = self.search_preview()
        self.assertRegex(preview, r"tests/test_alpha\.py#[0-9A-F]{4}")
        self.assertIn("2:    tagged_marker(1)", preview)

    def test_test_hit_edits_without_a_read_and_unshown_line_is_rejected(self):
        preview = self.search_preview()
        tag = re.search(r"tests/test_alpha\.py#([0-9A-F]{4})", preview).group(1)
        target = self.root / "tests" / "test_alpha.py"
        original = target.read_text()
        rejected = self.write({
            "path": "tests/test_alpha.py", "tag": tag,
            "ops": [{"op": "replace_text", "old": "unrelated_tail", "new": "x"}]})
        self.assertTrue(harness.tool_is_error(rejected), harness.tool_result_text(rejected))
        self.assertEqual(target.read_text(), original)
        written = self.write({
            "path": "tests/test_alpha.py", "tag": tag,
            "ops": [{"op": "replace_text", "old": "tagged_marker(1)", "new": "tagged_marker(9)"}]})
        self.assertFalse(harness.tool_is_error(written), harness.tool_result_text(written))
        self.assertEqual(target.read_text(), original.replace("marker(1)", "marker(9)"))


if __name__ == "__main__":
    unittest.main()
