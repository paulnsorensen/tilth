"""AC-8 self-tests for benchmark/extraction/compare.py.

Proves the comparator rejects bad candidate results instead of silently
passing them, and that it classifies the happy path and the blocked path
correctly. Hermetic: builds synthetic manifests and records under a
temporary directory, never reads a live .generated run.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import compare  # noqa: E402


def make_manifest(fixture_id="rust-sample", language="rust"):
    return {
        "version": 1,
        "fixtures": [
            {
                "id": fixture_id,
                "source": f"src/{language}/sample.rs",
                "language": language,
                "hash": {"algorithm": "xxh32", "value": "deadbeef"},
                "capabilities": {
                    "definitions": {
                        "applicability": "applicable",
                        "expected": [["Function", "compute"]],
                        "evidence": "synthetic",
                    },
                    "nesting": {
                        "applicability": "applicable",
                        "expected": [],
                        "evidence": "synthetic",
                    },
                    "signatures": {
                        "applicability": "applicable",
                        "expected": [["compute", "pub fn compute() -> i32"]],
                        "evidence": "synthetic",
                    },
                    "imports": {
                        "applicability": "applicable",
                        "expected": [],
                        "evidence": "synthetic",
                    },
                    "edit_spans": {
                        "applicability": "applicable",
                        "expected": [["compute", "pub fn compute() -> i32 {\n    0\n}"]],
                        "evidence": "synthetic",
                    },
                },
            }
        ],
    }


def write_record(directory, fixture_id, record):
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / f"{fixture_id}.json", "w", encoding="utf-8") as handle:
        json.dump(record, handle)


def correct_record(fixture_id="rust-sample"):
    return {
        "version": 1,
        "fixture_id": fixture_id,
        "definitions": {"status": "supported", "value": [["function", "compute"]]},
        "nesting": {"status": "supported", "value": []},
        "signatures": {"status": "supported", "value": [["compute", "pub fn compute() -> i32"]]},
        "imports": {"status": "supported", "value": []},
        "edit_spans": {
            "status": "supported",
            "value": [["compute", "pub fn compute() -> i32 {\n    0\n}"]],
        },
    }


class BuildMatrixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manifest = make_manifest()

    def cell(self, matrix, capability, candidate="cand", language="rust"):
        return matrix["candidates"][candidate][language][capability]

    def test_happy_path_is_pass(self):
        cand_dir = self.root / "cand"
        write_record(cand_dir, "rust-sample", correct_record())

        matrix = compare.build_matrix(self.manifest, {"cand": cand_dir})

        for capability in compare.CAPABILITIES:
            cell = self.cell(matrix, capability)
            self.assertEqual(cell["verdict"], "pass", msg=f"{capability}: {cell}")

    def test_missing_record_is_blocked(self):
        cand_dir = self.root / "cand-missing"
        # No record written at all for this fixture.

        matrix = compare.build_matrix(self.manifest, {"cand": cand_dir})

        for capability in compare.CAPABILITIES:
            cell = self.cell(matrix, capability)
            self.assertEqual(cell["verdict"], "blocked", msg=f"{capability}: {cell}")
            self.assertIn("missing normalized record", cell["fixtures"][0]["detail"])

    def test_missing_capability_output_is_rejected(self):
        """AC-8 fault 1: a record with a capability output missing entirely.

        The comparator must not silently treat the absent entry as a pass;
        it must flag the cell (blocked), never pass or unsupported.
        """
        cand_dir = self.root / "cand-missing-cap"
        record = correct_record()
        del record["edit_spans"]
        write_record(cand_dir, "rust-sample", record)

        matrix = compare.build_matrix(self.manifest, {"cand": cand_dir})

        cell = self.cell(matrix, "edit_spans")
        self.assertEqual(cell["verdict"], "blocked")
        self.assertNotIn(cell["verdict"], ("pass", "unsupported"))

    def test_incorrect_owned_edit_span_is_a_fail(self):
        """AC-8 fault 2: a record whose owned edit-span text is wrong.

        A span missing its leading doc comment (an ownership error the spec
        calls out explicitly) must mark that cell `fail`, never `pass`.
        """
        cand_dir = self.root / "cand-bad-span"
        record = correct_record()
        record["edit_spans"] = {
            "status": "supported",
            # Missing the function body -- wrong owned source text.
            "value": [["compute", "pub fn compute() -> i32 {\n"]],
        }
        write_record(cand_dir, "rust-sample", record)

        matrix = compare.build_matrix(self.manifest, {"cand": cand_dir})

        cell = self.cell(matrix, "edit_spans")
        self.assertEqual(cell["verdict"], "fail")
        self.assertNotEqual(cell["verdict"], "pass")

    def test_unsupported_status_is_unsupported_not_pass(self):
        cand_dir = self.root / "cand-unsupported"
        record = correct_record()
        record["nesting"] = {"status": "unsupported"}
        write_record(cand_dir, "rust-sample", record)

        matrix = compare.build_matrix(self.manifest, {"cand": cand_dir})

        cell = self.cell(matrix, "nesting")
        self.assertEqual(cell["verdict"], "unsupported")

    def test_definitions_kind_case_is_normalized_but_name_is_not(self):
        cand_dir = self.root / "cand-case"
        record = correct_record()
        record["definitions"] = {"status": "supported", "value": [["FUNCTION", "compute"]]}
        write_record(cand_dir, "rust-sample", record)

        matrix = compare.build_matrix(self.manifest, {"cand": cand_dir})

        self.assertEqual(self.cell(matrix, "definitions")["verdict"], "pass")

        record["definitions"] = {"status": "supported", "value": [["function", "wrong_name"]]}
        write_record(cand_dir, "rust-sample", record)
        matrix = compare.build_matrix(self.manifest, {"cand": cand_dir})
        self.assertEqual(self.cell(matrix, "definitions")["verdict"], "fail")

    def test_fixture_level_known_tilth_divergence_reaches_matrix_cell(self):
        """M1: the manifest stores known_tilth_divergence on the fixture, not
        the capability (the schema forbids it on capability). Reading it at
        the wrong nesting level silently drops every divergence annotation.
        """
        manifest = make_manifest()
        manifest["fixtures"][0]["known_tilth_divergence"] = "synthetic divergence note"
        cand_dir = self.root / "cand-divergence"
        write_record(cand_dir, "rust-sample", correct_record())

        matrix = compare.build_matrix(manifest, {"cand": cand_dir})

        cell = self.cell(matrix, "definitions")
        self.assertEqual(cell["fixtures"][0].get("known_tilth_divergence"), "synthetic divergence note")

    def test_missing_expected_is_blocked_not_a_crash(self):
        """L5: 'expected' is optional per schema; a manifest edited to drop it
        must yield a blocked cell, not a KeyError."""
        manifest = make_manifest()
        del manifest["fixtures"][0]["capabilities"]["definitions"]["expected"]
        cand_dir = self.root / "cand-no-expected"
        write_record(cand_dir, "rust-sample", correct_record())

        matrix = compare.build_matrix(manifest, {"cand": cand_dir})

        cell = self.cell(matrix, "definitions")
        self.assertEqual(cell["verdict"], "blocked")

    def test_malformed_definitions_entry_is_blocked_not_a_crash(self):
        """L5: a malformed (non-pair) definitions entry must not raise
        ValueError out of the comparator; it must yield a blocked cell."""
        cand_dir = self.root / "cand-malformed"
        record = correct_record()
        record["definitions"] = {"status": "supported", "value": [["function", "compute", "extra"]]}
        write_record(cand_dir, "rust-sample", record)

        matrix = compare.build_matrix(self.manifest, {"cand": cand_dir})

        cell = self.cell(matrix, "definitions")
        self.assertEqual(cell["verdict"], "blocked")

    def test_candidate_error_status_is_a_fail_not_a_pass(self):
        """AC-8: a candidate that reports an extraction error must mark the
        cell `fail`, never `pass` or `blocked`. Guards compare.py's
        status=='error' branch against a regression that swallows errors."""
        cand_dir = self.root / "cand-error"
        record = correct_record()
        record["definitions"] = {"status": "error", "value": "parser panicked"}
        write_record(cand_dir, "rust-sample", record)

        matrix = compare.build_matrix(self.manifest, {"cand": cand_dir})

        cell = self.cell(matrix, "definitions")
        self.assertEqual(cell["verdict"], "fail")
        self.assertNotIn(cell["verdict"], ("pass", "blocked", "unsupported"))

    def test_unrecognized_status_is_blocked_not_a_pass(self):
        """AC-8: a candidate record with a status the comparator does not
        recognize must yield `blocked`, never a silent `pass`. Guards
        compare.py's terminal unrecognized-status branch."""
        cand_dir = self.root / "cand-garbage-status"
        record = correct_record()
        record["definitions"] = {"status": "totally-bogus", "value": [["function", "compute"]]}
        write_record(cand_dir, "rust-sample", record)

        matrix = compare.build_matrix(self.manifest, {"cand": cand_dir})

        cell = self.cell(matrix, "definitions")
        self.assertEqual(cell["verdict"], "blocked")
        self.assertNotEqual(cell["verdict"], "pass")


class ValuesEqualTests(unittest.TestCase):
    def test_duplicate_counts_matter(self):
        expected = [["Function", "compute"], ["Function", "compute"]]
        actual_short = [["function", "compute"]]
        self.assertFalse(compare.values_equal("definitions", expected, actual_short))
        actual_full = [["function", "compute"], ["function", "compute"]]
        self.assertTrue(compare.values_equal("definitions", expected, actual_full))

    def test_order_independent(self):
        expected = [["compute", "sig-a"], ["deep", None]]
        actual = [["deep", None], ["compute", "sig-a"]]
        self.assertTrue(compare.values_equal("signatures", expected, actual))


if __name__ == "__main__":
    unittest.main()
