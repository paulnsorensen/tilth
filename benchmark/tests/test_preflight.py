"""Admission of external instances: round trip, refusal reasons, verdict cache, and the preflight CLI."""

import json
import shutil
import subprocess
import sys
import textwrap
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
import external_support
from external_support import FB_F2P_EDIT, FB_LV1, FB_LV2, SWE_GO, SWE_PY, SWE_RS, git, run_python

AGENT_CLIS = {"claude", "codex", "opencode"}


def verdict(instance_id: str, admitted: bool, reason: str = "") -> external.preflight.PreflightVerdict:
    return external.preflight.PreflightVerdict(
        instance_id=instance_id, dataset="swebench_ml", data_rev=external.SWEBENCH_ML_REVISION,
        env_fingerprint="stub", admitted=admitted, reason=reason or ("admitted" if admitted else "gold_unresolved"),
        gold=(3, 3), empty=(1, 3), tampered=(2, 3),
    )


def patch_for(bench, repo: str, files: dict[str, str]) -> str:
    """A git diff from ``repo``'s base commit to ``files``."""
    work = bench.upstream / "edits" / repo
    if not work.exists():
        git("clone", "-q", str(bench.upstream / f"{repo}.git"), str(work), cwd=bench.upstream)
    external_support.write_tree(work, files)
    git("add", "-A", cwd=work)
    patch = git("diff", "--no-color", "--cached", "HEAD", cwd=work)
    git("reset", "-q", "--hard", "HEAD", cwd=work)
    return patch


# --- AC-4: admission round trip ---


@pytest.mark.parametrize(("instance_id", "reason"), [
    (SWE_PY, "admitted"),
    (FB_LV1, "admitted:mask_patch_forward"),
])
def test_preflight_admits_discriminating_fixture(external_bench, instance_id: str, reason: str) -> None:
    external_bench.seed(instance_id)

    result = external.preflight.admit(instance_id)

    assert result.admitted is True and result.reason == reason
    assert result.gold[0] == result.gold[1] and result.empty[0] < result.empty[1]
    assert result.tampered[0] < result.tampered[1]
    dataset = "featurebench" if instance_id == FB_LV1 else "swebench_ml"
    cached = external.preflight.verdict_path(instance_id, external.data.REVISIONS[dataset], result.env_fingerprint)
    assert json.loads(cached.read_text())["reason"] == reason
    assert not [argv for argv in external_bench.commands if Path(argv[0]).name in AGENT_CLIS]


@pytest.mark.parametrize(("instance_id", "toolchain"), [(SWE_GO, "go"), (SWE_RS, "cargo")])
def test_compiled_round_trip_rebuilds_each_grade(external_bench, monkeypatch: pytest.MonkeyPatch,
                                                 instance_id: str, toolchain: str) -> None:
    if shutil.which(toolchain) is None:
        pytest.skip(f"{toolchain} is not installed")
    # A target directory shared across grades would let the empty patch reuse the gold build.
    monkeypatch.setenv("CARGO_TARGET_DIR", str(external_bench.data / "shared-target"))
    external_bench.seed(instance_id)

    result = external.preflight.admit(instance_id)

    assert (result.admitted, result.gold, result.empty, result.tampered) == (True, (3, 3), (1, 3), (2, 3))


def test_patch_editing_f2p_file_is_admitted(external_bench, tmp_path: Path) -> None:
    external_bench.seed(FB_F2P_EDIT)
    task = external.featurebench.load(FB_F2P_EDIT, external.FEATUREBENCH_REVISION)
    workdir = tmp_path / "workdir"
    task.prepare(workdir)
    tampered = external.patches.tampered(task.gold_patch)
    subprocess.run(["git", "apply", "-"], cwd=workdir, input=tampered, text=True, check=True)

    assert task.check_correctness("", str(workdir))[0] is False
    result = external.preflight.admit(FB_F2P_EDIT)
    assert result.admitted is True and result.reason == "admitted:mask_patch_forward"


def test_admit_never_downloads(external_bench, monkeypatch: pytest.MonkeyPatch) -> None:
    external_bench.seed(SWE_PY)

    def no_download(*args, **kwargs):
        raise AssertionError("admit downloaded")

    monkeypatch.setattr(external.download, "fetch", no_download)
    monkeypatch.setattr(urllib.request, "urlopen", no_download)

    assert external.preflight.admit(SWE_PY).admitted is True
    assert not [argv for argv in external_bench.commands
                if argv[:1] == ["git"] and {"fetch", "clone", "pull", "ls-remote"} & set(argv)]


_CHILD = textwrap.dedent("""
    import json
    import external.data as data
    import external.preflight as preflight

    data.REPO_LANGUAGES.update({languages})
    calls = []

    def counting_round_trip(task, fingerprint):
        calls.append(fingerprint)
        return preflight.PreflightVerdict(
            instance_id=task.name, dataset=task.dataset, data_rev=task.data_rev, env_fingerprint=fingerprint,
            admitted=False, reason="gold_unresolved")

    preflight.round_trip = counting_round_trip
    if {other_fingerprint}:
        preflight.env_fingerprint = lambda task: "a-different-host"
    result = preflight.admit({instance_id!r})
    print(json.dumps({{"calls": len(calls), "admitted": result.admitted, "reason": result.reason}}))
""")


def test_cached_verdict_skips_round_trip(external_bench, monkeypatch: pytest.MonkeyPatch) -> None:
    external_bench.seed(SWE_PY)
    first: list[str] = []

    def stub_round_trip(task, fingerprint):
        first.append(fingerprint)
        return external.preflight.PreflightVerdict(
            instance_id=task.name, dataset=task.dataset, data_rev=task.data_rev, env_fingerprint=fingerprint,
            admitted=True, reason="admitted", gold=(3, 3), empty=(1, 3), tampered=(2, 3))

    monkeypatch.setattr(external.preflight, "round_trip", stub_round_trip)
    assert external.preflight.admit(SWE_PY).admitted is True
    assert len(first) == 1

    def child(other_fingerprint: bool) -> dict:
        code = _CHILD.format(languages=dict(external_support.REPO_LANGUAGES), other_fingerprint=other_fingerprint,
                             instance_id=SWE_PY)
        completed = run_python(code, env={"TILTH_BENCH_DATA": str(external_bench.data)})
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout.strip().splitlines()[-1])

    assert child(other_fingerprint=False) == {"calls": 0, "admitted": True, "reason": "admitted"}
    assert child(other_fingerprint=True) == {"calls": 1, "admitted": False, "reason": "gold_unresolved"}


def _variant(bench, row: dict, instance_id: str, **fields) -> str:
    """Cache a copy of ``row`` under ``instance_id`` with ``fields`` replaced."""
    bench.seed_row({**row, **fields, "instance_id": instance_id})
    return instance_id


@pytest.mark.parametrize("reason", ["gold_unresolved", "empty_resolved", "tampered_resolved"])
def test_preflight_rejects_non_discriminating(external_bench, reason: str) -> None:
    row = external_bench.row(SWE_PY)
    ops = external_support.projects()["fixture/calc"]["calc/ops.py"]
    if reason == "gold_unresolved":
        fields = {"patch": patch_for(external_bench, "fixture/calc",
                                     {"calc/ops.py": ops.replace("# Multiply two integers.", "# Multiply.")})}
    elif reason == "empty_resolved":
        fields = {"FAIL_TO_PASS": ["tests/test_ops.py::test_add"], "PASS_TO_PASS": []}
    else:
        notes = patch_for(external_bench, "fixture/calc", {"calc/notes.py": "NOTE = 1\n"})
        fields = {"patch": row["patch"] + notes}
    instance_id = _variant(external_bench, row, f"fixture__calc-{reason}", **fields)

    result = external.preflight.admit(instance_id)

    assert result.admitted is False and result.reason == reason


def test_preflight_refuses_by_reason(external_bench, monkeypatch: pytest.MonkeyPatch) -> None:
    external_bench.seed(FB_LV2)
    assert (external.preflight.admit(FB_LV2).admitted, external.preflight.admit(FB_LV2).reason) == (False, "level2")

    suffixless = FB_LV1.removesuffix(".lv1")
    external_bench.seed_row({**external_bench.row(FB_LV1), "instance_id": suffixless})
    assert external.preflight.admit(suffixless).reason == "unclassified"

    fb = external_bench.row(FB_LV1)
    broken = {**fb, "instance_id": "fixture__shapes.0a1b2c3d.test_area.00000000.lv1",
              "patch": fb["patch"].replace("def _unit():", "def _unknown_context():")}
    external_bench.seed_row(broken)
    result = external.preflight.admit(broken["instance_id"])
    assert (result.admitted, result.reason) == (False, "prepare_failed")

    swe = _variant(external_bench, external_bench.row(SWE_PY), "fixture__calc-env")
    recording = external.proc.run

    def failing_venv(argv, **kwargs):
        if list(argv[:2]) == ["uv", "venv"]:
            return subprocess.CompletedProcess(argv, 2, "", "no interpreter")
        return recording(argv, **kwargs)

    monkeypatch.setattr(external.proc, "run", failing_venv)
    result = external.preflight.admit(swe)
    assert (result.admitted, result.reason) == (False, "env_build_failed")
    assert "no interpreter" in result.detail


def test_real_key_rows_are_not_refused_before_round_trip(external_bench, monkeypatch: pytest.MonkeyPatch) -> None:
    external_bench.seed(FB_LV1, SWE_GO)
    assert json.loads(external_bench.row(FB_LV1)["repo_settings"])["docker_specs"]["run_args"]["cuda_visible_devices"]
    reached: list[str] = []

    def counting_round_trip(task, fingerprint):
        reached.append(task.name)
        return external.preflight.PreflightVerdict(
            instance_id=task.name, dataset=task.dataset, data_rev=task.data_rev, env_fingerprint=fingerprint,
            admitted=True, reason="admitted")

    monkeypatch.setattr(external.preflight, "round_trip", counting_round_trip)

    assert external.preflight.admit(FB_LV1).admitted is True
    assert external.preflight.admit(SWE_GO).admitted is True
    assert reached == [FB_LV1, SWE_GO]


# --- AC-5: preflight CLI ---


class _Cli:
    """Stubbed ``fetch`` and ``admit`` around ``preflight.main`` with fixture candidates."""

    def __init__(self, bench, monkeypatch: pytest.MonkeyPatch, refused: set[str] = frozenset()) -> None:
        self.bench = bench
        self.refused = set(refused)
        self.fetched: list[str] = []
        self.admitted: list[str] = []
        monkeypatch.setattr(external.featurebench, "PARETO12_IDS", (FB_LV1, FB_F2P_EDIT))
        monkeypatch.setattr(external.swebench_ml, "PICKS", {
            "go": (SWE_GO, "fixture__calcgo-9"),
            "rust": (SWE_RS, "fixture__calcrs-9"),
        })
        monkeypatch.setattr(external.download, "fetch", self.fetch)
        monkeypatch.setattr(external.preflight, "admit", self.admit)

    def fetch(self, dataset: str, **kwargs) -> None:
        self.fetched.append(dataset)
        for instance_id in external_support.rows(dataset):
            self.bench.seed(instance_id)
        if dataset == "swebench_ml":
            for primary, fallback in ((SWE_GO, "fixture__calcgo-9"), (SWE_RS, "fixture__calcrs-9")):
                self.bench.seed_row({**self.bench.row(primary), "instance_id": fallback})

    def admit(self, instance_id: str) -> external.preflight.PreflightVerdict:
        self.admitted.append(instance_id)
        return verdict(instance_id, instance_id not in self.refused)


def test_preflight_cli_fetches_uncached(external_bench, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cli = _Cli(external_bench, monkeypatch)

    assert external.preflight.main([]) == 0
    assert sorted(cli.fetched) == ["featurebench", "swebench_ml"]
    cli.fetched.clear()
    assert external.preflight.main([]) == 0
    assert cli.fetched == []

    panel = tmp_path / "panel.json"
    panel.write_text(json.dumps({"members": [{"id": SWE_GO, "family": "swebench_ml"},
                                             {"id": FB_LV1, "family": "featurebench"}]}))
    assert external.preflight.main(["--panel", str(panel)]) == 0
    assert cli.fetched == []

    for path in (external_bench.data / "rows").rglob("*.json"):
        path.unlink()
    assert external.preflight.main(["--panel", str(panel)]) == 0
    assert sorted(cli.fetched) == ["featurebench", "swebench_ml"]


@pytest.mark.parametrize("primary_refused", [False, True])
def test_cherry_pick_falls_back(external_bench, monkeypatch: pytest.MonkeyPatch, primary_refused: bool) -> None:
    cli = _Cli(external_bench, monkeypatch, refused={SWE_GO} if primary_refused else set())

    external.preflight.main([])

    assert ("fixture__calcgo-9" in cli.admitted) is primary_refused
    assert "fixture__calcrs-9" not in cli.admitted


@pytest.mark.parametrize(("refused", "family"), [
    ({FB_LV1, FB_F2P_EDIT}, "featurebench_admitted: 0"),
    ({SWE_GO, "fixture__calcgo-9"}, "go_admitted: 0"),
    ({SWE_RS, "fixture__calcrs-9"}, "rust_admitted: 0"),
])
def test_zero_family_admitted_reports_and_fails(external_bench, monkeypatch: pytest.MonkeyPatch,
                                                capsys: pytest.CaptureFixture[str], refused: set[str],
                                                family: str) -> None:
    _Cli(external_bench, monkeypatch, refused=refused)

    assert external.preflight.main([]) != 0
    output = capsys.readouterr()
    assert family in output.out
    assert family.split("_")[0] in output.err


def test_preflight_cli_reports_admitted(external_bench, monkeypatch: pytest.MonkeyPatch,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    _Cli(external_bench, monkeypatch, refused={FB_F2P_EDIT})

    assert external.preflight.main([]) == 0
    output = capsys.readouterr().out
    for instance_id in (FB_LV1, SWE_GO, SWE_RS):
        assert instance_id in output
    assert "gold=3/3 empty=1/3 tampered=2/3" in output
    assert "featurebench_admitted: 1" in output
    assert "go_admitted: 1" in output and "rust_admitted: 1" in output


def test_preflight_panel_form(external_bench, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                              capsys: pytest.CaptureFixture[str]) -> None:
    panel = tmp_path / "panel.json"
    panel.write_text(json.dumps({"members": [
        {"id": "gin_edit_render_context", "family": "local"},
        {"id": FB_LV1, "family": "featurebench"},
        {"id": SWE_GO, "family": "swebench_ml"},
    ]}))
    cli = _Cli(external_bench, monkeypatch)

    assert external.preflight.main(["--panel", str(panel)]) == 0
    assert sorted(cli.admitted) == sorted([FB_LV1, SWE_GO])

    cli.refused = {SWE_GO}
    assert external.preflight.main(["--panel", str(panel)]) != 0
    assert SWE_GO in capsys.readouterr().err

    cli.refused = {FB_LV1}
    assert external.preflight.main(["--panel", str(panel)]) != 0
    assert "featurebench" in capsys.readouterr().err


@pytest.mark.parametrize("stored", [{"instance_id": SWE_PY, "admitted": True}, ["admitted"],
                                    {"instance_id": SWE_PY, "admitted": True, "reason": "admitted",
                                     "dataset": "swebench_ml", "data_rev": "x", "env_fingerprint": "y",
                                     "schema": 2}])
def test_stale_schema_verdict_is_recomputed(external_bench, monkeypatch: pytest.MonkeyPatch, stored) -> None:
    external_bench.seed(SWE_PY)
    task = external.swebench_ml.load(SWE_PY, external.SWEBENCH_ML_REVISION)
    fingerprint = external.preflight.env_fingerprint(task)
    path = external.preflight.verdict_path(SWE_PY, external.SWEBENCH_ML_REVISION, fingerprint)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(stored))
    monkeypatch.setattr(external.preflight, "round_trip", lambda task, fingerprint: verdict(SWE_PY, False))

    result = external.preflight.admit(SWE_PY)

    assert (result.admitted, result.reason) == (False, "gold_unresolved")
