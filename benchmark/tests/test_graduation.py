import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from variants import Experiment, Variant, load_experiment  # noqa: E402

from benchmark.graduation.evaluate import evaluate, main  # noqa: E402
from benchmark.graduation.schema import BLOCKED, load_manifest  # noqa: E402


def _experiment_manifest(tmp_path: Path) -> Path:
    """Build a pinned experiment manifest via the existing variants machinery.

    Confirms the graduation evaluator's tests share the same experiment
    loading path (Experiment/load_experiment) as the rest of the harness,
    rather than reinventing manifest parsing.
    """
    manifest_path = tmp_path / "experiment.json"
    manifest_path.write_text(
        json.dumps(
            {
                "arm_order_seed": 163162,
                "variants": [
                    {"name": "no_tilth"},
                    {
                        "name": "upstream",
                        "repository": "https://github.com/jahala/tilth",
                        "git_sha": "ad9eb2cdb90a4333c4eec337ff7499e1867d248f",
                    },
                    {
                        "name": "fork",
                        "repository": "https://github.com/paulnsorensen/tilth",
                        "git_sha": "4bb885e76d4ade1babf51ee820c7588114df9ea9",
                    },
                ],
            }
        )
    )
    return manifest_path


def test_experiment_manifest_loads_via_variants_machinery(tmp_path):
    experiment = load_experiment(_experiment_manifest(tmp_path))
    assert isinstance(experiment, Experiment)
    assert {variant.name for variant in experiment.variants} == {
        "no_tilth",
        "upstream",
        "fork",
    }


def _results_jsonl(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "results.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


def _telemetry_dir(tmp_path: Path, rows: list[dict]) -> Path:
    telemetry_dir = tmp_path / "telemetry"
    telemetry_dir.mkdir()
    (telemetry_dir / "harness.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n"
    )
    return telemetry_dir


def test_blocked_floor_never_passes(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "thresholds": {"accuracy": 0.5},
                "floors": {"rg_search_dispatch": BLOCKED},
            }
        )
    )
    manifest = load_manifest(manifest_path)
    results = [{"correct": True, "total_cost_usd": 0.1}] * 10
    telemetry = [{"harness": "rg_search_dispatch", "passed": True}] * 10

    verdict = evaluate(manifest, results=results, telemetry=telemetry)

    assert verdict.status == "blocked"
    assert any(BLOCKED in reason for reason in verdict.reasons)


def test_missing_threshold_is_blocked(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "thresholds": {"accuracy": 0.5},
                "floors": {"rg_search_dispatch": 0.9},
            }
        )
    )
    manifest = load_manifest(manifest_path)
    # No results supplied at all -> "accuracy" has no measured value.
    telemetry = [{"harness": "rg_search_dispatch", "passed": True}] * 10

    verdict = evaluate(manifest, results=[], telemetry=telemetry)

    assert verdict.status == "blocked"
    assert any("accuracy" in reason and "no measured value" in reason for reason in verdict.reasons)


def test_pass_requires_every_gate_present_and_met(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "thresholds": {"accuracy": 0.5, "cost_per_correct": 1.0},
                "floors": {"rg_search_dispatch": 0.8},
            }
        )
    )
    manifest = load_manifest(manifest_path)
    results = [{"correct": True, "total_cost_usd": 0.2}] * 8 + [
        {"correct": False, "total_cost_usd": 0.2}
    ] * 2
    telemetry = [{"harness": "rg_search_dispatch", "passed": True}] * 9 + [
        {"harness": "rg_search_dispatch", "passed": False}
    ]

    verdict = evaluate(manifest, results=results, telemetry=telemetry)

    assert verdict.status == "pass"
    assert verdict.reasons == ()

    # Dropping the floor below its requirement flips the verdict to blocked.
    telemetry_failing = [{"harness": "rg_search_dispatch", "passed": False}] * 10
    failing_verdict = evaluate(manifest, results=results, telemetry=telemetry_failing)
    assert failing_verdict.status == "blocked"


def test_cli_exits_nonzero_when_blocked(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"thresholds": {}, "floors": {"rg_search_dispatch": BLOCKED}})
    )

    exit_code = main(["--manifest", str(manifest_path)])

    assert exit_code != 0


def test_evaluate_py_module_exists():
    module_path = Path(__file__).parent.parent / "graduation" / "evaluate.py"
    assert module_path.exists()


def test_cli_runs_as_module_from_repo_root(tmp_path):
    repo_root = Path(__file__).parent.parent.parent
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"thresholds": {}, "floors": {"rg_search_dispatch": BLOCKED}})
    )

    result = subprocess.run(
        [sys.executable, "-m", "benchmark.graduation.evaluate", "--manifest", str(manifest_path)],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "blocked" in result.stdout
