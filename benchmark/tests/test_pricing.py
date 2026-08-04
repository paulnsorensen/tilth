import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import analyze


def _report_run():
    return {
        "task": "task",
        "mode": "baseline",
        "model": "haiku",
        "repetition": 0,
        "correct": True,
        "total_cost_usd": 1.0,
        "context_tokens": 1,
        "output_tokens": 1,
        "input_tokens": 1,
        "cache_creation_tokens": 1,
        "cache_read_tokens": 1,
        "num_turns": 1,
        "num_tool_calls": 0,
        "duration_ms": 1,
    }


def test_model_specific_breakdown_uses_all_token_categories():
    run = {
        "model": "claude-haiku-4-5-20251001",
        "input_tokens": 1_000_000,
        "cache_creation_tokens": 1_000_000,
        "cache_read_tokens": 1_000_000,
        "output_tokens": 1_000_000,
    }

    costs = analyze.compute_cost_breakdown(run)

    assert costs == {
        "input_cost": 1.0,
        "cache_creation_cost": 1.25,
        "cache_read_cost": 0.10,
        "output_cost": 5.0,
    }

def test_alias_pricing_resolves_when_actual_model_id_is_absent():
    costs = analyze.compute_cost_breakdown({
        "model_alias": "haiku",
        "input_tokens": 1_000_000,
        "cache_creation_tokens": 1_000_000,
        "cache_read_tokens": 1_000_000,
        "output_tokens": 1_000_000,
    })

    assert costs == {
        "input_cost": 1.0,
        "cache_creation_cost": 1.25,
        "cache_read_cost": 0.10,
        "output_cost": 5.0,
    }


def test_actual_model_does_not_fall_back_to_alias_pricing():
    with pytest.raises(ValueError, match="no pricing entry"):
        analyze.compute_cost_breakdown({
            "model": "unpriced-model",
            "model_alias": "haiku",
            "input_tokens": 1,
        })


def test_stale_pricing_warns_only_after_thirty_days():
    assert analyze.pricing_staleness_warning("2026-06-01", today=date(2026, 8, 4))
    assert not analyze.pricing_staleness_warning("2026-07-05", today=date(2026, 8, 4))


def test_generated_report_renders_stale_pricing_warning(monkeypatch):
    monkeypatch.setitem(analyze.PRICING_DATA, "as_of", "2026-06-01")

    report = analyze.generate_report([_report_run()])

    assert "WARNING: pricing table as_of 2026-06-01" in report
