"""Adversarial pass over the benchmark audit: parse sums, the failed-patch ledger, the reserve refresh, prompt placement."""

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import run
from evolve import failures
from evolve.candidate import content_id
from evolve_support import build, log_text, seed_candidate
from parse import parse_stream_json
from test_evolve import ready, world  # noqa: F401  (world is a fixture)
from test_evolve_failed_patches import SRC, prior_run
from test_evolve_repair import Harness, broken_patch, fixed_patch
from test_preamble_arm import (  # noqa: F401  (results and tree are fixtures)
    PREAMBLE_A, TASK, _argv, _flag, _modes_file, _stock_tilth, _tilth_entry, results, tree,
)

PRICED_MODEL = "claude-sonnet-5"


def _stream(*events: dict) -> str:
    return "\n".join(json.dumps(event) for event in events)


def _assistant(text: str) -> dict:
    usage = {"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    return {"type": "assistant", "message": {"usage": usage, "content": [{"type": "text", "text": text}]}}


# --- parse.py: several result events ---


def test_a_later_result_event_without_cost_does_not_borrow_the_earlier_native_cost() -> None:
    # Cost is cumulative and read from the last event; an earlier cost must not be silently reused or summed.
    raw = _stream(_assistant("a"), {"type": "result", "total_cost_usd": 0.5, "num_turns": 1},
                  _assistant("b"), {"type": "result", "total_cost_usd": None, "num_turns": 1})

    result = parse_stream_json(raw, model=PRICED_MODEL)

    assert result.cost_source == "pricing"
    assert result.total_cost_usd != 0.5 and result.total_cost_usd != 1.0


def test_cost_is_not_summed_across_result_events() -> None:
    raw = _stream(_assistant("a"), {"type": "result", "total_cost_usd": 0.25, "num_turns": 1},
                  _assistant("b"), {"type": "result", "total_cost_usd": 0.75, "num_turns": 1})

    result = parse_stream_json(raw)

    assert result.total_cost_usd == 0.75 and result.cost_source == "native"


def test_three_result_events_sum_each_segment_and_keep_the_last_cumulative_fields() -> None:
    # total_cost_usd, modelUsage and duration_api_ms are cumulative; a sum of duration_api_ms would be 550.
    def event(turns: int, ms: int, api_ms: int, out: int, cost: float, usage_out: int) -> dict:
        return {"type": "result", "num_turns": turns, "duration_ms": ms, "duration_api_ms": api_ms,
                "total_cost_usd": cost, "usage": {"input_tokens": 1, "output_tokens": out},
                "modelUsage": {"m": {"outputTokens": usage_out}}}

    result = parse_stream_json(_stream(event(1, 100, 50, 10, 0.1, 10), event(2, 200, 150, 20, 0.3, 30),
                                       event(4, 400, 350, 40, 0.6, 70)))

    assert (result.num_turns, result.duration_ms, result.duration_api_ms) == (7, 700, 350)
    assert (result.total_input_tokens, result.total_output_tokens) == (3, 70)
    assert result.total_cost_usd == 0.6
    assert result.model_usage["m"]["outputTokens"] == 70


def test_a_result_event_with_null_numbers_adds_zero_not_a_crash() -> None:
    raw = _stream(_assistant("a"), {"type": "result", "num_turns": 3, "duration_ms": 50, "total_cost_usd": 0.1,
                                    "usage": {"input_tokens": 5, "output_tokens": None}},
                  {"type": "result", "num_turns": None, "duration_ms": None, "total_cost_usd": 0.2,
                   "usage": {"input_tokens": None, "output_tokens": 7}})

    result = parse_stream_json(raw)

    assert (result.num_turns, result.duration_ms) == (3, 50)
    assert (result.total_input_tokens, result.total_output_tokens) == (5, 7)


def test_a_result_event_with_null_usage_adds_zero() -> None:
    raw = _stream(_assistant("a"), {"type": "result", "num_turns": 1, "usage": None, "total_cost_usd": 0.1},
                  {"type": "result", "num_turns": 1, "usage": {"input_tokens": 4}, "total_cost_usd": 0.2})

    assert parse_stream_json(raw).total_input_tokens == 4


def test_only_one_result_event_carries_num_turns_so_the_sum_is_that_one() -> None:
    raw = _stream(_assistant("a"), _assistant("b"), {"type": "result", "total_cost_usd": 0.1},
                  {"type": "result", "num_turns": 4, "total_cost_usd": 0.2})

    assert parse_stream_json(raw).num_turns == 4


def test_a_stream_with_no_result_event_reports_zero_durations_and_the_turn_count() -> None:
    result = parse_stream_json(_stream(_assistant("a"), _assistant("b")), model=PRICED_MODEL)

    assert (result.duration_ms, result.duration_api_ms, result.total_input_tokens) == (0, 0, 0)
    assert result.num_turns == len(result.turns) == 2


# --- failed-patch ledger ---


def test_a_failed_header_in_a_log_with_a_timestamp_prefix_is_not_a_failure() -> None:
    # The pin: the reader matches the line the loop writes, byte for byte, from column 0.
    line = "12:00:01 " + failures.header("a" * 12, "build")

    assert failures.parse_log(line + "\n  error: x\n") == []


@pytest.mark.parametrize("stage", ["integrity infra", "cheap tier", "paid tier", "build infra", ""])
def test_only_gate_stages_are_remembered(stage: str) -> None:
    ledger = failures.FailedPatches()
    ledger.add("e" * 12, stage, "error: x")

    assert ledger.get("e" * 12) is None
    assert failures.parse_log(failures.header("e" * 12, stage) + "\n  error: x\n") == []


def test_uppercase_or_short_ids_are_not_parsed() -> None:
    for cid in ("ABCDEF012345", "abc", "abcdef0123456"):
        text = f"candidate {cid}: failed at build\n  error: x\n"
        assert failures.parse_log(text) == [], cid


def test_a_tail_ends_at_the_first_unindented_line() -> None:
    text = (failures.header("a" * 12, "build") + "\n  error[E0425]: no x\nnext log line error: y\n  error: z\n"
            + failures.header("b" * 12, "apply") + "\n")

    assert failures.parse_log(text) == [("a" * 12, "build", "error[E0425]: no x"), ("b" * 12, "apply", "")]


def test_a_repeated_failure_moves_to_the_newest_slot_and_keeps_the_latest_verdict() -> None:
    ledger = failures.FailedPatches()
    ledger.add("a" * 12, "build", "error: old")
    ledger.add("b" * 12, "build", "error: b")
    ledger.add("a" * 12, "just check", "error: new")

    assert [entry["id"] for entry in ledger.summary()] == ["b" * 12, "a" * 12]
    assert ledger.get("a" * 12) == ("just check", "error: new")


def test_a_full_length_content_id_finds_its_twelve_character_entry() -> None:
    ledger = failures.FailedPatches()
    ledger.add("0123456789abcdef" * 4, "build", "error: x")

    assert ledger.get("0123456789ab") == ("build", "error: x")
    assert ledger.get("0123456789abffff") == ("build", "error: x")


def test_the_error_line_is_clipped_and_loses_the_worktree_path() -> None:
    long = "error: " + "x" * 500
    assert len(failures.first_error(long)) == failures.MAX_ERROR_CHARS

    shown = failures.first_error("error[E0425]: no x\n --> /home/u/.t3/worktrees/tilth/t3-ab12/src/lib.rs:3")
    assert shown == "error[E0425]: no x"
    assert "/home/u" not in failures.first_error("Diff in /home/u/worktrees/run-1/src/lib.rs:3:")


def test_a_summary_error_never_leaks_an_ansi_free_empty_tail_as_none() -> None:
    ledger = failures.FailedPatches()
    ledger.add("a" * 12, "apply", "")

    assert ledger.summary() == [{"id": "a" * 12, "stage": "apply", "error": ""}]


def test_seeding_from_a_log_that_is_not_utf8_reads_zero_and_does_not_raise(tmp_path: Path) -> None:
    log = tmp_path / "log.txt"
    log.write_bytes(b"candidate \xff\xfe: failed at build\n")
    ledger = failures.FailedPatches()

    assert ledger.seed_from(log) == 0 and ledger.seed_from(tmp_path / "absent.txt") == 0
    assert ledger.entries == {}


def test_a_directory_named_log_txt_does_not_raise(tmp_path: Path) -> None:
    (tmp_path / "log.txt").mkdir()

    assert failures.FailedPatches().seed_from(tmp_path / "log.txt") == 0


def test_a_later_seed_replaces_an_earlier_verdict_for_the_same_patch(tmp_path: Path) -> None:
    first, second = tmp_path / "a.txt", tmp_path / "b.txt"
    first.write_text(failures.header("a" * 12, "build") + "\n  error: old\n")
    second.write_text(failures.header("a" * 12, "apply") + "\n  error: new\n")
    ledger = failures.FailedPatches()
    ledger.seed_from(first)
    ledger.seed_from(second)

    assert ledger.get("a" * 12) == ("apply", "error: new")


# --- evolve: seeding, skip and the seed exemption ---


def test_a_prior_failure_recorded_for_the_seed_never_blocks_the_seed(world, tmp_path) -> None:  # noqa: F811
    evo = build(world, "--avoid-failures-from", str(prior_run(tmp_path, build(world).seed_id)))

    assert evo.failed.get(evo.seed_id) is not None
    assert not evo._failed_before(evo.seed)
    assert evo._prior_failure(evo.seed_id) is None


def test_a_resumed_run_keeps_the_prior_dir_and_adds_its_own_log(world, tmp_path) -> None:  # noqa: F811
    prior = prior_run(tmp_path, "a" * 12)
    first = build(world)
    first.log(failures.header("b" * 12, "build"))
    first.log("  error: own")

    resumed = build(world, "--avoid-failures-from", str(prior))

    assert resumed.failed.get("a" * 12) is not None and resumed.failed.get("b" * 12) == ("build", "error: own")


def test_a_build_infra_header_in_a_resumed_log_is_ignored(world) -> None:  # noqa: F811
    first = build(world)
    first.log(failures.header("c" * 12, "build infra"))
    first.log("  OSError: timeout")

    assert build(world).failed.get("c" * 12) is None


def test_a_repair_skipped_child_is_not_given_a_second_failed_patches_record(world) -> None:  # noqa: F811
    broken = broken_patch(world)
    harness = Harness(world, patches=[broken, fixed_patch(world)])
    harness.dispatch()

    later = [r for r in harness.calls[-1][1] if "failed_patches" in r]
    assert len(later) <= 1


def test_no_failed_patches_record_is_sent_before_any_gate_failure(world) -> None:  # noqa: F811
    harness = Harness(world, patches=[fixed_patch(world)])
    harness.dispatch()

    assert all("failed_patches" not in record for _c, records in harness.calls for record in records)
    assert harness.evo._failed_patch_records() == []


def test_precheck_of_a_known_failure_returns_the_stored_stage_without_a_check(world) -> None:  # noqa: F811
    harness = Harness(world, patches=[])
    child = {**seed_candidate(), SRC: broken_patch(world)}
    harness.evo.failed.add(content_id(child), "integrity", "error: seeded integrity")

    verdict = harness.evo.precheck(child)

    assert verdict is not None and verdict[0] == "integrity" and "seeded integrity" in verdict[1]
    assert harness.checked == []


def test_a_seeded_build_failure_is_not_a_precheck_failure_but_is_skipped_in_the_cascade(world) -> None:  # noqa: F811
    harness = Harness(world, patches=[])
    child = {**seed_candidate(), SRC: fixed_patch(world)}
    harness.evo.failed.add(content_id(child), "build", "error[E0308]: mismatched")
    calls = len(world.calls)

    assert harness.evo.precheck(child) is None  # build is not a precheck stage: the cascade owns it
    result = harness.evo.cascade(child)

    assert result.stage == "build" and len(world.calls) == calls


# --- finish reserve refresh ---


def test_the_refresh_never_raises_the_reserve(world) -> None:  # noqa: F811
    evo = build(world)
    assert evo.preflight() is None
    evo.ledger.reserve = 0.0

    evo._refresh_reserve()

    assert evo.ledger.reserve == 0.0
    assert "after the baselines" not in log_text(world)


def test_the_refresh_twice_is_a_no_op_the_second_time(world) -> None:  # noqa: F811
    evo = build(world)
    assert evo.preflight() is None and evo.buy_baselines() is None
    held = evo.ledger.reserve

    evo._refresh_reserve()

    assert evo.ledger.reserve == held
    assert log_text(world).count("after the baselines") == 1


# --- run.py: prompt placement ---


def test_a_non_strict_replace_arm_and_a_preamble_arm_carry_no_settings_flag() -> None:
    plain = _argv(run.MODES["tilth_forced"], "sonnet5")
    preamble = _argv(run.preamble_mode(run.MODES["tilth_forced"], PREAMBLE_A), "sonnet5")

    assert "--settings" not in plain and "--settings" not in preamble
    assert "autoMemoryEnabled" not in " ".join(plain + preamble)


def test_a_strict_preamble_arm_does_not_get_memory_off() -> None:
    mode = run.preamble_mode(run.MODES["tilth_forced"], PREAMBLE_A)
    cmd = run._claude_command(TASK, mode, "claude-x", Path("/work/repo"), bare=True, reasoning_effort=None,
                              max_budget_usd=1.0, strict_file_tools=True, strict_hook_binary="/bin/hook")

    assert "autoMemoryEnabled" not in json.loads(_flag(cmd, "--settings"))


def test_a_plain_stock_append_arm_also_turns_memory_off_and_keeps_the_cwd_only_prompt() -> None:
    cmd = _argv(_stock_tilth(), "sonnet5")

    assert json.loads(_flag(cmd, "--settings")) == {"autoMemoryEnabled": False}
    assert _flag(cmd, "--append-system-prompt") == "Your current working directory is: /work/repo"


def test_the_stock_arm_and_the_append_preamble_arm_have_different_cell_keys(monkeypatch: pytest.MonkeyPatch,
                                                                           results) -> None:
    def key(mode: run.ModeConfig) -> str:
        monkeypatch.setitem(run.MODES, "tilth_stock", mode)
        return run.cell_identity("preamble_task", "tilth_stock", "sonnet5", 0, bare=True, reasoning_effort=None,
                                 max_budget_usd=1.0, strict_file_tools=False)["run_key"]

    assert key(_stock_tilth()) != key(run.preamble_mode(_stock_tilth(), PREAMBLE_A))


def test_a_preamble_cannot_be_put_on_a_preamble_or_unknown_prompt_mode() -> None:
    for mode in ("preamble", "append_preamble"):
        carrying = replace(run.MODES["tilth_forced"], prompt_mode=mode, preamble=PREAMBLE_A)
        with pytest.raises(ValueError, match="cannot take a preamble"):
            run.preamble_mode(carrying, PREAMBLE_A)


def test_an_append_preamble_arm_without_text_is_refused() -> None:
    mode = replace(_stock_tilth(), prompt_mode="append_preamble", preamble=None)

    with pytest.raises(ValueError, match="no preamble text"):
        _argv(mode, "sonnet5")


def test_a_modes_file_with_an_unknown_prompt_mode_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(run, "MODES", dict(run.MODES))

    with pytest.raises(ValueError, match="prompt_mode must be"):
        run.load_modes_json(_modes_file(tmp_path, _tilth_entry("t_bad", "prepend")))


def test_a_failed_modes_file_registers_no_arm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tree) -> None:
    repo, first, _second = tree
    monkeypatch.setattr(run, "REPO_ROOT", repo)
    monkeypatch.setattr(run, "MODES", dict(run.MODES))
    bad = {"name": "b_pre", "tools": ["Read"], "description": "b", "prompt_mode": "preamble"}

    with pytest.raises(ValueError):
        run.load_modes_json(_modes_file(tmp_path, _tilth_entry("t_ok", "append_preamble"), bad), preamble_sha=first)

    assert "b_pre" not in run.MODES


def test_a_modes_file_without_a_preamble_arm_never_reads_the_sha(monkeypatch: pytest.MonkeyPatch,
                                                                 tmp_path: Path) -> None:
    monkeypatch.setattr(run, "MODES", dict(run.MODES))
    monkeypatch.setattr(run, "read_preamble", lambda *_a, **_k: pytest.fail("read_preamble must not run"))

    run.load_modes_json(_modes_file(tmp_path, _tilth_entry("t_stock2", "append")), preamble_sha="deadbeef")


def test_the_cli_flag_refuses_a_codex_run_over_a_stock_arm(monkeypatch: pytest.MonkeyPatch, tree) -> None:
    repo, _first, _second = tree
    monkeypatch.setattr(run, "REPO_ROOT", repo)
    monkeypatch.setitem(run.MODES, "tilth_stock", _stock_tilth())
    run._apply_preamble(["tilth_stock"], ["luna56"], None, None)

    with pytest.raises(ValueError, match="append_preamble"):
        _argv(run.MODES["tilth_stock"], "luna56")
