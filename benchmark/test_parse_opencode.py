#!/usr/bin/env python3
"""Regression tests for parse_opencode_json.

No pytest in this harness — run directly: `python3 benchmark/test_parse_opencode.py`.

The contract under test: opencode emits per-step `step-finish` events whose
`cost` and `tokens` are PER-STEP, not cumulative. The parser must SUM them.
A last-write-wins reader (the throwaway spike's `_find_keys`) under-reports
cost by the step count — ~15x on a multi-turn edit task. These tests fail
loudly if the parser ever regresses to last-write-wins.
"""

import io
import json
import sys
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from parse import parse_opencode_json, tool_call_counts  # noqa: E402

# Minimal NDJSON mirroring the real opencode `run --format json` schema:
# {type, part, sessionID, timestamp} per line; step-finish carries cost+tokens.
# Two steps with DIFFERENT costs so sum (0.03) != last (0.02) != first (0.01).
_FIXTURE_EVENTS = [
    {"type": "step_start", "sessionID": "ses_x", "part": {"type": "step-start"}},
    {"type": "tool_use", "sessionID": "ses_x", "part": {
        "type": "tool", "tool": "tilth_tilth_search", "callID": "c1",
        "state": {"input": {"queries": [{"query": "foo"}]}}}},
    {"type": "text", "sessionID": "ses_x", "part": {"type": "text", "text": "thinking..."}},
    {"type": "step_finish", "sessionID": "ses_x", "part": {
        "type": "step-finish", "cost": 0.01,
        "tokens": {"input": 100, "output": 10, "reasoning": 0,
                   "cache": {"write": 7, "read": 0}}}},
    {"type": "step_start", "sessionID": "ses_x", "part": {"type": "step-start"}},
    {"type": "text", "sessionID": "ses_x", "part": {"type": "text", "text": "final answer"}},
    {"type": "step_finish", "sessionID": "ses_x", "part": {
        "type": "step-finish", "cost": 0.02,
        "tokens": {"input": 5, "output": 20, "reasoning": 0,
                   "cache": {"write": 0, "read": 200}}}},
]
_FIXTURE_NDJSON = "\n".join(json.dumps(e) for e in _FIXTURE_EVENTS)


def _approx(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) <= tol


def test_sums_cost_and_tokens_across_steps():
    r = parse_opencode_json(_FIXTURE_NDJSON)
    # Cost must be the SUM, not last (0.02) and not first (0.01).
    assert _approx(r.total_cost_usd, 0.03), f"cost {r.total_cost_usd} != 0.03 (summed)"
    assert r.total_input_tokens == 105, r.total_input_tokens
    assert r.total_output_tokens == 30, r.total_output_tokens
    assert r.total_cache_creation_tokens == 7, r.total_cache_creation_tokens
    assert r.total_cache_read_tokens == 200, r.total_cache_read_tokens


def test_turns_tools_and_result_text():
    r = parse_opencode_json(_FIXTURE_NDJSON)
    assert r.num_turns == 2, r.num_turns
    assert len(r.turns) == 2, len(r.turns)
    # Final assistant text is the LAST text event, not the first partial.
    assert r.result_text == "final answer", repr(r.result_text)
    assert r.session_id == "ses_x", r.session_id
    counts = tool_call_counts(r)
    assert counts == {"tilth_search": 1}, counts
    # The tool call is attributed to the turn it preceded (turn 0).
    assert r.turns[0].tool_calls[0].turn_index == 0
    assert r.turns[0].tool_calls[0].input == {"queries": [{"query": "foo"}]}


def test_empty_output_is_safe():
    r = parse_opencode_json("")
    assert r.num_turns == 0
    assert _approx(r.total_cost_usd, 0.0)
    assert r.result_text == ""


def test_real_dump_sums_match_jq_ground_truth():
    """Opt-in: validate against captured spike dumps when present.

    Ground-truth values were computed with jq over the raw NDJSON. They lock
    the ~15x last-write-wins regression: e.g. rg_edit real sum is $0.1325, the
    spike's last-step reading was $0.0088.
    """
    spike = Path(__file__).parent / "results" / "spike"
    expected = {
        "find_definition_tilth_20260621_084015.raw.json": {
            "cost": 0.05846854, "input": 172853, "output": 883,
            "cache_read": 165168, "turns": 6, "tools": 5},
        "rg_edit_line_count_tilth_20260621_084517.raw.json": {
            "cost": 0.13250563, "input": 130495, "output": 1696,
            "cache_read": 798168, "turns": 15, "tools": 14},
        "rg_edit_line_count_baseline_20260621_085534.raw.json": {
            "cost": 0.38237872, "input": 765686, "output": 9677,
            "cache_read": 1645691, "turns": 34, "tools": 33},
    }
    checked = 0
    for name, exp in expected.items():
        path = spike / name
        if not path.exists():
            continue
        checked += 1
        r = parse_opencode_json(path.read_text())
        assert _approx(r.total_cost_usd, exp["cost"], 1e-6), (name, r.total_cost_usd)
        assert r.total_input_tokens == exp["input"], (name, r.total_input_tokens)
        assert r.total_output_tokens == exp["output"], (name, r.total_output_tokens)
        assert r.total_cache_read_tokens == exp["cache_read"], (name, r.total_cache_read_tokens)
        assert r.num_turns == exp["turns"], (name, r.num_turns)
        assert sum(tool_call_counts(r).values()) == exp["tools"], (name, tool_call_counts(r))
    if checked == 0:
        print("  (skipped real-dump check — results/spike dumps not present)")


def test_skips_malformed_lines():
    """Unpinned/best-effort schema: a garbage line must not sink the run, but
    it must NOT pass silently — a skipped line warns to stderr so a truncated
    run can't masquerade as clean data."""
    lines = _FIXTURE_NDJSON.split("\n")
    polluted = "\n".join([lines[0], "not json at all {{{", *lines[1:]])
    err = io.StringIO()
    with redirect_stderr(err):
        r = parse_opencode_json(polluted)
    # Valid events still parsed; cost still the correct sum.
    assert _approx(r.total_cost_usd, 0.03), r.total_cost_usd
    assert r.num_turns == 2, r.num_turns
    # The malformed line is surfaced, not swallowed.
    assert "skipped" in err.getvalue().lower(), repr(err.getvalue())


def test_missing_token_fields_default_to_zero():
    """A step-finish with no tokens/cache must default, not KeyError/crash."""
    sparse = json.dumps({"type": "step_finish", "sessionID": "s",
                         "part": {"type": "step-finish", "cost": 0.005}})
    r = parse_opencode_json(sparse)
    assert r.num_turns == 1
    assert _approx(r.total_cost_usd, 0.005)
    assert r.total_input_tokens == 0
    assert r.total_cache_read_tokens == 0


def test_null_fields_do_not_crash():
    """Explicit null (not absent key) in step_finish must not raise AttributeError/TypeError.

    The `or {}` / `or 0` coercions fire for null values; `dict.get(key, default)`
    only fires when the key is ABSENT — so explicit null was a crash before this fix.
    """
    # step 1: cost null, tokens null -> everything degrades to 0
    step1 = json.dumps({"type": "step_finish", "sessionID": "s",
                        "part": {"type": "step-finish", "cost": None, "tokens": None}})
    # step 2: cost present, tokens dict with null sub-fields
    step2 = json.dumps({"type": "step_finish", "sessionID": "s",
                        "part": {"type": "step-finish", "cost": 0.007,
                                  "tokens": {"input": None, "output": None, "cache": None}}})
    r = parse_opencode_json("\n".join([step1, step2]))
    assert r.num_turns == 2, r.num_turns
    assert _approx(r.total_cost_usd, 0.007), r.total_cost_usd
    assert r.total_input_tokens == 0, r.total_input_tokens
    assert r.total_output_tokens == 0, r.total_output_tokens


def test_opencode_rejects_unsupported_mode():
    """opencode has no built-in-tool allowlist, so tilth_forced has no analog.

    run_single must raise a clear error BEFORE spawning opencode (the command
    branch raises on an unmapped mode), not run the wrong configuration.
    """
    import run
    try:
        run.run_single("find_definition", "tilth_forced", "gpt5mini", 0)
    except RuntimeError as e:
        assert "tilth_forced" in str(e), str(e)
    else:
        raise AssertionError("expected RuntimeError for unsupported opencode mode")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
