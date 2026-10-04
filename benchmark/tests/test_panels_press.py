"""Adversarial attacks on the panel contract (Press): naming, loader routing, reused-row stamps, lock scope."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import baselines
import external.featurebench
import external.swebench_ml
import panels
import run
from panel_support import (
    COMPLETE, FB_IDS, GO_SLOT, RENDER, RUST_SLOT, load, member, panel_run, read, restratify, stamp_digest,
    stub_task, without, write,
)


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return tmp_path / "store" / baselines.STORE_FILENAME


def test_incomplete_panel_names_every_missing_member(tmp_path: Path, store: Path) -> None:
    path = write(tmp_path, restratify(without(read(COMPLETE), RENDER, RUST_SLOT, *FB_IDS)))

    with pytest.raises(panels.PanelError) as refused:
        load(path, store)

    message = str(refused.value)
    assert RENDER in message and RUST_SLOT in message and "featurebench" in message


def test_register_routes_each_family_to_its_loader(monkeypatch: pytest.MonkeyPatch, store: Path) -> None:
    routed: dict[str, list[str]] = {"featurebench": [], "swebench_ml": []}

    def loader(family: str):
        def load_task(instance_id: str, revision: str):
            routed[family].append(instance_id)
            return stub_task(instance_id, revision)
        return load_task

    monkeypatch.setattr(external.featurebench, "load", loader("featurebench"))
    monkeypatch.setattr(external.swebench_ml, "load", loader("swebench_ml"))

    load(COMPLETE, store).register(dict(run.TASKS))

    assert sorted(routed["featurebench"]) == sorted(FB_IDS)
    assert sorted(routed["swebench_ml"]) == sorted([GO_SLOT, RUST_SLOT])


def test_unsafe_featurebench_id_refused(tmp_path: Path, store: Path) -> None:
    data = read(COMPLETE)
    member(data, FB_IDS[0])["id"] = "../escape.lv1"
    with pytest.raises(panels.PanelError, match="Level 1"):
        load(write(tmp_path, data), store)


def test_reused_output_takes_current_stamp_and_store_keeps_original(bench, monkeypatch: pytest.MonkeyPatch,
                                                                    tmp_path: Path) -> None:
    prun = panel_run(bench, monkeypatch, tmp_path)
    data = read(COMPLETE)
    original = {"panel_name": data["name"], "panel_split_digest": stamp_digest(data), "panel_split": "dev"}
    seeded = bench.row("rg_search_dispatch", "plain", 0, **original)
    bench.seed(seeded)

    assert prun.main(prun.panel(data), "--panel-split", "cheap") == 0

    output = {row["task"]: row for row in bench.output_rows()}
    assert output["rg_search_dispatch"]["reused"] is True
    assert output["rg_search_dispatch"]["panel_split"] == "cheap"
    stored = [row for row in bench.stored_rows() if row["task"] == "rg_search_dispatch"]
    assert stored[0]["panel_split"] == "dev"


def test_lock_holds_when_a_newer_row_matches(store: Path) -> None:
    data = read(COMPLETE)
    for digest in ("0" * 64, stamp_digest(data)):
        baselines.store({"run_key": digest, "panel_name": data["name"], "panel_split_digest": digest}, path=store)
    before = store.read_bytes()

    with pytest.raises(panels.PanelError, match="split changed"):
        load(COMPLETE, store)
    assert store.read_bytes() == before


def test_panel_rows_flow_into_analysis_once(tmp_path: Path) -> None:
    import analyze

    row = {"task": "t", "mode": "baseline", "correct": True, "panel_name": "p", "contaminated": True}
    path = tmp_path / "results.jsonl"
    path.write_text(json.dumps(row) + "\n")

    [loaded] = analyze.load_results(path)

    assert loaded["correct"] is False
    assert {key: value for key, value in loaded.items() if key != "correct"} == {
        key: value for key, value in row.items() if key != "correct"}
