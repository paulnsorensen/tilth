"""External task loading, prepared workspaces, native grading, and dataset fetch (bench-external-tasks)."""

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import external
import external.data
import external.download
import external.featurebench
import external.patches
import external.preflight
import external.proc
import external.swebench_ml
import external.task
from external_support import (
    FB_F2P_EDIT, FB_LV1, OUTPUTS, SWE_GO, SWE_PY, SWE_RS, git, parquet_bytes, projects, rows,
)

FEATUREBENCH_URL = ("https://huggingface.co/datasets/LiberCoders/FeatureBench/resolve/v1.1/"
                    "data/lite-00000-of-00001.parquet")
SWEBENCH_ML_URL = ("https://huggingface.co/datasets/SWE-bench/SWE-bench_Multilingual/resolve/"
                   "846e647b9f33c0b51b739d005d13d85493c9af09/data/test-00000-of-00001.parquet")


def load(instance_id: str) -> external.ExternalTask:
    if instance_id in rows("featurebench"):
        return external.featurebench.load(instance_id, external.FEATUREBENCH_REVISION)
    return external.swebench_ml.load(instance_id, external.SWEBENCH_ML_REVISION)


def prepared(bench, instance_id: str, tmp_path: Path) -> tuple[external.ExternalTask, Path]:
    bench.seed(instance_id)
    task = load(instance_id)
    workdir = tmp_path / "workdir"
    task.prepare(workdir)
    return task, workdir


def apply_patch(workdir: Path, patch: str) -> None:
    subprocess.run(["git", "apply", "-"], cwd=workdir, input=patch, text=True, check=True, capture_output=True)


def tree_text(root: Path) -> dict[str, str]:
    skipped = {".git", ".venv", "target"}
    return {
        str(path.relative_to(root)): path.read_text(errors="replace")
        for path in sorted(root.rglob("*"))
        if path.is_file() and not skipped & set(path.relative_to(root).parts)
    }


def base_file(bench, repo: str, relative: str) -> str | None:
    row_base = {row["repo"]: row["base_commit"] for dataset in ("featurebench", "swebench_ml")
                for row in rows(dataset).values()}[repo]
    try:
        return git("show", f"{row_base}:{relative}", cwd=bench.upstream / f"{repo}.git")
    except subprocess.CalledProcessError:
        return None


def test_fixture_rows_use_real_keys_and_pinned_bases(external_bench) -> None:
    featurebench_keys = {"instance_id", "patch", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS", "image_name",
                         "repo", "base_commit", "problem_statement", "repo_settings"}
    swebench_keys = {"base_commit", "created_at", "eval_type", "image", "instance_id", "log_parser", "repo",
                     "version", "patch", "test_patch", "eval_script", "problem_statement", "hints_text",
                     "FAIL_TO_PASS", "PASS_TO_PASS"}
    assert all(set(row) == featurebench_keys for row in rows("featurebench").values())
    assert all(set(row) == swebench_keys for row in rows("swebench_ml").values())
    for dataset in ("featurebench", "swebench_ml"):
        for row in rows(dataset).values():
            upstream = external_bench.upstream / f"{row['repo']}.git"
            assert git("rev-parse", "main", cwd=upstream).strip() == row["base_commit"]


# --- AC-1: prepared workspace ---


@pytest.mark.parametrize("instance_id", [SWE_PY, FB_LV1])
def test_prepare_hides_tests_and_history(external_bench, tmp_path: Path, instance_id: str) -> None:
    task, workdir = prepared(external_bench, instance_id, tmp_path)
    row = external_bench.row(instance_id)
    files = tree_text(workdir)
    held_out_lines = [line[1:] for line in row["test_patch"].splitlines()
                      if line.startswith(("+", "-")) and not line.startswith(("+++", "---")) and line[1:].strip()]

    if instance_id == FB_LV1:
        assert not (workdir / "tests" / "test_area.py").exists()
        assert not any("square_area(3)" in text for text in files.values())
    else:
        assert not any("test_mul" in text for text in files.values())
    for text in files.values():
        assert row["patch"] not in text and row["test_patch"] not in text
        for entry in (*row["FAIL_TO_PASS"], *row["PASS_TO_PASS"]):
            assert entry not in text
    assert held_out_lines
    assert git("remote", cwd=workdir).strip() == ""
    assert git("rev-list", "--count", "--all", cwd=workdir).strip() == "1"
    assert row["base_commit"] not in git("log", "--all", "--format=%H", cwd=workdir)


@pytest.mark.parametrize("instance_id", [FB_LV1, FB_F2P_EDIT])
def test_featurebench_prepare_applies_mask_forward(external_bench, tmp_path: Path, instance_id: str) -> None:
    task, workdir = prepared(external_bench, instance_id, tmp_path)

    area = (workdir / "shapes" / "area.py").read_text()
    assert "def square_area" not in area and "def circle_area" not in area
    assert "def _half" in area
    assert not (workdir / "tests" / "test_area.py").exists()
    assert (workdir / "tests" / "test_misc.py").is_file()
    assert task.source.transformation == "mask_patch_forward"
    assert git("status", "--porcelain", cwd=workdir).strip() == ""


# --- AC-2: native environment ---


def test_python_env_uses_uv_venv(external_bench, tmp_path: Path) -> None:
    _, workdir = prepared(external_bench, FB_LV1, tmp_path)

    venvs = [argv for argv in external_bench.commands if argv[:2] == ["uv", "venv"]]
    assert venvs and venvs[0][-1] == str(workdir / ".venv")
    assert (workdir / ".venv" / "bin" / "python").exists()
    excluded = (workdir / ".git" / "info" / "exclude").read_text().split()
    assert ".venv/" in excluded and "target/" in excluded


@pytest.mark.parametrize(("instance_id", "argv"), [
    (SWE_GO, ["go", "mod", "download"]),
    (SWE_RS, ["cargo", "fetch"]),
])
def test_go_rust_use_host_toolchain(external_bench, tmp_path: Path, instance_id: str, argv: list[str]) -> None:
    _, workdir = prepared(external_bench, instance_id, tmp_path)

    assert (argv, str(workdir)) in list(zip(external_bench.commands, external_bench.cwds))
    assert not external_bench.argv_containing("venv")


def test_no_container_process(external_bench, tmp_path: Path) -> None:
    for instance_id in (SWE_PY, FB_LV1):
        task, workdir = prepared(external_bench, instance_id, tmp_path / instance_id)
        task.check_correctness("", str(workdir))
        external.preflight.admit(instance_id)

    assert external_bench.commands
    assert not [argv for argv in external_bench.commands
                if any(Path(part).name in {"docker", "podman"} for part in argv)]


# --- AC-3: native grading ---


@pytest.mark.parametrize("instance_id", [FB_LV1, FB_F2P_EDIT])
def test_featurebench_gold_resolves_on_real_row_shape(external_bench, tmp_path: Path, instance_id: str) -> None:
    task, workdir = prepared(external_bench, instance_id, tmp_path)

    assert "tests/test_area.py" not in task.gold_patch
    apply_patch(workdir, task.gold_patch)
    correct, reason = task.check_correctness("", str(workdir))

    assert correct, reason
    assert task.grade_details() == {"pass_rate": 1.0, "f2p_passed": 1, "f2p_total": 1,
                                    "p2p_passed": 1, "p2p_total": 1}


def test_gold_changes_grade_correct(external_bench, tmp_path: Path) -> None:
    task, workdir = prepared(external_bench, SWE_PY, tmp_path)
    apply_patch(workdir, task.gold_patch)
    (workdir / ".venv" / "junk.py").write_text("raise SystemExit(1)\n")
    (workdir / "target" / "debug").mkdir(parents=True)
    (workdir / "target" / "debug" / "calc.py").write_text("raise SystemExit(1)\n")

    correct, reason = task.check_correctness("", str(workdir))

    assert correct, reason
    assert task.grade_details()["pass_rate"] == 1.0


def _weaken_swe_tests(workdir: Path) -> None:
    (workdir / "tests" / "test_ops.py").write_text(
        "def test_add():\n    pass\n\n\ndef test_mul():\n    pass\n\n\ndef test_div():\n    pass\n")


def _delete_swe_tests(workdir: Path) -> None:
    (workdir / "tests" / "test_ops.py").unlink()


def _recreate_featurebench_tests(workdir: Path) -> None:
    (workdir / "tests" / "test_area.py").write_text(
        "def test_square_area():\n    pass\n\n\ndef test_circle_area():\n    pass\n")


def _delete_featurebench_p2p(workdir: Path) -> None:
    (workdir / "tests" / "test_misc.py").unlink()
    _recreate_featurebench_tests(workdir)


@pytest.mark.parametrize(("instance_id", "edit"), [
    (SWE_PY, _weaken_swe_tests),
    (SWE_PY, _delete_swe_tests),
    (FB_LV1, _recreate_featurebench_tests),
    (FB_LV1, _delete_featurebench_p2p),
])
def test_agent_test_edits_do_not_mask_heldout(external_bench, tmp_path: Path, instance_id: str, edit) -> None:
    task, workdir = prepared(external_bench, instance_id, tmp_path)
    edit(workdir)

    correct, _ = task.check_correctness("", str(workdir))

    assert correct is False
    assert task.grade_details()["f2p_passed"] < task.grade_details()["f2p_total"]


def test_featurebench_file_entries_aggregate() -> None:
    output = (OUTPUTS / "pytest_rA.txt").read_text()
    entries = ["tests/test_a.py", "tests/test_ab.py", "tests/test_b.py", "tests/test_c.py",
               "tests/test_skip.py", "tests/test_d.py", "tests/test_a"]

    assert external.featurebench.file_verdicts(output, entries) == {
        "tests/test_a.py": True,
        "tests/test_ab.py": False,
        "tests/test_b.py": False,
        "tests/test_c.py": False,
        "tests/test_skip.py": False,
        "tests/test_d.py": False,
        "tests/test_a": False,
    }


def test_go_and_rust_outputs_parse_per_test() -> None:
    go = (OUTPUTS / "go_test_v.txt").read_text()
    cargo = (OUTPUTS / "cargo_test.txt").read_text()
    pytest_output = (OUTPUTS / "pytest_rA.txt").read_text()

    assert external.swebench_ml.entry_verdicts(
        go, ["TestAdd", "TestMul", "TestSub/neg", "TestSub/pos", "TestSub", "TestSkip", "TestDiv"], "go",
    ) == {"TestAdd": True, "TestMul": False, "TestSub/neg": True, "TestSub/pos": False, "TestSub": False,
          "TestSkip": False, "TestDiv": False}
    assert external.swebench_ml.entry_verdicts(
        cargo, ["add_works", "mul_works", "nested::div_works", "slow_works", "tests::bench_like", "div_works"],
        "rust",
    ) == {"add_works": True, "mul_works": False, "nested::div_works": True, "slow_works": False,
          "tests::bench_like": True, "div_works": False}
    assert external.swebench_ml.entry_verdicts(
        pytest_output, ["tests/test_a.py::test_x", "tests/test_a.py::test_y[a b]", "tests/test_b.py::test_two",
                        "tests/test_a.py::test_missing"], "python",
    ) == {"tests/test_a.py::test_x": True, "tests/test_a.py::test_y[a b]": True,
          "tests/test_b.py::test_two": False, "tests/test_a.py::test_missing": False}


# --- AC-5: loaders, cached rows, language, resolution, fetch ---


@pytest.mark.parametrize("instance_id", [FB_LV1, FB_F2P_EDIT, SWE_PY, SWE_GO, SWE_RS])
def test_loaders_build_tasks_from_pinned_rows(external_bench, tmp_path: Path, instance_id: str) -> None:
    task, workdir = prepared(external_bench, instance_id, tmp_path)
    row = external_bench.row(instance_id)
    dataset = "featurebench" if "repo_settings" in row else "swebench_ml"

    assert task.name == instance_id
    assert task.repo == dataset
    assert row["repo"] in task.source.origin and task.source.commit_or_tag == row["base_commit"]
    assert task.prompt == row["problem_statement"] == task.problem_statement
    assert not any(entry in task.prompt for entry in (*row["FAIL_TO_PASS"], *row["PASS_TO_PASS"]))
    if dataset == "swebench_ml":
        assert task.gold_patch == row["patch"]
    else:
        apply_patch(workdir, task.gold_patch)
        for relative, content in projects()[row["repo"]].items():
            if relative not in row["FAIL_TO_PASS"]:
                assert (workdir / relative).read_text() == content, relative

    loader = external.featurebench.load if dataset == "featurebench" else external.swebench_ml.load
    with pytest.raises(ValueError):
        loader(instance_id, "main")
    external.data.row_path(dataset, external.data.REVISIONS[dataset], instance_id).unlink()
    with pytest.raises(LookupError):
        loader(instance_id, external.data.REVISIONS[dataset])


def test_fetch_pins_revisions(external_bench, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(external.featurebench, "PARETO12_IDS", (FB_LV1,))
    monkeypatch.setattr(external.swebench_ml, "PICKS", {"go": (SWE_GO,), "rust": (SWE_RS,)})
    requested: list[str] = []

    def client(url: str) -> bytes:
        requested.append(url)
        dataset = "featurebench" if "FeatureBench" in url else "swebench_ml"
        return parquet_bytes(list(rows(dataset).values()))

    external.download.fetch("featurebench", client=client)
    external.download.fetch("swebench_ml", client=client)

    assert requested == [FEATUREBENCH_URL, SWEBENCH_ML_URL]
    stored = sorted(str(path.relative_to(external_bench.data / "rows"))
                    for path in (external_bench.data / "rows").rglob("*.json"))
    assert stored == sorted(
        [f"featurebench/v1.1/{instance_id}.json" for instance_id in rows("featurebench")]
        + [f"swebench_ml/846e647b9f33c0b51b739d005d13d85493c9af09/{instance_id}.json"
           for instance_id in rows("swebench_ml")]
    )
    for instance_id in (FB_LV1, SWE_GO, SWE_RS):
        row = external_bench.row(instance_id)
        git("cat-file", "-e", f"{row['base_commit']}^{{commit}}", cwd=external.data.mirror_path(row["repo"]))

    before = len(external_bench.commands)
    external.download.fetch("featurebench", client=client)
    external.download.fetch("swebench_ml", client=client)

    assert len(requested) == 2
    assert not [argv for argv in external_bench.commands[before:] if {"fetch", "clone"} & set(argv)]


def _block_side_effects(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    attempts: list[object] = []

    def blocked(*args, **kwargs):
        attempts.append(args)
        raise AssertionError(f"blocked side effect: {args!r}")

    monkeypatch.setattr(subprocess, "run", blocked)
    monkeypatch.setattr(subprocess, "Popen", blocked)
    monkeypatch.setattr(urllib.request, "urlopen", blocked)
    monkeypatch.setattr(external.proc, "run", blocked)
    monkeypatch.setattr(external.download, "fetch", blocked)
    return attempts


def _snapshot(root: Path) -> dict[str, tuple[int, bytes]]:
    return {str(path): (path.stat().st_mtime_ns, path.read_bytes())
            for path in root.rglob("*") if path.is_file()}


def test_cached_row(external_bench, monkeypatch: pytest.MonkeyPatch) -> None:
    external_bench.seed(FB_LV1, SWE_GO)
    before = _snapshot(external_bench.data)
    attempts = _block_side_effects(monkeypatch)

    assert external.cached_row(FB_LV1) == rows("featurebench")[FB_LV1]
    assert external.cached_row(SWE_GO) == rows("swebench_ml")[SWE_GO]
    assert external.cached_row(SWE_RS) is None
    assert external.cached_row("../../etc/passwd") is None
    assert attempts == []
    assert _snapshot(external_bench.data) == before


def test_language_of(external_bench) -> None:
    redacted = {instance_id: {key: row[key] for key in ("instance_id", "repo", "base_commit")}
                for dataset in ("featurebench", "swebench_ml") for instance_id, row in rows(dataset).items()}

    assert external.language_of(redacted[FB_LV1]) == "python"
    assert external.language_of(redacted[SWE_GO]) == "go"
    assert external.language_of(redacted[SWE_RS]) == "rust"
    assert external.language_of({"instance_id": "x", "repo": "fixture/calcgo", "language": "rust"}) == "rust"
    with pytest.raises(ValueError):
        external.language_of({"instance_id": "x", "repo": "nobody/unmapped", "base_commit": "0" * 40})
    external_bench.seed(FB_LV1, SWE_GO, SWE_RS)
    for instance_id in (FB_LV1, SWE_GO, SWE_RS):
        assert load(instance_id).language == external.language_of(redacted[instance_id])


def test_pinned_repo_map_covers_candidates() -> None:
    assert external.language_of({"repo": "sympy/sympy"}) == "python"
    assert external.language_of({"repo": "pydata/xarray"}) == "python"
    assert external.language_of({"repo": "gin-gonic/gin"}) == "go"
    assert external.language_of({"repo": "prometheus/prometheus"}) == "go"
    assert external.language_of({"repo": "sharkdp/bat"}) == "rust"
    assert external.language_of({"repo": "tokio-rs/tokio"}) == "rust"


def _verdict(instance_id: str, admitted: bool, reason: str) -> external.preflight.PreflightVerdict:
    return external.preflight.PreflightVerdict(
        instance_id=instance_id, dataset="swebench_ml", data_rev=external.SWEBENCH_ML_REVISION,
        env_fingerprint="stub", admitted=admitted, reason=reason,
    )


def test_resolve_task(external_bench, monkeypatch: pytest.MonkeyPatch) -> None:
    external_bench.seed(SWE_PY)
    admitted: list[str] = []
    verdicts = {SWE_PY: _verdict(SWE_PY, True, "admitted")}

    def fake_admit(instance_id: str) -> external.preflight.PreflightVerdict:
        admitted.append(instance_id)
        return verdicts[instance_id]

    def no_download(*args, **kwargs):
        raise AssertionError("resolve_task downloaded")

    monkeypatch.setattr(external.preflight, "admit", fake_admit)
    monkeypatch.setattr(external.download, "fetch", no_download)
    monkeypatch.setattr(urllib.request, "urlopen", no_download)

    task = external.resolve_task(SWE_PY)
    assert isinstance(task, external.ExternalTask) and task.name == SWE_PY

    verdicts[SWE_PY] = _verdict(SWE_PY, False, "gold_unresolved")
    assert external.resolve_task(SWE_PY) is None

    assert external.resolve_task("nobody__unknown-1") is None
    assert admitted == [SWE_PY, SWE_PY]
    assert not external_bench.argv_containing("fetch")


def test_featurebench_candidates_curated() -> None:
    pareto = [
        "sympy__sympy.c1097516.test_nullspace.f14fc970.lv1",
        "pydata__xarray.97f3a746.test_treenode.aa8ba777.lv2",
        "pandas-dev__pandas.82fa2715.test_concat.ebe5de39.lv1",
        "astropy__astropy.b0db0daa.test_table.48eef659.lv1",
        "huggingface__transformers.e2e8dbed.test_serve.4e7860c7.lv1",
        "Lightning-AI__pytorch-lightning.126fa6f1.test_data.c8b292af.lv1",
        "mlflow__mlflow.93dab383.test_span.69efd376.lv1",
        "mwaskom__seaborn.7001ebe7.test_regression.ce8c62e2.lv1",
    ]

    assert external.featurebench.candidates(pareto) == [
        "sympy__sympy.c1097516.test_nullspace.f14fc970.lv1",
        "mwaskom__seaborn.7001ebe7.test_regression.ce8c62e2.lv1",
    ]
    default = external.featurebench.candidates()
    assert default and all(instance_id.endswith(".lv1") for instance_id in default)
    deferred = ("pandas-dev__", "astropy__", "huggingface__transformers", "Lightning-AI__", "mlflow__")
    assert not [instance_id for instance_id in default if instance_id.startswith(deferred)]


def test_patch_helpers_split_reverse_and_tamper() -> None:
    row = rows("featurebench")[FB_F2P_EDIT]
    gold = rows("swebench_ml")[SWE_PY]["patch"]

    assert [path for path, _ in external.patches.split_files(row["patch"])] == ["shapes/area.py", "tests/test_area.py"]
    assert external.patches.reverse(external.patches.reverse(row["patch"])) == row["patch"]
    tampered = external.patches.tampered(gold)
    assert "+    return left * right" in tampered
    assert "+    return left // right\n" not in tampered
    single = external.patches.tampered(external.patches.split_files(rows("swebench_ml")[SWE_GO]["test_patch"])[0][1])
    assert not [line for line in single.splitlines() if line.startswith("+") and not line.startswith("+++")]


def _featurebench_variant(bench, instance_id: str, **settings) -> external.ExternalTask:
    row = bench.row(FB_LV1)
    merged = {**json.loads(row["repo_settings"]), **settings}
    bench.seed_row({**row, "instance_id": instance_id, "repo_settings": json.dumps(merged)})
    return external.featurebench.load(instance_id, external.FEATUREBENCH_REVISION)


def test_featurebench_env_follows_dataset_setup_order(external_bench, tmp_path: Path) -> None:
    task = _featurebench_variant(
        external_bench, "fixture__shapes.0a1b2c3d.test_area.0rder000.lv1",
        pip_packages=["pytest", "rich>=13"], pre_install=["python -c pass"],
        install="python -c pass && false && touch never-installed",
    )
    workdir = tmp_path / "workdir"

    task.prepare(workdir)

    steps = [argv for argv in external_bench.commands
             if argv[:2] in (["uv", "pip"], ["bash", "-c"]) or argv[0].endswith("/.venv/bin/python")]
    assert steps[:3] == [["uv", "pip", "install", "pytest-timeout"], ["uv", "pip", "install", "pytest"],
                         ["uv", "pip", "install", "rich>=13"]]
    assert steps[3] == [str(workdir / ".venv" / "bin" / "python"), "-c", "pass"]
    assert steps[4][:2] == ["bash", "-c"] and "python -c pass && false" in steps[4][2]
    assert not (workdir / "never-installed").exists()


def test_failing_install_command_fails_the_env_build(external_bench, tmp_path: Path) -> None:
    task = _featurebench_variant(external_bench, "fixture__shapes.0a1b2c3d.test_area.fa11ed00.lv1",
                                 install="python -c 'raise SystemExit(3)'")

    with pytest.raises(external.task.EnvBuildError):
        task.prepare(tmp_path / "workdir")


def test_grader_disables_summary_replacing_reporter(external_bench, tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    task, workdir = prepared(external_bench, FB_LV1, tmp_path)
    apply_patch(workdir, task.gold_patch)
    grading: list[list[str]] = []
    recording = external.proc.run

    def record(argv, **kwargs):
        if "pytest" in argv:
            grading.append(list(argv))
        return recording(argv, **kwargs)

    monkeypatch.setattr(external.proc, "run", record)
    correct, reason = task.check_correctness("", str(workdir))

    assert correct, reason
    [argv] = grading
    # pytest-pretty registers as "pretty" and replaces the -rA summary the grader parses.
    assert argv[argv.index("no:pretty") - 1] == "-p"


def test_container_named_packages_are_not_container_steps(external_bench, tmp_path: Path) -> None:
    task = _featurebench_variant(external_bench, "fixture__shapes.0a1b2c3d.test_area.d0c4e5d4.lv1",
                                 pip_packages=["docker", "podman-compose"],
                                 install="python -c pass && GOFLAGS=x sudo docker info")

    with pytest.raises(external.task.EnvBuildError, match="container steps"):
        task.prepare(tmp_path / "workdir")
    assert ["uv", "pip", "install", "docker"] in external_bench.commands
    assert ["uv", "pip", "install", "podman-compose"] in external_bench.commands


def test_interrupted_fetch_leaves_no_truncated_row(external_bench, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(external.featurebench, "PARETO12_IDS", ())
    replaced: list[str] = []
    real_replace = os.replace

    def crash_on_second(source, target):
        replaced.append(str(target))
        if len(replaced) == 2:
            raise KeyboardInterrupt
        real_replace(source, target)

    def client(url: str) -> bytes:
        return parquet_bytes(list(rows("featurebench").values()))

    monkeypatch.setattr(os, "replace", crash_on_second)
    with pytest.raises(KeyboardInterrupt):
        external.download.fetch("featurebench", client=client)
    monkeypatch.setattr(os, "replace", real_replace)

    for path in (external_bench.data / "rows").rglob("*.json"):
        json.loads(path.read_text())
    assert len(list((external_bench.data / "rows").rglob("*.json"))) == 1
    external.download.fetch("featurebench", client=client)
    assert all(external.cached_row(instance_id) == row for instance_id, row in rows("featurebench").items())


@pytest.mark.parametrize("step", ["env -i docker info", "sudo -E podman info", "A=1 env -u B docker ps"])
def test_container_after_prefix_options_is_refused(external_bench, tmp_path: Path, step: str) -> None:
    task = _featurebench_variant(external_bench, "fixture__shapes.0a1b2c3d.test_area.0e0e0e0e.lv1",
                                 install=f"python -c pass && {step}")

    with pytest.raises(external.task.EnvBuildError, match="container steps"):
        task.prepare(tmp_path / "workdir")
