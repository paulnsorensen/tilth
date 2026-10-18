"""Regression tests for benchmark model aliases and configured IDs."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import MODELS, RUNNERS


def test_bare_claude_names_are_cli_aliases() -> None:
    assert MODELS["haiku"] == "haiku"
    assert MODELS["sonnet"] == "sonnet"
    assert MODELS["opus"] == "opus"


def test_pinned_claude_names_keep_snapshot_ids() -> None:
    assert MODELS["haiku45"] == "claude-haiku-4-5-20251001"
    assert MODELS["sonnet46"] == "claude-sonnet-4-6"
    assert MODELS["sonnet5"] == "claude-sonnet-5"
    assert MODELS["opus5"] == "claude-opus-5"


def test_openai_frontier_id_is_pinned() -> None:
    assert MODELS["gpt5"] == "gpt-5.6-sol"



def test_luna_5_6_alias_uses_codex() -> None:
    assert MODELS["luna56"] == "gpt-5.6-luna"
    assert RUNNERS["luna56"] == "codex"

def test_runner_aliases_remain_stable() -> None:
    for name in ("haiku", "sonnet", "opus", "haiku45", "sonnet46", "sonnet5", "opus5"):
        assert RUNNERS[name] == "claude", name
    assert RUNNERS["gpt5"] == "codex"
    assert RUNNERS["o3"] == "codex"
    assert RUNNERS["gpt5mini"] == "opencode"


def test_every_model_has_a_runner() -> None:
    assert set(MODELS) == set(RUNNERS)


def test_every_claude_name_has_a_pricing_alias() -> None:
    from pricing import PRICING_DATA

    claude_names = [name for name in MODELS if RUNNERS[name] == "claude"]
    aliases = PRICING_DATA["aliases"]
    assert [name for name in claude_names if aliases.get(name) not in PRICING_DATA["models"]] == []
    for name in ("haiku45", "sonnet46", "sonnet5", "opus5"):
        assert aliases[name] == MODELS[name]
