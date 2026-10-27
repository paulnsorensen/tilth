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


class GoReceiverScope(SearchFixture):
    def setUp(self):
        super().setUp()
        for index, receiver in enumerate(("MatchExpression", "MatchHost", "MatchPath")):
            (self.root / f"match_{index}.go").write_text(
                f"package caddy\n\ntype {receiver} struct{{}}\n\n"
                f"func (m *{receiver}) UnmarshalCaddyfile(d *Dispenser) error {{\n"
                f"\treturn nil // {receiver}\n}}\n")

    def test_receiver_selects_its_own_method(self):
        query = "func (m *MatchHost) UnmarshalCaddyfile(d *Dispenser) error {"
        result = self.search([{"query": query}])["results"][0]
        self.assertEqual(result["resolved_as"], "symbol", result)
        self.assertIn("return nil // MatchHost", result["core"])

    def test_unknown_receiver_searches_the_signature_literally(self):
        query = "func (m *MatchNothing) UnmarshalCaddyfile(d *Dispenser) error {"
        result = self.search([{"query": query}])["results"][0]
        self.assertEqual(result["resolved_as"], "miss", result)

    def test_receiverless_method_name_stays_ambiguous(self):
        result = self.search([{"query": "func UnmarshalCaddyfile"}])["results"][0]
        self.assertEqual(result["resolved_as"], "ambiguous", result)


class ContainerHitIsNotReturnedWhole(SearchFixture):
    def test_large_class_is_cut_with_a_continuation(self):
        methods = "".join(
            f"    def method_{i}(self):\n        return compute({i}, 'padding padding')\n\n"
            for i in range(450))
        (self.root / "big.py").write_text(f"class BigClass:\n{methods}")
        result = self.search([{"query": "BigClass"}])["results"][0]
        self.assertEqual(result["resolved_as"], "symbol", result)
        shown = len(result["core"].splitlines())
        self.assertLessEqual(shown, 60, shown)
        self.assertEqual(result["completeness"], "partial", result)
        self.assertEqual(result["continue"], f"big.py#{shown + 1}-{result['core_end']}")
        self.assertGreater(result["core_end"], 1000, result)


class CandidateFollowBeyondTen(SearchFixture):
    def test_fifteenth_candidate_follows_without_glob(self):
        for index in range(30):
            (self.root / f"r{index:02}.go").write_text(
                f"package p{index}\n\nfunc Render() int {{\n\treturn {index}\n}}\n")
        result = self.search([{"query": "Render"}], budget=24000)["results"][0]
        self.assertEqual(result["resolved_as"], "ambiguous", result)
        candidates = result["candidates"]
        self.assertEqual(len(candidates), 30)
        candidate = candidates[14]
        hint = {"kind": "fetch_callers", "target": {
            "path": candidate["path"], "scope": ".", "line": candidate["line"],
            "name": candidate["def_name"], "occurrence": candidate["occurrence"]}}
        request = harness.tools_call_request(
            2, "tilth_search", {"cwd": self.cwd, "queries": [{"follow": hint}]})
        run = harness.run_mcp([], [harness.initialize_request(), request])
        response = run.response_by_id(2)
        self.assertFalse(harness.tool_is_error(response), harness.tool_result_text(response))
        followed = json.loads(harness.tool_result_text(response))["results"][0]
        self.assertEqual(followed["resolved_as"], "fetch_callers", followed)
        self.assertEqual(followed["target"]["path"], candidate["path"], followed)
        self.assertEqual(followed["status"], "no_match", followed)


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


class GoReceiverBeyondHitCap(SearchFixture):
    def setUp(self):
        super().setUp()
        for index in range(70):
            (self.root / f"m{index:02}.go").write_text(
                f"package caddy\n\ntype T{index:02} struct{{}}\n\n"
                f"func (m *T{index:02}) UnmarshalCaddyfile(d *D) error {{\n"
                f"\treturn nil // T{index:02}\n}}\n")

    def test_receiver_past_the_definition_cap_resolves(self):
        for query in ("func (T65) UnmarshalCaddyfile",
                      "func (m *T65) UnmarshalCaddyfile(d *D) error {"):
            result = self.search([{"query": query}])["results"][0]
            self.assertEqual(result["resolved_as"], "symbol", result)
            self.assertIn("return nil // T65", result["core"])

    def test_unknown_receiver_is_a_complete_miss_only_after_a_full_scan(self):
        result = self.search([{"query": "func (T99) UnmarshalCaddyfile"}])["results"][0]
        self.assertEqual(result["resolved_as"], "miss", result)


class GoReceiverStaysAmbiguous(SearchFixture):
    def test_two_methods_on_same_receiver_name_are_returned_as_candidates(self):
        for directory in ("a", "b"):
            (self.root / directory).mkdir()
            (self.root / directory / "h.go").write_text(
                f"package {directory}\n\ntype Handler struct{{}}\n\n"
                "func (h *Handler) ServeHTTP(w int) {\n\t_ = w\n}\n")
        result = self.search([{"query": "func (Handler) ServeHTTP"}])["results"][0]
        self.assertEqual(result["resolved_as"], "ambiguous", result)
        self.assertEqual(len(result["candidates"]), 2, result)


class GoGenericReceiver(SearchFixture):
    def setUp(self):
        super().setUp()
        (self.root / "g.go").write_text(
            "package g\n\ntype Set[T comparable] struct{}\n\n"
            "func (s *Set[T]) Add(v T) {\n\t_ = v // set\n}\n\n"
            "type Map[K comparable, V any] struct{}\n\n"
            "func (m *Map[K, V]) Add(k K, v V) {\n\t_ = k // map\n}\n")

    def test_map_receiver_with_and_without_type_params_resolves(self):
        for query in ("func (m *Map) Add", "func (m *Map[K, V]) Add(k K, v V)"):
            result = self.search([{"query": query}])["results"][0]
            self.assertEqual(result["resolved_as"], "symbol", result)
            self.assertIn("// map", result["core"])


class GoSignatureShapes(SearchFixture):
    def setUp(self):
        super().setUp()
        (self.root / "s.go").write_text(
            "package s\n\nfunc F(a interface{ M() }) int {\n\treturn 1 // plain\n}\n\n"
            "func Map[S ~[]E, E any](s S) int {\n\treturn 2 // generic\n}\n")

    def test_interface_literal_param_resolves(self):
        result = self.search([{"query": "func F(a interface{ M() }) int"}])["results"][0]
        self.assertEqual(result["resolved_as"], "symbol", result)
        self.assertIn("// plain", result["core"])

    def test_nested_bracket_type_params_resolve(self):
        result = self.search([{"query": "func Map[S ~[]E, E any](s S) int"}])["results"][0]
        self.assertEqual(result["resolved_as"], "symbol", result)
        self.assertIn("// generic", result["core"])

    def test_regex_shapes_stay_regex(self):
        for query in ("func Get(User|Order)", "func New[A-Z]"):
            result = self.search([{"query": query}])["results"][0]
            self.assertEqual(result["resolved_as"], "regex", result)


class GoSignatureFormsStayStructural(SearchFixture):
    def setUp(self):
        super().setUp()
        (self.root / "f.go").write_text(
            "package f\n\n"
            "func Sum[T int | float64](xs []T) T {\n\treturn xs[0] // union\n}\n\n"
            "func Pair[K, V any](k K, v V) int {\n\treturn 0 // grouped\n}\n\n"
            "func (c *Client) Do(opts ...*Option) error {\n\treturn nil // variadic\n}\n")

    def test_union_grouped_and_variadic_forms_resolve(self):
        cases = (
            ("func Sum[T int | float64](xs []T) T", "// union"),
            ("func Pair[K, V any](k K, v V)", "// grouped"),
            ("func (c *Client) Do(opts ...*Option)", "// variadic"),
        )
        for query, marker in cases:
            result = self.search([{"query": query}])["results"][0]
            self.assertEqual(result["resolved_as"], "symbol", result)
            self.assertIn(marker, result["core"])

    def test_pipe_in_params_and_class_in_type_params_stay_regex(self):
        for query in ("func Get(User|Order)", "func New[A-Z]", "func Do(a, b .*)"):
            result = self.search([{"query": query}])["results"][0]
            self.assertEqual(result["resolved_as"], "regex", result)


class GoReceiverManyDefinitions(SearchFixture):
    def write_methods(self, receiver, count, prefix):
        for index in range(count):
            (self.root / f"{prefix}{index:03}.go").write_text(
                f"package p{index}\n\ntype {receiver} struct{{}}\n\n"
                f"func (o *{receiver}) Validate() error {{\n\treturn nil\n}}\n")

    def test_receiver_candidates_are_capped_and_partial(self):
        self.write_methods("Options", 200, "opt")
        result = self.search([{"query": "func (o *Options) Validate"}], budget=3000)["results"][0]
        self.assertEqual(result["resolved_as"], "ambiguous", result)
        self.assertEqual(len(result["candidates"]), 10, result)
        self.assertEqual(result["completeness"], "partial", result)

    def test_methods_on_other_receivers_do_not_crowd_out_the_scoped_ones(self):
        for index in range(150):
            self.write_methods(f"Other{index:03}", 1, f"other{index:03}_")
        self.write_methods("Options", 3, "opt")
        result = self.search([{"query": "func (o *Options) Validate"}], budget=3000)["results"][0]
        self.assertEqual(result["resolved_as"], "ambiguous", result)
        self.assertEqual(len(result["candidates"]), 3, result)
        self.assertEqual(result["completeness"], "complete", result)
        for candidate in result["candidates"]:
            self.assertTrue(candidate["path"].startswith("opt"), candidate)

    def test_all_receiver_candidates_within_the_cap_are_complete(self):
        self.write_methods("Options", 80, "opt")
        calls = "".join(f"\tx{i}.Validate()\n" for i in range(150))
        (self.root / "calls.go").write_text(f"package calls\n\nfunc run() {{\n{calls}}}\n")
        result = self.search([{"query": "func (o *Options) Validate"}], budget=40000)["results"][0]
        self.assertEqual(result["resolved_as"], "ambiguous", result)
        self.assertEqual(len(result["candidates"]), 80, result)
        self.assertEqual(result["completeness"], "complete", result)


class GoReceiverMissKeepsNormalization(SearchFixture):
    def test_literal_and_miss_paths_report_the_rewrite(self):
        (self.root / "m.go").write_text(
            "package m\n\nfunc (m *Real) Run() {\n\t_ = 1\n}\n")
        query = "func (x *Nothing) Run()"
        payload = self.search([{"query": query}])
        self.assertEqual(payload["results"][0]["resolved_as"], "miss", payload)
        normalizations = payload["diagnostics"]["normalizations"]
        self.assertTrue(any(n["query"] == query for n in normalizations), normalizations)


class SameNameDefinitionsInOneFile(SearchFixture):
    def test_every_occurrence_in_one_file_follows(self):
        source = "package p\n\n" + "".join(
            f"type T{i:02} struct{{}}\n\nfunc (T{i:02}) String() string {{\n\treturn \"{i}\"\n}}\n\n"
            for i in range(15))
        (self.root / "s.go").write_text(source)
        result = self.search([{"query": "String"}], budget=24000)["results"][0]
        candidates = result["candidates"]
        self.assertEqual(len(candidates), 15, result)
        for index in (10, 12, 14):
            candidate = candidates[index]
            hint = {"kind": "fetch_callers", "target": {
                "path": candidate["path"], "scope": ".", "line": candidate["line"],
                "name": candidate["def_name"], "occurrence": candidate["occurrence"]}}
            request = harness.tools_call_request(
                2, "tilth_search", {"cwd": self.cwd, "queries": [{"follow": hint}]})
            run = harness.run_mcp([], [harness.initialize_request(), request])
            response = run.response_by_id(2)
            self.assertFalse(harness.tool_is_error(response), harness.tool_result_text(response))
            followed = json.loads(harness.tool_result_text(response))["results"][0]
            self.assertEqual(followed["resolved_as"], "fetch_callers", followed)
            self.assertEqual(followed["target"]["line"], candidate["line"], followed)


if __name__ == "__main__":
    unittest.main()
