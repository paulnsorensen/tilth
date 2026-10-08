"""Run-wide spend ledger shared by cells, judge calls, reflection, and the proposer."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from spend import SpendLedger


def test_would_cross_compares_spent_plus_estimate_to_ceiling() -> None:
    ledger = SpendLedger(max_usd=1.0)
    ledger.charge(0.5, source="native")

    assert ledger.would_cross(0.6)
    assert not ledger.would_cross(0.5)


def test_charges_accumulate_with_their_source() -> None:
    ledger = SpendLedger(max_usd=10.0)
    ledger.charge(0.25, source="native")
    ledger.charge(0.5, source="estimate")

    assert ledger.spent == pytest.approx(0.75)
    assert ledger.charges == [(0.25, "native"), (0.5, "estimate")]


def test_unbounded_ledger_never_crosses() -> None:
    ledger = SpendLedger(max_usd=None)
    ledger.charge(1_000.0, source="pricing")

    assert not ledger.would_cross(1_000.0)


def test_negative_charge_refused() -> None:
    with pytest.raises(ValueError):
        SpendLedger(max_usd=1.0).charge(-0.1, source="native")


def test_would_cross_counts_reserve() -> None:
    ledger = SpendLedger(max_usd=1.0, reserve=0.3)
    ledger.charge(0.5, source="native")

    assert not ledger.would_cross(0.2)
    assert ledger.would_cross(0.3)
    ledger.reserve = 0.5
    assert ledger.would_cross(0.1)
    ledger.reserve = 0.0
    assert not ledger.would_cross(0.5)
    assert not SpendLedger(max_usd=None, reserve=10.0).would_cross(1_000.0)
