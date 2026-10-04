"""Tests for benchmark/parse.py — transcript → RunResult parsing.

Each parser accumulates result_text across every assistant turn: a short
wrap-up turn at the end must add to, not replace, a substantive earlier turn.
Grading reads result_text (tasks/base.py check_correctness), so losing an
earlier turn silently discards the real answer.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from parse import (
    parse_codex_json,
    parse_opencode_json,
    parse_stream_json,
    tool_batch_sizes,
    tool_op_kinds,
)


# --- stream-json (claude -p) ------------------------------------------------


def _assistant_event(text: str) -> dict:
    """Build a minimal stream-json 'assistant' event carrying one text block."""
    return {
        "type": "assistant",
        "message": {
            "usage": {
                "input_tokens": 10,
                "output_tokens": 10,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
            "content": [{"type": "text", "text": text}],
        },
    }


def _result_event() -> dict:
    return {"type": "result", "num_turns": 5, "total_cost_usd": 0.01}


def _tool_use_event(name: str, tool_input: dict, tool_id: str = "t1") -> dict:
    return {
        "type": "assistant",
        "message": {
            "usage": {"input_tokens": 1, "output_tokens": 1,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
            "content": [{"type": "tool_use", "id": tool_id, "name": name,
                         "input": tool_input}],
        },
    }


def test_tool_batch_sizes_records_batchable_calls_in_order():
    """Batch-size capture is the needle for batching instrumentation: array
    params record their length; the upstream singular fallbacks (path/query)
    count as 1; both write shapes (upstream files, fork edits) are covered;
    non-batchable tools (Bash, Glob, tilth_files) are excluded entirely."""
    events = [
        _tool_use_event("mcp__tilth__tilth_read", {"paths": ["a.rs", "b.rs", "c.rs"]}, "t1"),
        _tool_use_event("mcp__tilth__tilth_read", {"path": "d.rs"}, "t2"),
        _tool_use_event("mcp__tilth__tilth_search", {"queries": [{"query": "x"}]}, "t3"),
        _tool_use_event("tilth_write", {"files": [{"path": "a"}, {"path": "b"}]}, "t4"),
        _tool_use_event("mcp__tilth__tilth_write",
                        {"edits": [{"path": "a", "ops": []}, {"path": "b", "ops": []},
                                   {"path": "c", "ops": []}]}, "t5"),
        _tool_use_event("Bash", {"command": "ls"}, "t6"),
        _tool_use_event("Glob", {"pattern": "**/*.rs", "path": "src"}, "t7"),
        _tool_use_event("mcp__tilth__tilth_files", {"pattern": "*.rs"}, "t8"),
        _result_event(),
    ]

    result = parse_stream_json("\n".join(json.dumps(e) for e in events))

    assert tool_batch_sizes(result) == {
        "mcp__tilth__tilth_read": [3, 1],
        "mcp__tilth__tilth_search": [1],
        "tilth_write": [2],
        "mcp__tilth__tilth_write": [3],
    }


def test_tool_batch_sizes_counts_coerced_bare_string_as_one():
    """The server coerces a bare-string array param to one item — these are
    exactly the un-batched calls the metric must count, not drop (dropping
    them inflates the multi-item share in favor of the batching hypothesis)."""
    events = [
        _tool_use_event("mcp__tilth__tilth_read", {"paths": "a.rs"}, "t1"),
        _tool_use_event("mcp__tilth__tilth_search", {"queries": "foo"}, "t2"),
        _result_event(),
    ]

    result = parse_stream_json("\n".join(json.dumps(e) for e in events))

    assert tool_batch_sizes(result) == {
        "mcp__tilth__tilth_read": [1],
        "mcp__tilth__tilth_search": [1],
    }


def test_tool_batch_sizes_skips_malformed_inputs():
    """Genuinely malformed inputs (non-list non-string, empty array, absent
    params) are skipped, never fabricated into a size."""
    events = [
        _tool_use_event("mcp__tilth__tilth_read", {"paths": 42}, "t1"),
        _tool_use_event("mcp__tilth__tilth_read", {"paths": []}, "t2"),
        _tool_use_event("mcp__tilth__tilth_read", {}, "t3"),
        _result_event(),
    ]

    result = parse_stream_json("\n".join(json.dumps(e) for e in events))

    assert tool_batch_sizes(result) == {}


def test_tool_op_kinds_counts_ops_across_edits_sections_and_calls():
    """Op-kind capture is the needle for replace_text adoption: counts by the
    ops' "op" key across every edits section of every tilth_write call,
    aggregated for the run."""
    events = [
        _tool_use_event("mcp__tilth__tilth_write", {"edits": [
            {"path": "a", "ops": [{"op": "replace", "start": 1, "end": 1, "content": "x"}]},
            {"path": "b", "ops": [{"op": "replace_text", "find": "x", "replace": "y"},
                                   {"op": "create_file", "content": "z"}]},
        ]}, "t1"),
        _tool_use_event("mcp__tilth__tilth_write", {"edits": [
            {"path": "c", "ops": [{"op": "replace_text", "find": "a", "replace": "b"}]},
        ]}, "t2"),
        _tool_use_event("Bash", {"command": "ls"}, "t3"),
        _result_event(),
    ]

    result = parse_stream_json("\n".join(json.dumps(e) for e in events))

    assert tool_op_kinds(result) == {"replace": 1, "replace_text": 2, "create_file": 1}


def test_tool_op_kinds_skips_malformed_inputs():
    """Malformed edits/ops shapes (non-list edits, non-list ops, missing or
    non-string \"op\") are skipped, never fabricated into a count."""
    events = [
        _tool_use_event("mcp__tilth__tilth_write", {"edits": "not-a-list"}, "t1"),
        _tool_use_event("mcp__tilth__tilth_write", {"edits": [{"path": "a", "ops": "not-a-list"}]}, "t2"),
        _tool_use_event("mcp__tilth__tilth_write", {"edits": [{"path": "a", "ops": [{}]}]}, "t3"),
        _tool_use_event("mcp__tilth__tilth_write", {}, "t4"),
        _result_event(),
    ]

    result = parse_stream_json("\n".join(json.dumps(e) for e in events))

    assert tool_op_kinds(result) == {}


def test_stream_json_result_text_accumulates_across_all_assistant_turns():
    """A substantive answer in an earlier turn must survive even when a later
    turn is a short wrap-up — grading reads result_text, so losing the earlier
    turn silently discards the real answer."""
    substantive = "The dependency resolution flow starts in get_dependant()."
    wrapup = "Let me know if you want more detail."

    events = [
        {"type": "system", "session_id": "abc123"},
        _assistant_event("Looking into the codebase now."),
        _assistant_event("Still investigating."),
        _assistant_event(substantive),
        _assistant_event("Just a status update, no new info."),
        _assistant_event(wrapup),
        _result_event(),
    ]
    raw_output = "\n".join(json.dumps(e) for e in events)

    result = parse_stream_json(raw_output)

    assert substantive in result.result_text
    assert wrapup in result.result_text


def test_stream_json_result_text_single_turn_unchanged():
    """A single-turn transcript's result_text is just that turn's text —
    accumulation must not introduce leading separators or duplication."""
    events = [
        {"type": "system", "session_id": "abc123"},
        _assistant_event("The answer is 42."),
        _result_event(),
    ]
    raw_output = "\n".join(json.dumps(e) for e in events)

    result = parse_stream_json(raw_output)

    assert result.result_text == "The answer is 42."


def test_stream_json_preserves_cache_creation_ttl_breakdown():
    event = _assistant_event("The answer is 42.")
    event["message"]["usage"].update({
        "cache_creation_input_tokens": 6_300,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 300,
            "ephemeral_1h_input_tokens": 6_000,
        },
    })
    raw_output = "\n".join(json.dumps(e) for e in [event, _result_event()])

    result = parse_stream_json(raw_output)

    assert result.turns[0].cache_creation_tokens == 6_300
    assert result.turns[0].cache_creation_5m_tokens == 300
    assert result.turns[0].cache_creation_1h_tokens == 6_000


def test_stream_json_counts_each_message_usage_once():
    thinking = _assistant_event("")
    thinking["message"].update({
        "id": "msg_1",
        "content": [{"type": "thinking", "thinking": "working"}],
    })
    text = _assistant_event("The answer is 42.")
    text["message"]["id"] = "msg_1"
    raw_output = "\n".join(json.dumps(e) for e in [thinking, text, _result_event()])

    result = parse_stream_json(raw_output)

    assert len(result.turns) == 1
    assert result.turns[0].input_tokens == 10
    assert result.turns[0].output_tokens == 10


def test_stream_json_captures_init_tools_and_mcp_servers():
    """The init event's tool list and MCP status are the only record of what
    the session could actually call — a tilth arm whose init lacks
    mcp__tilth__ tools ran native-only and must be detectable from the row."""
    init = {
        "type": "system",
        "subtype": "init",
        "session_id": "abc123",
        "tools": ["Bash", "Read", "mcp__tilth__tilth_search"],
        "mcp_servers": [{"name": "tilth", "status": "connected"}],
    }
    events = [init, _assistant_event("ok"), _result_event()]

    result = parse_stream_json("\n".join(json.dumps(e) for e in events))

    assert result.available_tools == ["Bash", "Read", "mcp__tilth__tilth_search"]
    assert result.mcp_servers == [{"name": "tilth", "status": "connected"}]


def test_stream_json_captures_native_model_usage():
    """The result event's modelUsage is the native, caching-aware cost record
    (including sidecar models); rows must keep tokens + costUSD per model and
    drop static metadata like contextWindow."""
    result_event = _result_event()
    result_event["modelUsage"] = {
        "claude-sonnet-5": {
            "inputTokens": 6,
            "outputTokens": 965,
            "cacheReadInputTokens": 16415,
            "cacheCreationInputTokens": 9163,
            "costUSD": 0.0743955,
            "contextWindow": 1000000,
            "provider": "firstParty",
        },
        "claude-haiku-4-5-20251001": {
            "inputTokens": 546,
            "outputTokens": 19,
            "cacheReadInputTokens": 0,
            "cacheCreationInputTokens": 0,
            "costUSD": 0.000641,
            "contextWindow": 200000,
        },
    }
    events = [_assistant_event("ok"), result_event]

    result = parse_stream_json("\n".join(json.dumps(e) for e in events))

    assert result.model_usage == {
        "claude-sonnet-5": {
            "inputTokens": 6,
            "outputTokens": 965,
            "cacheReadInputTokens": 16415,
            "cacheCreationInputTokens": 9163,
            "costUSD": 0.0743955,
        },
        "claude-haiku-4-5-20251001": {
            "inputTokens": 546,
            "outputTokens": 19,
            "cacheReadInputTokens": 0,
            "cacheCreationInputTokens": 0,
            "costUSD": 0.000641,
        },
    }


def test_stream_json_without_init_leaves_availability_empty():
    events = [
        {"type": "system", "session_id": "abc123"},
        _assistant_event("ok"),
        _result_event(),
    ]

    result = parse_stream_json("\n".join(json.dumps(e) for e in events))

    assert result.available_tools == []
    assert result.mcp_servers == []


# --- codex exec --json ------------------------------------------------------


def _codex_agent_message(text: str) -> dict:
    return {"type": "item.completed", "item": {"type": "agent_message", "text": text}}


def test_codex_result_text_accumulates_across_all_assistant_turns():
    substantive = "The router dispatches through ServeHTTP in engine.go."
    wrapup = "Happy to dig deeper if needed."

    events = [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "turn.started"},
        _codex_agent_message(substantive),
        {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 5}},
        {"type": "turn.started"},
        _codex_agent_message(wrapup),
        {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 3}},
    ]
    raw_output = "\n".join(json.dumps(e) for e in events)

    result = parse_codex_json(raw_output, "gpt-5-codex")

    assert substantive in result.result_text
    assert wrapup in result.result_text


def test_codex_result_text_single_turn_unchanged():
    events = [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "turn.started"},
        _codex_agent_message("The answer is 42."),
        {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 5}},
    ]
    raw_output = "\n".join(json.dumps(e) for e in events)

    result = parse_codex_json(raw_output, "gpt-5-codex")

    assert result.result_text == "The answer is 42."


def test_codex_usage_partitions_cached_and_cache_write_tokens():
    events = [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "turn.started"},
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 10,
                "cached_input_tokens": 4,
                "cache_write_input_tokens": 3,
                "output_tokens": 2,
            },
        },
    ]

    result = parse_codex_json("\n".join(json.dumps(e) for e in events), "gpt-5.6-sol")

    assert result.turns[0].input_tokens == 3
    assert result.turns[0].cache_creation_tokens == 3
    assert result.turns[0].cache_read_tokens == 4
    assert result.turns[0].context_tokens == 10
    assert result.total_input_tokens == 3
    assert result.total_cache_creation_tokens == 3

    assert result.total_cost_usd == 0.00009575


def test_codex_gpt_5_6_cost_uses_short_and_long_context_rates_per_turn():
    events = [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "turn.started"},
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 200_000,
                "cached_input_tokens": 100_000,
                "output_tokens": 100_000,
            },
        },
        {"type": "turn.started"},
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 300_000,
                "cached_input_tokens": 200_000,
                "output_tokens": 100_000,
            },
        },
    ]

    result = parse_codex_json("\n".join(json.dumps(e) for e in events), "gpt-5.6-sol")

    assert result.total_cost_usd == 9.25


def test_codex_mcp_calls_and_file_changes_match_report_names():
    events = [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "tilth", "tool": "tilth_search", "arguments": {"queries": []}}},
        {"type": "item.completed", "item": {"type": "file_change", "id": "edit1", "changes": [{"path": "a.py", "kind": "update"}]}},
        {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}},
    ]
    result = parse_codex_json("\n".join(json.dumps(e) for e in events), "gpt-5.6-sol")
    assert [call.name for call in result.turns[0].tool_calls] == ["mcp__tilth__tilth_search", "Edit"]


@pytest.mark.parametrize("terminal", [{"type": "turn.failed", "error": {"message": "MCP failed"}}, {"type": "error", "message": "MCP failed"}, None])
def test_codex_rejects_failed_or_incomplete_stream(terminal):
    events = [{"type": "thread.started", "thread_id": "t1"}, {"type": "turn.started"}]
    if terminal:
        events.append(terminal)
    with pytest.raises(ValueError):
        parse_codex_json("\n".join(json.dumps(e) for e in events), "gpt-5.6-sol")


# --- opencode run --format json ---------------------------------------------


def _opencode_text(text: str) -> dict:
    return {"type": "text", "part": {"text": text}}


def _opencode_step_finish() -> dict:
    return {"type": "step_finish", "part": {"tokens": {"input": 5, "output": 5}}}


def test_opencode_result_text_accumulates_across_all_assistant_turns():
    substantive = "Search dispatch is classified in classify.rs."
    wrapup = "Let me know if that helps."

    events = [
        {"type": "text", "part": {"text": substantive}, "sessionID": "s1"},
        _opencode_step_finish(),
        _opencode_text(wrapup),
        _opencode_step_finish(),
    ]
    raw_output = "\n".join(json.dumps(e) for e in events)

    result = parse_opencode_json(raw_output)

    assert substantive in result.result_text
    assert wrapup in result.result_text


def test_opencode_result_text_single_turn_unchanged():
    events = [
        {"type": "text", "part": {"text": "The answer is 42."}, "sessionID": "s1"},
        _opencode_step_finish(),
    ]
    raw_output = "\n".join(json.dumps(e) for e in events)

    result = parse_opencode_json(raw_output)

    assert result.result_text == "The answer is 42."


# --- cost source and stream fixtures ------------------------------------------

_STREAMS = Path(__file__).parent / "fixtures" / "streams"


def test_present_native_cost_is_used():
    result = parse_stream_json((_STREAMS / "claude_native_cost.jsonl").read_text())

    assert result.total_cost_usd == 0.0421
    assert result.cost_source == "native"


def test_reported_zero_native_cost_is_not_repriced():
    events = [_assistant_event("ok"), {"type": "result", "num_turns": 1, "total_cost_usd": 0.0}]

    result = parse_stream_json("\n".join(json.dumps(e) for e in events))

    assert result.total_cost_usd == 0.0
    assert result.cost_source == "native"


def test_missing_native_cost_uses_pricing():
    from pricing import compute_cost_breakdown

    result = parse_stream_json((_STREAMS / "claude_no_native_cost.jsonl").read_text())
    expected = sum(compute_cost_breakdown({
        "model": "claude-sonnet-5",
        "per_turn_token_usage": [
            {"input_tokens": 1200, "cache_creation_tokens": 4000, "cache_creation_5m_tokens": 4000,
             "cache_creation_1h_tokens": 0, "cache_read_tokens": 0, "output_tokens": 300},
            {"input_tokens": 50, "cache_creation_tokens": 0, "cache_creation_5m_tokens": 0,
             "cache_creation_1h_tokens": 0, "cache_read_tokens": 5200, "output_tokens": 120},
        ],
        "input_tokens": 1250, "cache_creation_tokens": 4000, "cache_creation_5m_tokens": 4000,
        "cache_creation_1h_tokens": 0, "cache_read_tokens": 5200, "output_tokens": 420,
    }).values())

    assert result.cost_source == "pricing"
    assert result.total_cost_usd > 0.0
    assert result.total_cost_usd == pytest.approx(expected)


def test_missing_native_cost_without_a_model_cannot_be_priced():
    events = [_assistant_event("ok"), {"type": "result", "num_turns": 1}]

    with pytest.raises(ValueError, match="model"):
        parse_stream_json("\n".join(json.dumps(e) for e in events))


def test_missing_native_cost_with_no_usage_prices_to_zero():
    events = [{"type": "system", "subtype": "init", "tools": []}, {"type": "result", "num_turns": 0}]

    result = parse_stream_json("\n".join(json.dumps(e) for e in events))

    assert result.total_cost_usd == 0.0
    assert result.cost_source == "pricing"


def test_stream_native_cost_distinguishes_absent_from_zero():
    from parse import stream_native_cost

    assert stream_native_cost((_STREAMS / "claude_native_cost.jsonl").read_text()) == 0.0421
    assert stream_native_cost((_STREAMS / "claude_no_native_cost.jsonl").read_text()) is None
    assert stream_native_cost(json.dumps({"type": "result", "total_cost_usd": 0}) + "\n{truncated") == 0.0


def test_quota_rejection_is_detected_from_rate_limit_event():
    from parse import detect_quota_rejection

    assert detect_quota_rejection((_STREAMS / "claude_quota_rejected.jsonl").read_text())
    assert detect_quota_rejection((_STREAMS / "claude_native_cost.jsonl").read_text()) is None
    allowed = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning"}}
    assert detect_quota_rejection(json.dumps(allowed)) is None


def test_quota_rejection_is_detected_from_limit_result_text():
    from parse import detect_quota_rejection

    event = {"type": "result", "is_error": True, "result": "You've hit your weekly limit · resets Mon"}
    assert detect_quota_rejection(json.dumps(event))


def test_claude_trajectory_pairs_untruncated_inputs_and_outputs():
    from parse import extract_trajectory

    raw = (_STREAMS / "claude_native_cost.jsonl").read_text()
    events = [json.loads(line) for line in raw.splitlines()]
    tool_use = events[1]["message"]["content"][0]
    tool_result = events[2]["message"]["content"][0]

    trajectory = extract_trajectory(raw, "claude")

    assert trajectory == [{
        "tool_use_id": "toolu_fixture_1",
        "name": "Bash",
        "input": tool_use["input"],
        "output": tool_result["content"],
        "is_error": False,
    }]


def test_codex_trajectory_records_commands_and_mcp_calls():
    from parse import extract_trajectory

    long_output = "x" * 500
    events = [
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "item_1", "type": "command_execution",
                                             "command": "go test ./...", "aggregated_output": long_output,
                                             "exit_code": 1, "status": "failed"}},
        {"type": "item.completed", "item": {"id": "item_2", "type": "mcp_tool_call", "server": "tilth",
                                             "tool": "tilth_read", "arguments": {"paths": ["a.go"]},
                                             "result": {"content": [{"type": "text", "text": long_output}]},
                                             "error": None, "status": "completed"}},
        {"type": "item.completed", "item": {"id": "item_3", "type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]

    trajectory = extract_trajectory("\n".join(map(json.dumps, events)), "codex")

    assert trajectory == [
        {"tool_use_id": "item_1", "name": "command_execution", "input": {"command": "go test ./..."},
         "output": long_output, "is_error": True},
        {"tool_use_id": "item_2", "name": "mcp__tilth__tilth_read", "input": {"paths": ["a.go"]},
         "output": {"content": [{"type": "text", "text": long_output}]}, "is_error": False},
    ]


def test_rejected_rate_limit_event_before_a_successful_result_is_not_quota():
    from parse import detect_quota_rejection

    events = [
        {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "rateLimitType": "seven_day_opus"}},
        {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.1},
    ]

    assert detect_quota_rejection("\n".join(map(json.dumps, events))) is None
