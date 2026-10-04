"""Pre-registered panels: schema, completeness, admission, tiers, stratified split, stamp, lock, registration."""

import hashlib
import json
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import baselines
import external
import external.data
import external.featurebench
import external.swebench_ml
import panels
import run
from panel_support import (
    CHEAP, COMMITTED_PANEL, COMPLETE, EXPECTED_DEV, EXPECTED_TEST, FALLBACKS, FB_ALGORITHMS, FB_IDS, FB_NULLSPACE,
    FB_REGRESSION, GEPA_ROWS, GO_FALLBACK, GO_SLOT, RENDER, RUST_FALLBACK, RUST_SLOT, load, member, panel_run, read,
    refusing, restratify, row_source, stamp_digest, stub_loaders, stub_task, without, write,
)

STAMP_FIELDS = ("panel_name", "panel_split_digest", "panel_split")
REDACTED_KEYS = {"instance_id", "repo", "base_commit", "language"}
GRADER_KEYS = {"patch", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS", "problem_statement"}


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return tmp_path / "store" / baselines.STORE_FILENAME


@pytest.fixture
def prun(bench, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    return panel_run(bench, monkeypatch, tmp_path)


# --- AC-1: load, split, select ---


def test_complete_panel_loads_and_splits(store: Path) -> None:
    panel = load(COMPLETE, store)

    assert panel.name == "fixture-primaries"
    assert set(panel.cheap) == set(CHEAP)
    assert set(panel.dev) == EXPECTED_DEV
    assert set(panel.test) == EXPECTED_TEST
    assert len(panel.members) == 9


def test_panel_split_selects_members(prun) -> None:
    assert prun.main(prun.panel(read(COMPLETE)), "--panel-split", "dev") == 0

    assert sorted(prun.called) == sorted(EXPECTED_DEV)
    assert sorted(prun.loaded) == sorted([*FB_IDS, GO_SLOT, RUST_SLOT])


# --- AC-2: completeness and admission ---


@pytest.mark.parametrize("refused", [FB_REGRESSION, GO_SLOT, RUST_SLOT])
def test_unadmitted_external_member_refused(prun, monkeypatch: pytest.MonkeyPatch,
                                            capsys: pytest.CaptureFixture[str], refused: str) -> None:
    monkeypatch.setattr(external.preflight, "admit", refusing(refused, reason="empty_resolved"))

    assert prun.main(prun.panel(read(COMPLETE))) != 0

    assert prun.called == []
    error = capsys.readouterr().err
    assert refused in error and "empty_resolved" in error


def test_direct_loader_refuses_incomplete_panel(tmp_path: Path, store: Path) -> None:
    missing = write(tmp_path, restratify(without(read(COMPLETE), RENDER)))
    with pytest.raises(panels.PanelError, match=RENDER):
        load(missing, store)

    with pytest.raises(panels.PanelError, match=f"{FB_NULLSPACE}.*tampered_resolved"):
        load(COMPLETE, store, admit=refusing(FB_NULLSPACE))


def test_unadmitted_members_are_all_named(store: Path) -> None:
    with pytest.raises(panels.PanelError) as refused:
        load(COMPLETE, store, admit=refusing(FB_ALGORITHMS, RUST_SLOT))
    assert FB_ALGORITHMS in str(refused.value) and RUST_SLOT in str(refused.value)


def test_admission_lookup_error_is_a_refusal(store: Path) -> None:
    def uncached(instance_id: str):
        raise LookupError(f"{instance_id} has no cached row")

    with pytest.raises(panels.PanelError, match="no cached row"):
        load(COMPLETE, store, admit=uncached)


# --- AC-3: fallbacks ---


def test_fallback_panel_loads(store: Path) -> None:
    primaries = load(COMPLETE, store)
    fallbacks = load(FALLBACKS, store)

    assert GO_FALLBACK in fallbacks.dev and RUST_FALLBACK in fallbacks.test
    assert fallbacks.split_digest != primaries.split_digest


def _both_in_slot(data: dict) -> dict:
    extra = {"id": GO_FALLBACK, "family": "swebench_ml", "language": "go", "split": "test",
             "fallback_for": GO_SLOT, "fallback_reason": "gold_unresolved"}
    return {**data, "members": [*data["members"], extra]}


def _wrong_primary(data: dict) -> dict:
    data = without(data, RUST_SLOT)
    extra = {"id": RUST_FALLBACK, "family": "swebench_ml", "language": "rust", "split": "test",
             "fallback_for": GO_SLOT, "fallback_reason": "env_build_failed"}
    return {**data, "members": [*data["members"], extra]}


def _drop_key(key: str):
    def mutate(data: dict) -> dict:
        data = json.loads(json.dumps(data))
        del member(data, GO_FALLBACK)[key]
        return data
    return mutate


def _empty_reason(data: dict) -> dict:
    data = json.loads(json.dumps(data))
    member(data, GO_FALLBACK)["fallback_reason"] = "  "
    return data


@pytest.mark.parametrize(("source", "mutate", "fault"), [
    (COMPLETE, _both_in_slot, "both"),
    (COMPLETE, _wrong_primary, "fallback_for"),
    (FALLBACKS, _drop_key("fallback_for"), "fallback_for"),
    (FALLBACKS, _drop_key("fallback_reason"), "fallback_reason"),
    (FALLBACKS, _empty_reason, "fallback_reason"),
], ids=["primary-and-fallback", "wrong-primary", "no-fallback-for", "no-fallback-reason", "empty-reason"])
def test_bad_fallback_refused(tmp_path: Path, store: Path, source: Path, mutate, fault: str) -> None:
    with pytest.raises(panels.PanelError, match=fault):
        load(write(tmp_path, mutate(read(source))), store)


def test_primary_must_not_carry_fallback_fields(tmp_path: Path, store: Path) -> None:
    data = read(COMPLETE)
    member(data, GO_SLOT)["fallback_reason"] = "not a fallback"
    with pytest.raises(panels.PanelError, match=GO_SLOT):
        load(write(tmp_path, data), store)


# --- AC-4: tiers, stratification, digest ---


def test_stratify_by_family() -> None:
    members = read(COMPLETE)["members"]
    first = panels.stratify(members, 7)

    assert first == panels.stratify(members, 7)
    assert {name for name, split in first.items() if split == "dev"} == EXPECTED_DEV
    assert {name for name, split in first.items() if split == "test"} == EXPECTED_TEST
    assert not set(CHEAP) & set(first)
    assert first[GO_SLOT] != first[RUST_SLOT]
    for family in ("featurebench", "swebench_ml"):
        sides = {first[entry["id"]] for entry in members if entry["family"] == family}
        assert sides == {"dev", "test"}


def test_stratify_orders_by_language_then_seeded_hash() -> None:
    members = [{"id": f"m{index}", "family": "featurebench", "language": "python"} for index in range(5)]
    assignment = panels.stratify(members, 3)

    assert list(assignment.values()).count("dev") == 3
    assert panels.stratify(members, 3) == assignment
    assert any(panels.stratify(members, seed) != assignment for seed in range(4, 20))

    # Language sorts first: the lone go member leads its stratum on every seed, so it is always dev.
    mixed = [*members, {"id": "z-go", "family": "featurebench", "language": "go"}]
    for seed in range(20):
        ordered = sorted(mixed, key=lambda entry: (entry["language"],
                                                   hashlib.sha256(f"{seed}:{entry['id']}".encode()).hexdigest()))
        expected = {entry["id"]: "dev" if index % 2 == 0 else "test" for index, entry in enumerate(ordered)}
        assert panels.stratify(mixed, seed) == expected
        assert expected["z-go"] == "dev"


@pytest.mark.parametrize(("member_id", "split"), [("rg_search_dispatch", "test"), (RENDER, "cheap"),
                                                  ("gin_servehttp_flow", "dev")])
def test_cheap_tier_is_dev_only(tmp_path: Path, store: Path, member_id: str, split: str) -> None:
    data = read(COMPLETE)
    member(data, member_id)["split"] = split
    with pytest.raises(panels.PanelError, match=member_id):
        load(write(tmp_path, data), store)


def test_handpicked_split_refused(tmp_path: Path, store: Path) -> None:
    data = read(COMPLETE)
    member(data, FB_ALGORITHMS)["split"], member(data, FB_REGRESSION)["split"] = "test", "dev"
    with pytest.raises(panels.PanelError, match="stratify"):
        load(write(tmp_path, data), store)


def test_split_digest_scope(tmp_path: Path, store: Path) -> None:
    baseline = load(COMPLETE, store).split_digest
    data = read(COMPLETE)
    assert baseline == stamp_digest(data)

    reordered = {**data, "members": list(reversed(data["members"]))}
    assert load(write(tmp_path, reordered, "reordered.json"), store).split_digest == baseline

    fallbacks = read(FALLBACKS)
    member(fallbacks, GO_FALLBACK)["fallback_reason"] = "a different reason text"
    assert load(write(tmp_path, fallbacks, "reason.json"), store).split_digest == load(FALLBACKS, store).split_digest

    swapped = json.loads(json.dumps(data))
    member(swapped, FB_NULLSPACE)["id"] = "pydantic__pydantic.e1dcaf9e.test_deprecated_fields.40a2ec54.lv1"
    swapped_path = write(tmp_path, restratify(swapped), "swapped.json")
    assert load(swapped_path, store).split_digest != baseline


# --- AC-5: stamp, store read, split lock ---


def test_stored_rows_carry_panel_stamp(prun, bench) -> None:
    assert prun.main(prun.panel(read(COMPLETE))) == 0

    stored = list(baselines.rows(bench.store_path, panel_name="fixture-primaries"))
    assert len(stored) == 9
    for row in stored:
        assert all(row.get(name) for name in STAMP_FIELDS), row["task"]
        unstamped = {key: value for key, value in row.items() if key not in STAMP_FIELDS}
        restamped = {**row, "panel_name": "other", "panel_split_digest": "0" * 64, "panel_split": "test"}
        assert baselines.run_key(row) == baselines.run_key(unstamped) == baselines.run_key(restamped)
        assert row["run_key"] == baselines.run_key(row)


def test_stamp_values_match_manifest(prun, bench) -> None:
    data = read(COMPLETE)
    assert prun.main(prun.panel(data)) == 0
    digest = stamp_digest(data)

    for rows in (bench.stored_rows(), bench.output_rows()):
        by_task = {row["task"]: row for row in rows}
        assert set(by_task) == {entry["id"] for entry in data["members"]}
        for row in rows:
            assert row["panel_name"] == "fixture-primaries"
            assert row["panel_split_digest"] == digest
            assert row["panel_split"] == member(data, row["task"])["split"]
        assert (by_task["rg_search_dispatch"]["panel_split"], by_task[RENDER]["panel_split"],
                by_task[RUST_SLOT]["panel_split"]) == ("cheap", "dev", "test")


def test_failed_cell_row_carries_stamp(prun, bench) -> None:
    def failing(_stream: Path) -> float:
        raise RuntimeError("runner failed")

    bench.runner(failing)
    assert prun.main(prun.panel(read(COMPLETE)), "--panel-split", "test") == 0

    stored = bench.stored_rows()
    assert stored and all(row.get("error") for row in stored)
    assert {row["panel_split"] for row in stored} == {"test"}
    assert all(row["panel_name"] == "fixture-primaries" for row in stored)


def test_stamp_refuses_a_non_member(store: Path) -> None:
    panel = load(COMPLETE, store)
    assert panel.stamp(FB_REGRESSION) == {"panel_name": "fixture-primaries",
                                          "panel_split_digest": panel.split_digest, "panel_split": "test"}
    with pytest.raises(panels.PanelError, match="find_definition"):
        panel.stamp("find_definition")


def test_reused_row_output_carries_stamp(prun, bench) -> None:
    data = read(COMPLETE)
    path = prun.panel(data)
    assert prun.main(path, "--panel-split", "cheap") == 0
    bench.calls.clear()

    assert prun.main(path, "--panel-split", "cheap") == 0

    assert bench.calls == []
    reused = bench.output_rows()
    assert len(reused) == 3 and all(row["reused"] for row in reused)
    for row in reused:
        assert row["panel_name"] == "fixture-primaries"
        assert row["panel_split_digest"] == stamp_digest(data)
        assert row["panel_split"] == "cheap"


def test_baselines_rows_is_read_only_filter(tmp_path: Path, store: Path) -> None:
    assert list(baselines.rows(tmp_path / "missing.jsonl")) == []
    stored = [{"run_key": "a", "panel_name": "p"}, {"run_key": "b", "panel_name": "q"},
              {"run_key": "c"}, {"run_key": "d", "panel_name": "p", "error": "timeout"}]
    for row in stored:
        baselines.store(row, path=store)
    before = store.read_bytes()

    assert [row["run_key"] for row in baselines.rows(store, panel_name="p")] == ["a", "d"]
    assert [row["run_key"] for row in baselines.rows(store)] == ["a", "b", "c", "d"]
    assert store.read_bytes() == before


def _seed_stamped(bench, digest: str, **fields: object) -> None:
    bench.seed(bench.row("cell_a", "plain", 0, panel_name="fixture-primaries", panel_split_digest=digest,
                         panel_split="dev", **fields))


def test_split_change_after_results_refused(prun, bench, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_stamped(bench, "0" * 64)

    assert prun.main(prun.panel(read(COMPLETE))) != 0

    assert prun.called == []
    assert "split" in capsys.readouterr().err


def test_unchanged_split_proceeds(prun, bench) -> None:
    _seed_stamped(bench, stamp_digest(read(COMPLETE)))

    assert prun.main(prun.panel(read(COMPLETE))) == 0
    assert len(prun.called) == 9


def test_first_run_proceeds(prun, bench) -> None:
    assert not bench.store_path.exists()
    assert prun.main(prun.panel(read(COMPLETE))) == 0
    assert len(prun.called) == 9


def test_other_panel_rows_do_not_lock(prun, bench) -> None:
    bench.seed(bench.row("cell_a", "plain", 0, panel_name="another-panel", panel_split_digest="0" * 64))
    assert prun.main(prun.panel(read(COMPLETE))) == 0


def test_error_rows_do_not_lock(prun, bench) -> None:
    _seed_stamped(bench, "0" * 64, error="timeout", timed_out=True)
    _seed_stamped(bench, "1" * 64, infra="quota", error="infra:quota: limit")

    assert prun.main(prun.panel(read(COMPLETE))) == 0
    assert len(prun.called) == 9


def test_direct_loader_refuses_changed_split(store: Path) -> None:
    baselines.store({"run_key": "k", "panel_name": "fixture-primaries", "panel_split_digest": "0" * 64}, path=store)
    with pytest.raises(panels.PanelError, match="split"):
        load(COMPLETE, store)
    assert load(FALLBACKS, store).name == "fixture-fallbacks"


# --- AC-6: schema, languages, flag guard ---


def _set(path: tuple, value: object):
    def mutate(data: dict) -> dict:
        data = json.loads(json.dumps(data))
        if len(path) == 1:
            data[path[0]] = value
        else:
            member(data, path[0])[path[1]] = value
        return data
    return mutate


def _add(entry: dict):
    def mutate(data: dict) -> dict:
        return {**data, "members": [*data["members"], entry]}
    return mutate


@pytest.mark.parametrize(("mutate", "fault"), [
    (_set(("notes",), "x"), "notes"),
    (_set(("rg_search_dispatch", "weight"), 2), "weight"),
    (_add({"id": RENDER, "family": "local", "language": "go", "split": "dev"}), "duplicate"),
    (_add({"id": "no_such_task", "family": "local", "language": "go", "split": "dev"}), "no_such_task"),
    (_add({"id": "gin_radix_tree", "family": "local", "language": "go", "split": "dev"}), "gin_radix_tree"),
    (_set((FB_REGRESSION, "family"), "swebench"), "swebench"),
    (_add({"id": "caddyserver__caddy-5870", "family": "swebench_ml", "language": "go", "split": "dev"}),
     "caddyserver__caddy-5870"),
    (_set(("rg_search_dispatch", "language"), "go"), "rg_search_dispatch"),
    (_set(("split_seed",), "7"), "split_seed"),
    (_set(("members",), {}), "members"),
    (_add({"id": "mwaskom__seaborn.7001ebe7.test_regression.ce8c62e2.lv2", "family": "featurebench",
           "language": "python", "split": "dev"}), "Level 1"),
], ids=["unknown-key", "unknown-member-key", "duplicate-id", "unknown-local", "extra-local", "unknown-family",
        "third-swebench", "local-language", "seed-type", "members-type", "level2"])
def test_malformed_panel_refused(tmp_path: Path, store: Path, mutate, fault: str) -> None:
    with pytest.raises(panels.PanelError, match=fault):
        load(write(tmp_path, mutate(read(COMPLETE))), store)


def _row_with_language(language: str):
    def rows(instance_id: str) -> dict:
        return {**row_source(instance_id), **({"language": language} if instance_id == GO_SLOT else {})}
    return rows


@pytest.mark.parametrize(("member_id", "declared", "rows"), [
    (FB_ALGORITHMS, "go", None),
    (GO_SLOT, "rust", None),
    (RUST_SLOT, "go", None),
    (GO_SLOT, "go", _row_with_language("rust")),
], ids=["featurebench-go", "go-slot-rust", "rust-slot-go", "row-disagrees"])
def test_external_language_refused(tmp_path: Path, store: Path, member_id: str, declared: str, rows) -> None:
    data = read(COMPLETE)
    member(data, member_id)["language"] = declared
    kwargs = {"rows": rows} if rows else {}
    with pytest.raises(panels.PanelError, match=member_id):
        load(write(tmp_path, data), store, **kwargs)


@pytest.mark.parametrize("flags", [("--tasks", "find_definition"), ("--repos", "gin")])
def test_panel_excludes_task_and_repo_filters(prun, flags: tuple[str, str]) -> None:
    assert prun.main(prun.panel(read(COMPLETE)), *flags) != 0
    assert prun.called == []


# --- AC-7: the committed panel ---


def _redacted_row(instance_id: str) -> dict:
    return json.loads((GEPA_ROWS / f"{instance_id}.json").read_text())


def test_committed_panel_is_complete(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store: Path) -> None:
    def no_network(*_args, **_kwargs):
        raise AssertionError("the committed panel load reached the network")

    def no_host_data(instance_id: str):
        raise AssertionError(f"the committed panel load read host data for {instance_id}")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(external.data, "cached_row", no_host_data)
    monkeypatch.setenv("TILTH_BENCH_DATA", str(tmp_path / "no-host-data"))
    data = read(COMMITTED_PANEL)

    panel = load(COMMITTED_PANEL, store, rows=_redacted_row)

    families = {entry["family"] for entry in data["members"]}
    assert families == {"local", "featurebench", "swebench_ml"}
    assert set(panel.cheap) == set(CHEAP)
    assert RENDER in panel.dev
    for entry in data["members"]:
        if entry["family"] != "local":
            assert entry["language"] == external.language_of(_redacted_row(entry["id"]))
    slots = {entry["language"] for entry in data["members"] if entry["family"] == "swebench_ml"}
    assert slots == {"go", "rust"}
    assignment = panels.stratify(data["members"], data["split_seed"])
    assert all(entry["split"] == assignment.get(entry["id"], "cheap") for entry in data["members"])
    assert not (tmp_path / "no-host-data").exists()


def test_committed_rows_hold_no_grader_material() -> None:
    external_ids = {entry["id"] for entry in read(COMMITTED_PANEL)["members"] if entry["family"] != "local"}
    files = sorted(GEPA_ROWS.iterdir())

    assert {path.stem for path in files} == external_ids
    for path in files:
        row = json.loads(path.read_text())
        assert set(row) <= REDACTED_KEYS, path.name
        assert not set(row) & GRADER_KEYS, path.name
        assert row["instance_id"] == path.stem


# --- AC-9: registration ---


def test_register_is_idempotent(monkeypatch: pytest.MonkeyPatch, store: Path) -> None:
    loaded = stub_loaders(monkeypatch)
    panel = load(COMPLETE, store)
    registry = dict(run.TASKS)

    panel.register(registry)
    snapshot = dict(registry)
    assert set(snapshot) - set(run.TASKS) == {*FB_IDS, GO_SLOT, RUST_SLOT}
    assert sorted(loaded) == sorted([*FB_IDS, GO_SLOT, RUST_SLOT])

    panel.register(registry)

    assert registry.keys() == snapshot.keys()
    assert all(registry[name] is snapshot[name] for name in snapshot)


def test_register_refuses_local_name_collision(monkeypatch: pytest.MonkeyPatch, store: Path) -> None:
    def loader(instance_id: str, revision: str):
        return stub_task(instance_id, revision) if instance_id != FB_ALGORITHMS else _named("gin_servehttp_flow", revision)

    monkeypatch.setattr(external.featurebench, "load", loader)
    monkeypatch.setattr(external.swebench_ml, "load", loader)
    with pytest.raises(panels.PanelError, match="gin_servehttp_flow"):
        load(COMPLETE, store).register(dict(run.TASKS))


def _named(name: str, revision: str) -> external.ExternalTask:
    task = stub_task(FB_ALGORITHMS, revision)
    task.row["instance_id"] = name
    return task


def test_register_refuses_changed_digest(monkeypatch: pytest.MonkeyPatch, store: Path) -> None:
    stub_loaders(monkeypatch)
    panel = load(COMPLETE, store)
    registry = dict(run.TASKS)
    panel.register(registry)

    def changed(instance_id: str, revision: str):
        return stub_task(instance_id, revision, problem="A different problem statement.\n")

    monkeypatch.setattr(external.swebench_ml, "load", changed)
    with pytest.raises(panels.PanelError, match=GO_SLOT):
        panel.register(registry)


def test_register_wraps_loader_errors(monkeypatch: pytest.MonkeyPatch, store: Path) -> None:
    stub_loaders(monkeypatch)

    def uncached(instance_id: str, revision: str):
        raise LookupError(f"{instance_id} has no cached row")

    monkeypatch.setattr(external.swebench_ml, "load", uncached)
    with pytest.raises(panels.PanelError, match=f"{GO_SLOT}.*no cached row"):
        load(COMPLETE, store).register(dict(run.TASKS))
