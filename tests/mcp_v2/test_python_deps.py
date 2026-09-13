"""Direct Python import dependencies across src-layout packages (#197 part A).

Both dependency engines must report import-only consumers of a producer file:
`tilth_deps` (the legacy analyzer) and `fetch_dependencies` (the persistent
index behind a real search continuation). Neither engine may borrow the other's
result as its oracle, so each is exercised through its own MCP surface.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import harness


def _init_repo(root: Path):
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)


def _write(root: Path, rel: str, body: str):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def _producer_consumer(root: Path):
    """The confirmed four-file fixture from the task brief."""
    _write(root, "packages/producer/src/producer/__init__.py", "")
    _write(root, "packages/producer/src/producer/ingest/rankings.py",
           "class RankingEntry:\n    pass\n")
    _write(root, "packages/producer/src/producer/ingest/__init__.py",
           "from .rankings import RankingEntry\n")
    _write(root, "consumer/src/consumer/__init__.py", "")
    _write(root, "consumer/src/consumer/direct.py",
           "from producer.ingest.rankings import RankingEntry\n")
    _write(root, "consumer/src/consumer/surface.py",
           "from producer.ingest import RankingEntry\n")


class PythonDeps(unittest.TestCase):
    def setUp(self):
        harness.build_if_needed()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path(self.tmp.name)
        _init_repo(self.cwd)
        self.env = dict(os.environ, XDG_CACHE_HOME=str(self.cwd / "cache"))

    # ---- tilth_deps (legacy analyzer) -------------------------------------

    def deps(self, target_rel: str, scope: str | None = None):
        args = {"cwd": str(self.cwd), "path": str(self.cwd / target_rel)}
        if scope is not None:
            args["scope"] = str(self.cwd / scope)
        run = harness.run_mcp([], [
            harness.initialize_request(1),
            harness.tools_call_request(2, "tilth_deps", args),
        ], env=self.env)
        return run.response_by_id(2)

    def deps_dependents(self, response) -> set[str]:
        """Parse the 'Used by' path set from a tilth_deps report."""
        self.assertFalse(harness.tool_is_error(response), response)
        text = harness.tool_result_text(response)
        seen = set()
        section = False
        for line in text.splitlines():
            if line.startswith("## Used by"):
                section = True
                continue
            if line.startswith("## "):
                section = False
                continue
            if section and ":" in line:
                seen.add(line.split(":", 1)[0].strip())
        return seen

    def test_deps_reports_direct_and_reexport_importers(self):
        _producer_consumer(self.cwd)
        got = self.deps_dependents(
            self.deps("packages/producer/src/producer/ingest/rankings.py"))
        self.assertEqual(got, {
            "consumer/src/consumer/direct.py",
            "packages/producer/src/producer/ingest/__init__.py",
        })

    def test_deps_reports_barrel_importer(self):
        _producer_consumer(self.cwd)
        got = self.deps_dependents(
            self.deps("packages/producer/src/producer/ingest/__init__.py"))
        self.assertIn("consumer/src/consumer/surface.py", got)

    def test_deps_aliases_and_parenthesized_and_same_name_exclusion(self):
        _producer_consumer(self.cwd)
        _write(self.cwd, "consumer/src/consumer/aliased.py",
               "import producer.ingest.rankings as r\n")
        _write(self.cwd, "consumer/src/consumer/parened.py",
               "from producer.ingest.rankings import (\n    RankingEntry,\n)\n")
        # A different package that happens to export the same symbol name must
        # NOT count as a dependent of producer's rankings.py.
        _write(self.cwd, "packages/other/src/other/rankings.py",
               "class RankingEntry:\n    pass\n")
        _write(self.cwd, "consumer/src/consumer/unrelated.py",
               "from other.rankings import RankingEntry\n")
        got = self.deps_dependents(
            self.deps("packages/producer/src/producer/ingest/rankings.py"))
        self.assertIn("consumer/src/consumer/aliased.py", got)
        self.assertIn("consumer/src/consumer/parened.py", got)
        self.assertNotIn("consumer/src/consumer/unrelated.py", got)

    def test_deps_narrower_scope_excludes_outside_consumers(self):
        _producer_consumer(self.cwd)
        got = self.deps_dependents(self.deps(
            "packages/producer/src/producer/ingest/rankings.py",
            scope="packages/producer"))
        # Consumers outside the narrower scope are not discovered; paths in the
        # report are relative to the requested scope.
        self.assertNotIn("consumer/src/consumer/direct.py", got)
        self.assertFalse(any("consumer" in d for d in got), got)
        self.assertIn("src/producer/ingest/__init__.py", got)

    def test_deps_ambiguous_identity_errors_with_candidates(self):
        _producer_consumer(self.cwd)
        # Duplicate package root: a second producer/ingest/rankings.py under
        # <scope>/src makes the module name resolve to two files.
        _write(self.cwd, "src/producer/ingest/rankings.py",
               "class RankingEntry:\n    pass\n")
        response = self.deps(
            "packages/producer/src/producer/ingest/rankings.py")
        self.assertTrue(harness.tool_is_error(response), response)
        text = harness.tool_result_text(response)
        self.assertIn("producer.ingest.rankings", text)
        self.assertIn("src/producer/ingest/rankings.py", text)

    # ---- fetch_dependencies (persistent index via search continuation) ----

    def dep_hint(self, target_rel: str):
        """A real file-level fetch_dependencies hint for `target_rel`.

        A whole-path query yields a file (line: null) result, whose only
        continuation is fetch_dependencies — the file target we need, and one
        that survives an ambiguous symbol name.
        """
        run = harness.run_mcp([], [
            harness.initialize_request(1),
            harness.tools_call_request(2, "tilth_search", {
                "cwd": str(self.cwd), "queries": [{"query": target_rel}],
            }),
        ], env=self.env)
        payload = json.loads(harness.tool_result_text(run.response_by_id(2)))
        for hint in payload.get("hints", []):
            if hint["kind"] == "fetch_dependencies" \
                    and hint["target"]["path"] == target_rel:
                return hint
        self.fail(f"no fetch_dependencies hint for {target_rel}: {payload.get('hints')}")

    def follow(self, hint):
        run = harness.run_mcp([], [
            harness.initialize_request(1),
            harness.tools_call_request(2, "tilth_search", {
                "cwd": str(self.cwd), "queries": [{"follow": hint}],
            }),
        ], env=self.env)
        response = run.response_by_id(2)
        self.assertFalse(harness.tool_is_error(response), response)
        return json.loads(harness.tool_result_text(response))["results"][0]

    def test_fetch_dependencies_direct_importer(self):
        _producer_consumer(self.cwd)
        hint = self.dep_hint("packages/producer/src/producer/ingest/rankings.py")
        result = self.follow(hint)
        impact = result["dependency_impact"]
        self.assertEqual(impact["coverage"], "complete")
        self.assertEqual(set(impact["dependents"]), {
            "consumer/src/consumer/direct.py",
            "packages/producer/src/producer/ingest/__init__.py",
        })

    def test_fetch_dependencies_barrel_importer(self):
        _producer_consumer(self.cwd)
        hint = self.dep_hint("packages/producer/src/producer/ingest/__init__.py")
        impact = self.follow(hint)["dependency_impact"]
        self.assertIn("consumer/src/consumer/surface.py", impact["dependents"])

    def test_fetch_dependencies_warm_index_drops_removed_edge(self):
        _producer_consumer(self.cwd)
        hint = self.dep_hint("packages/producer/src/producer/ingest/rankings.py")
        first = self.follow(hint)["dependency_impact"]
        self.assertIn("consumer/src/consumer/direct.py", first["dependents"])
        # Mutate one consumer and re-query WITHOUT clearing the index (same
        # worktree, client identity, and cache dir): the stale edge must drop.
        _write(self.cwd, "consumer/src/consumer/direct.py",
               "value = 1\n")
        second = self.follow(hint)["dependency_impact"]
        self.assertNotIn("consumer/src/consumer/direct.py", second["dependents"])
        self.assertEqual(second["coverage"], "complete")

    def test_fetch_dependencies_ambiguous_identity_is_partial(self):
        _producer_consumer(self.cwd)
        _write(self.cwd, "src/producer/ingest/rankings.py",
               "class RankingEntry:\n    pass\n")
        hint = self.dep_hint("packages/producer/src/producer/ingest/rankings.py")
        result = self.follow(hint)
        self.assertEqual(result["completeness"], "partial")
        self.assertEqual(result["dependency_impact"]["coverage"], "partial")
        self.assertFalse(result["dependency_impact"]["timed_out"])


if __name__ == "__main__":
    unittest.main()
