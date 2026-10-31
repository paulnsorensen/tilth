"""Tests for the evolve diagnosis wiring: attribution records, the weighted selector, soft hypotheses, replay."""

import json
import random
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import run
from evolve import attribution as cost_attribution
from evolve import candidate as candidates
from evolve import cli, diagnosis, engine, proposer, replay
from evolve_support import CHEAP, DEV, TEST, build, child, log_text, main, result_stream, seed_candidate
from test_evolve import keep_builds, kept, patch_for, ready, world  # noqa: F401  (world is a fixture)

READ_MD = "prompts/tools/read.md"
SRC = candidates.SRC_PATCH


def dominant(component: str, chars: float = 900.0, cause: str = "wide_reads") -> dict:
    return {"cause": cause, "excess_chars_per_cell": chars, "evidence": f"{cause} adds {chars} chars",
            "component_hint": component}


def summary(chars: float = 1000.0, *, tool: str = "tilth_read", calls: float = 2.0, errors: float = 0.0,
            dominant_: list[dict] | None = None) -> dict:
    return {
        "coverage": {"candidate_cells": 2, "baseline_cells": 2},
        "per_tool": [{"tool": tool, "arm": "candidate", "calls_per_cell": calls, "result_chars_per_cell": chars,
                      "mean_result_chars": chars / calls, "error_rate": errors}],
        "call_shapes": [], "errors": [], "dominant": dominant_ or [],
    }


def hypothesis_text(**overrides) -> str:
    body = {"metric": "result_chars_per_cell", "scope": "tilth_read", "direction": "decrease",
            "tolerance": 0.1, "invariants": ["accuracy does not drop"], **overrides}
    return f"<hypothesis>{json.dumps(body)}</hypothesis>"


# --- prompts ---


def test_headers_carry_the_diagnosis_sentence_and_keep_the_objective() -> None:
    for header in (engine.REFLECTION_HEADER, proposer.PROPOSER_HEADER):
        assert diagnosis.DIAGNOSIS_NOTE in header
        assert candidates.OBJECTIVE in header
        assert "<hypothesis>" in header
    assert engine.REFLECTION_HEADER.index("</new_text>") < engine.REFLECTION_HEADER.index("<hypothesis>")


# --- compact attribution record ---


def test_compact_stays_under_the_cap_with_evidence_and_causes() -> None:
    big = summary(dominant_=[dominant(READ_MD, cause=f"cause_{i}") for i in range(6)])
    big["call_shapes"] = [{"shape": "x" * 300, "n": i} for i in range(80)]
    big["errors"] = [{"tool": "tilth_write", "message": "y" * 300} for _ in range(80)]

    view = diagnosis.compact(big)

    assert len(json.dumps(view, sort_keys=True)) <= diagnosis.ATTRIBUTION_CAP
    assert set(view) == {"coverage", "dominant", "call_shapes", "errors"}
    assert view["dominant"] and view["dominant"][0]["evidence"]
    assert len(view["dominant"]) <= diagnosis.DOMINANT_SHOWN


# --- dispatcher records ---


def paid_entry(**extra) -> dict:
    return {"stage": "paid", "task_scores": {"dev_a": 1.0}, "records": [], **extra}


def test_dispatcher_sends_one_attribution_and_one_source_map_per_candidate() -> None:
    seen: dict[str, list[dict]] = {}
    view = diagnosis.compact(summary(dominant_=[dominant(READ_MD)]))
    dispatcher = engine.Dispatcher(
        engine.StopState(),
        reflect=lambda name, text, records: seen.setdefault(name, records) and text,
        propose=lambda candidate, records: "patch",
    )
    dispatcher(seed_candidate(), {READ_MD: [paid_entry(attribution=view)] * 3}, [READ_MD])

    records = seen[READ_MD]
    assert [r for r in records if "attribution" in r] == [{"attribution": view}]
    assert len([r for r in records if "source_map" in r]) == 1
    assert all("attribution" not in r.get("scores", {}) for r in records)


def test_dispatcher_sends_no_diagnosis_without_attribution() -> None:
    seen: dict[str, list[dict]] = {}
    dispatcher = engine.Dispatcher(
        engine.StopState(),
        reflect=lambda name, text, records: seen.setdefault(name, records) and text,
        propose=lambda candidate, records: "patch",
    )
    dispatcher(seed_candidate(), {READ_MD: [paid_entry()]}, [READ_MD])

    assert not [r for r in seen[READ_MD] if "attribution" in r or "source_map" in r]


def test_dispatcher_hands_extra_records_to_reflection_and_proposer() -> None:
    seen: dict[str, list[dict]] = {}
    dispatcher = engine.Dispatcher(
        engine.StopState(),
        reflect=lambda name, text, records: seen.setdefault(name, records) and text,
        propose=lambda candidate, records: seen.setdefault(SRC, records) and "patch",
        extra_records=lambda candidate: [{"replay": [{"task": "dev_a"}]}],
    )
    dispatcher(seed_candidate(), {READ_MD: [paid_entry()], SRC: [paid_entry()]}, [READ_MD, SRC])

    for name in (READ_MD, SRC):
        assert {"replay": [{"task": "dev_a"}]} in seen[name]


# --- weighted selector ---


def run_selector(seed: int, hint: str, proposals: int) -> tuple[list[str], list[str]]:
    logs: list[str] = []
    parent = seed_candidate()
    selector = diagnosis.WeightedSelector(lambda candidate: [dominant(hint)], logs.append, random.Random(seed))
    return [selector(None, None, None, 0, parent)[0] for _ in range(proposals)], logs


@pytest.mark.parametrize("hint", [READ_MD, "prompts/mcp.md"])
def test_selector_picks_src_patch_within_two_proposals_whatever_the_hints(hint: str) -> None:
    for seed in range(40):
        picks, _logs = run_selector(seed, hint, 3)
        assert SRC in picks[:diagnosis.SRC_PATCH_BY_PROPOSAL], (seed, picks)


def test_selector_never_leaves_a_component_out_for_more_than_max_unselected_proposals() -> None:
    assert diagnosis.MAX_UNSELECTED == 6
    names = list(seed_candidate())
    for seed in range(20):
        picks, _logs = run_selector(seed, READ_MD, 80)
        for name in names:
            gap = 0
            for pick in picks:
                gap = 0 if pick == name else gap + 1
                assert gap <= diagnosis.MAX_UNSELECTED, (seed, name, picks)


def test_selector_favours_the_hinted_component() -> None:
    # Measured over seeds 0-19 with 300 proposals each at MAX_UNSELECTED 6: the hinted component takes 31% of the
    # turns, the same component unhinted takes 17%, and a uniform choice among five would give 20%.
    def share(hint: str) -> float:
        picks = [pick for seed in range(20) for pick in run_selector(seed, hint, 300)[0]]
        return picks.count(READ_MD) / len(picks)

    assert share(READ_MD) >= 0.29
    assert share("prompts/mcp.md") <= 0.18


def test_selector_without_attribution_still_returns_one_component() -> None:
    logs: list[str] = []
    selector = diagnosis.WeightedSelector(lambda candidate: None, logs.append, random.Random(3))
    parent = seed_candidate()
    picks = [selector(None, None, None, 0, parent) for _ in range(12)]

    assert all(len(pick) == 1 and pick[0] in parent for pick in picks)
    assert any("uniform choice, no attribution" in line for line in logs)


def test_selector_logs_each_selection_with_its_evidence_and_reason() -> None:
    picks, logs = run_selector(2, READ_MD, 4)

    assert len(logs) == 4
    for index, (pick, line) in enumerate(zip(picks, logs, strict=True), start=1):
        assert line.startswith(f"selector: proposal {index} ")
        assert f"-> {pick}:" in line
        assert "wide_reads adds 900.0 chars" not in line
        assert "wide_reads -> prompts/tools/read.md (900.0 chars/cell)" in line
        assert any(reason in line for reason in ("weighted choice", "deadline"))


def test_selector_is_wired_into_gepa_and_logs_to_the_run_log(world) -> None:  # noqa: F811
    evo = ready(world)
    pick = evo.selector(None, None, None, 0, evo.seed)

    assert len(pick) == 1 and pick[0] in evo.seed
    assert f"selector: proposal 1 parent {candidates.short_id(evo.seed)} -> {pick[0]}:" in log_text(world)


# --- hypotheses ---


def test_parse_hypothesis_reads_the_block_and_rejects_malformed_ones() -> None:
    assert diagnosis.parse_hypothesis("no block here") is None
    parsed = diagnosis.parse_hypothesis(f"text {hypothesis_text()} more")
    assert parsed == {"metric": "result_chars_per_cell", "scope": "tilth_read", "direction": "decrease",
                      "tolerance": 0.1, "invariants": ["accuracy does not drop"]}
    for bad in (hypothesis_text(metric="vibes"), hypothesis_text(direction="sideways"),
                hypothesis_text(tolerance="big"), hypothesis_text(invariants="all"),
                "<hypothesis>not json</hypothesis>"):
        with pytest.raises(ValueError):
            diagnosis.parse_hypothesis(bad)


@pytest.mark.parametrize(("parent", "child_", "direction", "status"), [
    (1000.0, 700.0, "decrease", "met"),
    (1000.0, 980.0, "decrease", "unmet"),
    (1000.0, 1200.0, "decrease", "unmet"),
    (1000.0, 1300.0, "increase", "met"),
])
def test_judge_compares_the_claim_with_the_measured_change(parent, child_, direction, status) -> None:
    claim = diagnosis.parse_hypothesis(hypothesis_text(direction=direction))
    result, observed = diagnosis.judge(claim, summary(parent), summary(child_))

    assert result == status
    assert observed["parent"] == parent and observed["child"] == child_


def test_judge_is_unmeasurable_without_a_summary_or_a_baseline_figure() -> None:
    claim = diagnosis.parse_hypothesis(hypothesis_text())
    assert diagnosis.judge(claim, None, summary())[0] == "unmeasurable"
    assert diagnosis.judge(claim, summary(), None)[0] == "unmeasurable"
    assert diagnosis.judge(claim, summary(0.0), summary())[0] == "unmeasurable"
    assert diagnosis.judge(diagnosis.parse_hypothesis(hypothesis_text(scope="tilth_write")),
                           summary(), summary())[0] == "unmeasurable"


def test_hypothesis_book_stores_by_parent_and_child_id_and_settles_once(tmp_path: Path) -> None:
    logs: list[str] = []
    book = diagnosis.HypothesisBook(tmp_path / "run" / "hypotheses.jsonl", logs.append)
    summaries = {"p" * 64: summary(1000.0), "c" * 64: summary(500.0)}

    book.offer(READ_MD, f"</new_text> {hypothesis_text()}")
    book.offer(SRC, "</new_text> prose only")
    book.attach("p" * 64, "c" * 64)
    stored = [json.loads(line) for line in (tmp_path / "run" / "hypotheses.jsonl").read_text().splitlines()]

    assert [(r["kind"], r["parent"], r["child"], r["component"]) for r in stored] == [
        ("hypothesis", "p" * 64, "c" * 64, READ_MD)]
    assert book.settle("c" * 64, summaries.get) == ["met"]
    assert book.settle("c" * 64, summaries.get) == []
    outcome = json.loads((tmp_path / "run" / "hypotheses.jsonl").read_text().splitlines()[-1])
    assert outcome["kind"] == "outcome" and outcome["status"] == "met"
    assert book.feedback() == [{"hypothesis_feedback": [
        {"parent": "p" * 12, "child": "c" * 12, "component": READ_MD, "hypothesis": stored[0]["hypothesis"],
         "status": "met", "observed": outcome["observed"]}]}]


def test_hypothesis_book_skips_a_malformed_block_and_an_unchanged_candidate(tmp_path: Path) -> None:
    logs: list[str] = []
    book = diagnosis.HypothesisBook(tmp_path / "hypotheses.jsonl", logs.append)

    book.offer(READ_MD, hypothesis_text(metric="vibes"))
    book.attach("a" * 64, "b" * 64)
    book.offer(READ_MD, hypothesis_text())
    book.attach("a" * 64, "a" * 64)

    assert not (tmp_path / "hypotheses.jsonl").exists()
    assert any("ignored" in line for line in logs)
    assert book.feedback() == []


def test_dispatcher_keys_the_hypothesis_by_ids_without_changing_the_content_id(tmp_path: Path) -> None:
    book = diagnosis.HypothesisBook(tmp_path / "hypotheses.jsonl", lambda line: None)
    parent = seed_candidate()
    new_text = parent[READ_MD] + " shorter"

    def reflect(name: str, text: str, records: list[dict]) -> str:
        book.offer(name, f"</new_text> {hypothesis_text()}")
        return new_text

    dispatcher = engine.Dispatcher(engine.StopState(), reflect=reflect, propose=lambda c, r: "", hypotheses=book)
    proposal = dispatcher(parent, {READ_MD: [paid_entry()]}, [READ_MD])

    child_id = candidates.content_id({**parent, **proposal})
    stored = json.loads((tmp_path / "hypotheses.jsonl").read_text().splitlines()[0])
    assert (stored["parent"], stored["child"]) == (candidates.content_id(parent), child_id)
    assert child_id == candidates.content_id({**parent, READ_MD: new_text})
    assert candidates.content_id(parent) == candidates.content_id(seed_candidate())


def test_reflection_client_holds_the_hypothesis_after_new_text(world) -> None:  # noqa: F811
    reply = f"<new_text>\nbetter text\n</new_text>\n{hypothesis_text()}"
    evo = ready(world, spawn=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, result_stream(reply), ""))
    text = evo.reflect(READ_MD, evo.seed[READ_MD], [])

    assert text.strip() == "better text"
    assert evo.hypotheses.pending[READ_MD]["metric"] == "result_chars_per_cell"


# --- loop wiring ---


def test_attribution_is_in_side_info_under_its_own_key_not_under_scores(world) -> None:  # noqa: F811
    evo = ready(world)
    _score, info = evo.evaluate(evo.seed, "dev_a")

    assert "attribution" in info
    assert set(info["attribution"]) == {"coverage", "dominant", "call_shapes", "errors"}
    assert "scores" not in info
    assert "attribution" not in json.dumps(info.get("task_scores", {}))
    assert len(json.dumps(info["attribution"], sort_keys=True)) <= diagnosis.ATTRIBUTION_CAP


def test_attribution_is_computed_once_per_candidate_from_dev_rows_only(world, monkeypatch) -> None:  # noqa: F811
    calls: list[tuple[list[dict], list[dict], list[str]]] = []
    real = cost_attribution.summarize

    def spy(candidate_rows, baseline_rows, tasks):
        calls.append((list(candidate_rows), list(baseline_rows), list(tasks)))
        return real(candidate_rows, baseline_rows, tasks)

    monkeypatch.setattr(cost_attribution, "summarize", spy)
    evo = ready(world)
    better = child(evo.seed, dev_a="1", dev_b="1", cheap_a="1", test_a="1")
    for _ in range(2):
        for task in DEV:
            evo.evaluate(evo.seed, task)
    for task in DEV:
        evo.evaluate(better, task)

    assert len(calls) == 2
    for candidate_rows, baseline_rows, tasks in calls:
        assert tasks == list(DEV)
        assert {row["task"] for row in candidate_rows} == set(DEV)
        assert {row["task"] for row in baseline_rows} <= set(DEV)
        assert not ({row["task"] for row in candidate_rows + baseline_rows} & {*CHEAP, *TEST})
        assert {row["mode"] for row in candidate_rows} == {"tilth_forced"}
        assert {row["mode"] for row in baseline_rows} == {"baseline"}
    log = log_text(world)
    assert log.count("attribution " + candidates.content_id(evo.seed)[:12]) == 1
    assert "SECRET_GT" not in json.dumps([evo.results[cid].attribution for cid in evo.results], default=str)


def test_a_failing_attribution_is_logged_and_does_not_fail_the_candidate(world, monkeypatch) -> None:  # noqa: F811
    def boom(*args, **kwargs):
        raise RuntimeError("no trajectories")

    monkeypatch.setattr(cost_attribution, "summarize", boom)
    evo = ready(world)
    _score, info = evo.evaluate(evo.seed, "dev_a")

    assert "attribution" not in info
    assert "skipped (RuntimeError: no trajectories)" in log_text(world)


def test_the_next_call_sees_the_hypothesis_outcome_of_the_earlier_one(world) -> None:  # noqa: F811
    evo = ready(world)
    for task in DEV:
        evo.evaluate(evo.seed, task)
    better = child(evo.seed, dev_a="1", dev_b="1", cheap_a="1", test_a="1")
    evo.hypotheses.pending[READ_MD] = diagnosis.parse_hypothesis(hypothesis_text(scope="all"))
    evo.hypotheses.attach(candidates.content_id(evo.seed), candidates.content_id(better))
    for task in DEV:
        evo.evaluate(better, task)

    assert [o["status"] for o in evo.hypotheses.outcomes] == ["unmet"]
    stored = [json.loads(line) for line in (Path(evo.hypotheses.path)).read_text().splitlines()]
    assert [r["kind"] for r in stored] == ["hypothesis", "outcome"]
    assert evo.hypotheses.path.name == "hypotheses.jsonl"
    assert evo.hypotheses.path.parent.name == "run1"
    feedback = [r for r in evo._extra_records(evo.seed) if "hypothesis_feedback" in r]
    assert feedback and feedback[0]["hypothesis_feedback"][0]["status"] == "unmet"
    assert "unmet" in log_text(world)


# --- replay diagnostic ---


def src_child(world) -> dict:  # noqa: F811
    return {**seed_candidate(), "src_patch": patch_for(world, {"src/lib.rs": "pub fn evolved() {}\n"})}


def stub_replay(monkeypatch, calls: list[tuple[str, str]], *, fail: bool = False) -> None:
    def replay_cell(row, binary, workdir):
        calls.append((row["task"], binary))
        if fail:
            raise RuntimeError("replay broke")
        return {"binary": binary, "task": row["task"]}

    def compare(seed, other):
        return {"coverage": 1.0, "result_chars_delta_by_tool": {"tilth_read": -40}, "new_error_tools": [],
                "files_match": True}

    monkeypatch.setattr(replay, "replay_cell", replay_cell)
    monkeypatch.setattr(replay, "compare", compare)


def test_replay_diagnostic_is_off_by_default(world, monkeypatch) -> None:  # noqa: F811
    calls: list[tuple[str, str]] = []
    stub_replay(monkeypatch, calls)
    evo = ready(world)
    for task in DEV:
        evo.evaluate(evo.seed, task)
    changed = src_child(world)
    for task in DEV:
        evo.evaluate(changed, task)

    assert evo.settings.replay_diagnostic == 0
    assert calls == []
    assert "replay " not in log_text(world)


def test_replay_diagnostic_logs_and_records_without_rejecting(world, monkeypatch) -> None:  # noqa: F811
    calls: list[tuple[str, str]] = []
    stub_replay(monkeypatch, calls)
    evo = ready(world, "--replay-diagnostic", "1")
    for task in DEV:
        evo.evaluate(evo.seed, task)
    changed = src_child(world)
    for task in DEV:
        evo.evaluate(changed, task)
    result = evo.results[candidates.content_id(changed)]

    assert evo.settings.replay_diagnostic == 1
    assert [task for task, _binary in calls] == ["dev_a", "dev_a"]
    assert calls[0][1] != calls[1][1]
    assert result.stage is None or result.stage == "paid"
    assert result.replay and result.replay[0]["task"] == "dev_a" and result.replay[0]["files_match"] is True
    assert f"replay {result.cid[:12]}: " in log_text(world)
    assert {"replay": result.replay} in evo._extra_records(changed)
    assert not any(row["mode"] == "replay" for row in world.stored())


def test_replay_clears_the_rebuilt_build_of_a_dominated_candidate(world, monkeypatch) -> None:  # noqa: F811
    calls: list[tuple[str, str]] = []
    stub_replay(monkeypatch, calls)
    keep_builds(world, monkeypatch)
    evo = ready(world, "--replay-diagnostic", "1")
    for task in DEV:
        evo.evaluate(evo.seed, task)
    dominated = {**child(evo.seed, dev_a="0", dev_b="1", cheap_a="1"),
                 "src_patch": patch_for(world, {"src/lib.rs": "pub fn evolved() {}\n"})}
    evo.evaluate(dominated, "dev_a")
    result = evo.results[candidates.content_id(dominated)]

    assert "dominated after one rollout" in log_text(world)
    assert result.replay and calls
    assert result not in evo.frontier
    assert not kept(result.sha) and kept(world.seed_sha)


def test_a_candidate_never_receives_another_candidates_replay(world, monkeypatch) -> None:  # noqa: F811
    calls: list[tuple[str, str]] = []
    stub_replay(monkeypatch, calls)
    evo = ready(world, "--replay-diagnostic", "1")
    for task in DEV:
        evo.evaluate(evo.seed, task)
    changed = src_child(world)
    for task in DEV:
        evo.evaluate(changed, task)
    other = child(evo.seed, dev_a="1", dev_b="1", cheap_a="1", test_a="1", tag="b")
    for task in DEV:
        evo.evaluate(other, task)

    assert {"replay": evo.results[candidates.content_id(changed)].replay} in evo._extra_records(changed)
    assert not [r for r in evo._extra_records(other) if "replay" in r]
    assert not [r for r in evo._extra_records(evo.seed) if "replay" in r]


def test_replay_diagnostic_skips_a_prompt_only_candidate(world, monkeypatch) -> None:  # noqa: F811
    calls: list[tuple[str, str]] = []
    stub_replay(monkeypatch, calls)
    evo = ready(world, "--replay-diagnostic", "2")
    for task in DEV:
        evo.evaluate(evo.seed, task)
    for task in DEV:
        evo.evaluate(child(evo.seed, dev_a="1", dev_b="1", cheap_a="1", test_a="1"), task)

    assert calls == []


def test_a_failing_replay_is_logged_and_the_candidate_still_scores(world, monkeypatch) -> None:  # noqa: F811
    calls: list[tuple[str, str]] = []
    stub_replay(monkeypatch, calls, fail=True)
    evo = ready(world, "--replay-diagnostic", "1")
    for task in DEV:
        evo.evaluate(evo.seed, task)
    changed = src_child(world)
    scores = [evo.evaluate(changed, task)[0] for task in DEV]

    assert calls and all(score >= 0 for score in scores)
    assert "skipped (RuntimeError: replay broke)" in log_text(world)
    assert evo.results[candidates.content_id(changed)].replay == []


def test_replay_flag_parses_and_rejects_a_negative_count(world, capsys) -> None:  # noqa: F811
    assert cli.build is not None
    assert build(world).settings.replay_diagnostic == 0
    assert build(world, "--replay-diagnostic", "3").settings.replay_diagnostic == 3
    assert main(world, "--replay-diagnostic", "-1") != 0
    assert "--replay-diagnostic" in capsys.readouterr().err
