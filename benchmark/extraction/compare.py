#!/usr/bin/env python3
"""Capability comparator for the extraction-reuse experiment (curd 3).

Compares each isolated candidate's normalized capture records against the
frozen fixture manifest's expected values and reports a
pass/partial/unsupported/fail/blocked verdict for every
candidate x language x capability cell (AC-3, AC-6). Reads only records
already written to disk by curds 1-2 (--offline); it never re-runs a
capture or a candidate build.

Comparison rule for `definitions`: kind labels are compared
case-insensitively (Tilth emits PascalCase like "Function"; both
candidates emit lowercase like "function") because this is a labeling
convention, not an ownership or accuracy difference. Names, signature
text, import text, and edit-span text are NEVER normalized -- an exact
owned-span mismatch (missing a doc comment, decorator, or export wrapper)
is a real accuracy fail, not noise to average away.

All five capability values are lists of pairs/tuples. Order is not
required to match across tools that walk the syntax tree differently, but
count and content are: comparison is multiset (order-independent,
duplicate-sensitive) equality, so a candidate that emits extra or
missing entries for the same (kind, name) still fails.
"""

import argparse
import json
from pathlib import Path

CAPABILITIES = ("definitions", "nesting", "signatures", "imports", "edit_spans")
CANDIDATES = ("tree-sitter-language-pack", "ast-grep-outline")
VERDICTS = ("pass", "partial", "unsupported", "fail", "blocked")


def load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def load_manifest(manifest_path):
    return load_json(manifest_path)


def load_record(path):
    """Load a normalized record, or return None if the file is absent.

    A missing file is a curd-1/2 prerequisite gap, not a candidate finding.
    """
    if not Path(path).exists():
        return None
    return load_json(path)


def fixture_language_groups(manifest):
    groups = {}
    for fixture in manifest["fixtures"]:
        groups.setdefault(fixture["language"], []).append(fixture)
    return groups


def _sort_key(item):
    # Stringify every element so None never collides with str during
    # sort comparisons, and so multiset equality is well defined.
    return tuple(json.dumps(part, sort_keys=True) for part in item)


def values_equal(capability, expected, actual):
    """Multiset-compare two capability values.

    `definitions` entries are (kind, name) pairs; kind is lower-cased
    before comparison (accepted PascalCase-vs-lowercase labeling
    difference). Every other capability compares its tuples exactly,
    with no normalization.
    """
    if capability == "definitions":
        norm_expected = [(str(kind).lower(), name) for kind, name in expected]
        norm_actual = [(str(kind).lower(), name) for kind, name in actual]
    else:
        norm_expected = [tuple(item) for item in expected]
        norm_actual = [tuple(item) for item in actual]

    return sorted(norm_expected, key=_sort_key) == sorted(norm_actual, key=_sort_key)


def evaluate_fixture(capability, expected, applicability, record):
    """Return one fixture-level outcome dict for a candidate x capability cell.

    outcome is one of: match, fail, unsupported, inapplicable, blocked.
    """
    if applicability != "applicable":
        return {"outcome": "inapplicable", "detail": f"applicability={applicability}"}

    if record is None:
        return {"outcome": "blocked", "detail": "missing normalized record for this candidate/fixture"}

    cap = record.get(capability)
    if cap is None:
        return {"outcome": "blocked", "detail": f"record has no '{capability}' entry"}

    status = cap.get("status")
    if status == "unsupported":
        return {"outcome": "unsupported", "detail": None}
    if status == "error":
        return {"outcome": "fail", "detail": f"candidate reported an extraction error: {cap.get('value')}"}
    if status == "supported":
        if "value" not in cap:
            return {"outcome": "blocked", "detail": f"'{capability}' status is supported but 'value' is missing"}
        actual = cap["value"]
        if values_equal(capability, expected, actual):
            return {"outcome": "match", "detail": None}
        return {"outcome": "fail", "detail": "value does not match expected"}

    return {"outcome": "blocked", "detail": f"unrecognized status '{status}'"}


def aggregate_cell(fixture_results):
    """Fold per-fixture outcomes into one candidate x language x capability verdict.

    pass: every applicable fixture matched expected. partial: some matched,
    some did not. unsupported: the candidate structurally lacks the
    capability on every applicable fixture. fail: it attempted and never
    matched. blocked: a prerequisite (record, capability entry, or value)
    is missing on at least one applicable fixture -- this takes priority
    because a missing prerequisite makes every other verdict unreliable.
    """
    applicable = [r for r in fixture_results if r["outcome"] != "inapplicable"]

    if not applicable:
        return {"verdict": "unsupported", "detail": "no applicable fixtures for this language"}

    blocked = [r for r in applicable if r["outcome"] == "blocked"]
    if blocked:
        details = "; ".join(f"{r['fixture_id']}: {r['detail']}" for r in blocked)
        return {"verdict": "blocked", "detail": details}

    total = len(applicable)
    matches = sum(1 for r in applicable if r["outcome"] == "match")
    unsupported = sum(1 for r in applicable if r["outcome"] == "unsupported")

    if unsupported == total:
        return {"verdict": "unsupported", "detail": "candidate reports unsupported for all applicable fixtures"}
    if matches == total:
        return {"verdict": "pass", "detail": f"{matches}/{total} applicable fixtures matched expected values"}
    if matches > 0:
        return {"verdict": "partial", "detail": f"{matches}/{total} applicable fixtures matched expected values"}
    return {"verdict": "fail", "detail": f"0/{total} applicable fixtures matched expected values"}


def build_matrix(manifest, baseline_dir, candidate_dirs):
    """Build the complete candidate x language x capability verdict matrix.

    baseline_dir: directory of Tilth normalized records (used only to
    surface known_tilth_divergence annotations, never to change a
    candidate's verdict).
    candidate_dirs: {candidate_name: directory_of_normalized_records}.
    """
    groups = fixture_language_groups(manifest)
    matrix = {"version": 1, "candidates": {}}

    for candidate, record_dir in candidate_dirs.items():
        matrix["candidates"][candidate] = {}
        for language, fixtures in groups.items():
            matrix["candidates"][candidate][language] = {}
            for capability in CAPABILITIES:
                fixture_results = []
                for fixture in fixtures:
                    fixture_id = fixture["id"]
                    cap_manifest = fixture["capabilities"][capability]
                    expected = cap_manifest["expected"]
                    applicability = cap_manifest.get("applicability", "applicable")
                    record = load_record(Path(record_dir) / f"{fixture_id}.json")
                    result = evaluate_fixture(capability, expected, applicability, record)
                    result["fixture_id"] = fixture_id
                    divergence = cap_manifest.get("known_tilth_divergence")
                    if divergence:
                        result["known_tilth_divergence"] = divergence
                    fixture_results.append(result)
                cell = aggregate_cell(fixture_results)
                cell["fixtures"] = fixture_results
                matrix["candidates"][candidate][language][capability] = cell

    return matrix


def render_summary(matrix):
    lines = []
    for candidate, languages in matrix["candidates"].items():
        lines.append(f"== {candidate} ==")
        for language, capabilities in languages.items():
            row = ", ".join(f"{cap}={cell['verdict']}" for cap, cell in capabilities.items())
            lines.append(f"  {language}: {row}")
    return "\n".join(lines)


def default_candidate_dirs(extraction_root):
    return {
        candidate: extraction_root / ".generated" / "candidates" / candidate / "normalized"
        for candidate in CANDIDATES
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, help="Path to fixtures/manifest.json")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Read only on-disk .generated records; missing records become blocked cells.",
    )
    args = parser.parse_args(argv)

    if not args.offline:
        parser.error("compare.py only supports --offline: it never re-runs a capture")

    manifest_path = Path(args.manifest).resolve()
    manifest = load_manifest(manifest_path)
    extraction_root = manifest_path.parent.parent

    baseline_dir = extraction_root / ".generated" / "normalized"
    candidate_dirs = default_candidate_dirs(extraction_root)

    matrix = build_matrix(manifest, baseline_dir, candidate_dirs)

    out_dir = extraction_root / ".generated"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "verdict-matrix.json"
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(matrix, handle, indent=2, sort_keys=True)
        handle.write("\n")

    print(render_summary(matrix))
    print(f"\nFull matrix written to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
