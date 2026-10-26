"""Pre-registered panels: schema, completeness, admission, tiers, stratified split, stamp, lock, registration."""

import json
import shutil
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import baselines
import chains
import config
import external
import external.data
import external.featurebench
import external.swebench_ml
import panels
import run
import tasks
from panel_support import (
    CHEAP, COMMITTED_PANEL, COMPLETE, EXPECTED_DEV, EXPECTED_TEST, FALLBACKS, FB_ALGORITHMS, FB_IDS, FB_NULLSPACE,
    FB_REGRESSION, GEPA_ROWS, GO_FALLBACK, GO_SLOT, RENDER, RUST_FALLBACK, RUST_SLOT, load, member, panel_run, read,
    RUN_ARGS, refusing, restratify, row_source, stamp_digest, stub_loaders, stub_task, without, write,
)

GEPA_V2_PANEL = Path(__file__).parent.parent / "panels" / "gepa-v2.json"
GEPA_V3_LONG_PANEL = Path(__file__).parent.parent / "panels" / "gepa-v3-long.json"
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
    # The stratum then alternates test, dev, test, dev, test over the five python members.
    mixed = [*members, {"id": "z-go", "family": "featurebench", "language": "go"}]
    for seed in range(20):
        split = panels.stratify(mixed, seed)
        assert split["z-go"] == "dev"
        assert [split[entry["id"]] for entry in members].count("dev") == 2
        assert [split[entry["id"]] for entry in members].count("test") == 3


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


def test_non_panel_reuse_drops_stored_stamp(prun, bench) -> None:
    assert prun.main(prun.panel(read(COMPLETE)), "--panel-split", "cheap") == 0
    bench.calls.clear()

    assert bench.main("--tasks", ",".join(CHEAP), *RUN_ARGS) == 0

    assert bench.calls == []
    reused = bench.output_rows()
    assert len(reused) == 3 and all(row["reused"] for row in reused)
    assert not any(field in row for row in reused for field in STAMP_FIELDS)


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
        load(COMPLETE, store, admit=lambda _id: pytest.fail("admission ran before the split lock"))
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


def _drop(path: tuple):
    def mutate(data: dict) -> dict:
        data = json.loads(json.dumps(data))
        del (data if len(path) == 1 else member(data, path[0]))[path[-1]]
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
    (_drop(("split_seed",)), "missing key"),
    (_set(("name",), "  "), "non-empty"),
    (_set((RENDER, "language"), 5), "must be strings"),
    (_drop((RENDER, "language")), "missing member key"),
    (_add({"id": "mwaskom__seaborn.7001ebe7.test_regression.ce8c62e2.lv2", "family": "featurebench",
           "language": "python", "split": "dev"}), "Level 1"),
], ids=["unknown-key", "unknown-member-key", "duplicate-id", "unknown-local", "extra-local", "unknown-family",
        "third-swebench", "local-language", "seed-type", "members-type", "no-seed", "empty-name", "non-string-field",
        "no-member-language", "level2"])
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


def test_panel_split_requires_panel(bench, capsys: pytest.CaptureFixture[str]) -> None:
    assert bench.main("--panel-split", "dev", *RUN_ARGS) == 2

    assert bench.calls == []
    assert "--panel-split requires --panel" in capsys.readouterr().err


def test_panel_refuses_runner_without_trajectory_sidecar(
    bench, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(panels, "load_panel", lambda *_args, **_kwargs: pytest.fail("panel loaded"))
    path = write(tmp_path, read(COMPLETE))

    assert bench.main("--panel", str(path), "--models", "sonnet5,gpt5mini", "--modes", "plain") == 2

    assert bench.calls == []
    assert "--panel requires claude or codex models" in capsys.readouterr().err


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


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _all_keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _all_keys(item)}
    return set()


def test_committed_manifests_hold_no_grader_material() -> None:
    manifests = [*sorted(COMMITTED_PANEL.parent.glob("*.json")), *sorted(COMPLETE.parent.glob("*.json"))]

    assert COMMITTED_PANEL in manifests and COMPLETE in manifests
    for path in manifests:
        assert not _all_keys(read(path)) & GRADER_KEYS, path.name


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


# --- gepa-v2: a screened pool with only dev and test ---

V2_LOCAL = ("express_edit_example_start", "rg_edit_flow_migration")
V2_SWE = (("tokio-rs__axum-734", "rust"), ("prometheus__prometheus-9248", "go"), (GO_SLOT, "go"))


def _v2_data() -> dict:
    members = [{"id": name, "family": "local", "language": config.REPOS[run.TASKS[name].repo].language, "split": "dev"}
               for name in V2_LOCAL]
    members += [{"id": name, "family": "featurebench", "language": "python", "split": "dev"}
                for name in (FB_ALGORITHMS, FB_REGRESSION)]
    members += [{"id": name, "family": "swebench_ml", "language": language, "split": "dev"} for name, language in V2_SWE]
    return restratify({"version": 2, "name": "fixture-v2", "split_seed": 11, "members": members})


def test_v2_loads_without_a_cheap_tier(tmp_path: Path, store: Path) -> None:
    data = _v2_data()
    panel = load(write(tmp_path, data), store)

    assignment = panels.stratify(data["members"], data["split_seed"])
    assert panel.cheap == ()
    assert set(panel.dev) == {name for name, split in assignment.items() if split == "dev"}
    assert set(panel.test) == {name for name, split in assignment.items() if split == "test"}
    assert panel.dev and panel.test and len(panel.members) == 7


def test_v2_refuses_a_cheap_member(tmp_path: Path, store: Path) -> None:
    data = _v2_data()
    data["members"].append({"id": CHEAP[0], "family": "local", "language": "rust", "split": "dev"})
    with pytest.raises(panels.PanelError, match=CHEAP[0]):
        load(write(tmp_path, data), store)

    data = _v2_data()
    member(data, V2_LOCAL[0])["split"] = "cheap"
    with pytest.raises(panels.PanelError, match="cheap"):
        load(write(tmp_path, data), store)


def test_v2_refuses_an_id_outside_the_pools(tmp_path: Path, store: Path) -> None:
    data = _v2_data()
    data["members"].append({"id": "sharkdp__bat-1", "family": "swebench_ml", "language": "rust", "split": "dev"})
    with pytest.raises(panels.PanelError, match="sharkdp__bat-1"):
        load(write(tmp_path, restratify(data)), store)


def test_v2_accepts_a_local_task_outside_the_v1_whitelist(tmp_path: Path, store: Path) -> None:
    assert V2_LOCAL[0] not in panels.LOCAL
    assert V2_LOCAL[0] in load(write(tmp_path, _v2_data()), store).members[0].id


def test_v2_refuses_fallback_keys(tmp_path: Path, store: Path) -> None:
    data = _v2_data()
    member(data, GO_SLOT).update(fallback_for="x", fallback_reason="y")
    with pytest.raises(panels.PanelError, match="fallback"):
        load(write(tmp_path, data), store)


def test_v2_refuses_a_missing_test_split(tmp_path: Path, store: Path) -> None:
    data = _v2_data()
    data["members"] = [entry for entry in data["members"] if entry["split"] == "dev"]
    with pytest.raises(panels.PanelError, match="test"):
        load(write(tmp_path, data), store)


def test_v2_refuses_a_hand_picked_split(tmp_path: Path, store: Path) -> None:
    data = _v2_data()
    entry = member(data, V2_LOCAL[0])
    entry["split"] = "test" if entry["split"] == "dev" else "dev"
    with pytest.raises(panels.PanelError, match="differs from panels.stratify"):
        load(write(tmp_path, data), store)


def test_v2_refuses_a_wrong_language(tmp_path: Path, store: Path) -> None:
    data = _v2_data()
    member(data, "tokio-rs__axum-734")["language"] = "go"
    with pytest.raises(panels.PanelError, match="tokio-rs__axum-734"):
        load(write(tmp_path, restratify(data)), store)


def test_a_non_integer_version_is_refused(tmp_path: Path, store: Path) -> None:
    for version in ("2", 2.0, True, 3):
        with pytest.raises(panels.PanelError, match="version"):
            load(write(tmp_path, {**read(COMPLETE), "version": version}), store)


def test_gepa_v1_split_digest_is_unchanged(store: Path) -> None:
    data = read(COMMITTED_PANEL)
    assert "version" not in data
    panel = load(COMMITTED_PANEL, store, rows=_redacted_row)

    assert panel.split_digest == stamp_digest(data)
    assert set(panel.cheap) == set(CHEAP)


def test_committed_gepa_v2_panel_loads(store: Path) -> None:
    data = read(GEPA_V2_PANEL)
    assert data["version"] == 2
    languages = {entry["id"]: entry["language"] for entry in data["members"]}
    panel = load(GEPA_V2_PANEL, store, rows=lambda member_id: {"language": languages[member_id]})

    assert len(panel.members) == 30
    assert panel.cheap == ()
    assert panel.dev and panel.test
    assignment = panels.stratify(data["members"], data["split_seed"])
    assert all(entry["split"] == assignment[entry["id"]] for entry in data["members"])


def test_committed_gepa_v3_long_panel_loads(store: Path) -> None:
    data = read(GEPA_V3_LONG_PANEL)
    assert data["version"] == 2
    languages = {entry["id"]: entry["language"] for entry in data["members"]}
    panel = load(GEPA_V3_LONG_PANEL, store, rows=lambda member_id: {"language": languages[member_id]})

    assert len(panel.members) == 20
    assert panel.cheap == ()
    assert panel.dev and panel.test
    assignment = panels.stratify(data["members"], data["split_seed"])
    assert all(entry["split"] == assignment[entry["id"]] for entry in data["members"])
    assert set(panel.dev) == {i for i, s in assignment.items() if s == "dev"}
    assert set(panel.test) == {i for i, s in assignment.items() if s == "test"}


@pytest.mark.parametrize("chain_id", ["express-edit-6", "gin-route-5"])
def test_new_gepa_v3_long_chains_compose_and_sit_in_the_panel(chain_id: str) -> None:
    chain = chains.CHAINS[chain_id]

    members = chains.parse_chain(chain.id, chain.members, tasks.TASKS)

    assert 5 <= len(members) <= 7
    assert members[-1].capability == "fix"
    assert f"chain:{chain_id}" in {entry["id"] for entry in read(GEPA_V3_LONG_PANEL)["members"]}


def test_long_pool_ids_hold_the_controls_and_stay_out_of_the_package_digest(tmp_path: Path, monkeypatch) -> None:
    controls = {"tokio-rs__tokio-4867", "burntsushi__ripgrep-2209", "caddyserver__caddy-6350",
                "hashicorp__terraform-35611", "axios__axios-5085", "uutils__coreutils-6682"}
    assert controls <= panels.long_pool_ids()
    assert not controls & set(external.swebench_ml.GEPA_V2_CANDIDATES)
    assert panels.LONG_POOL_FILE.suffix != ".py"

    package = tmp_path / "external"
    shutil.copytree(external.task.PACKAGE_DIR, package, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(external.task, "PACKAGE_DIR", package)
    before = external.task.package_digest()
    pool_file = package / "long_pool_candidates.json"
    pool_file.write_text(pool_file.read_text().replace("tokio-rs__tokio-4867", "tokio-rs__tokio-0000"))
    assert external.task.package_digest() == before
    assert panels.long_pool_ids(pool_file) != panels.long_pool_ids()
