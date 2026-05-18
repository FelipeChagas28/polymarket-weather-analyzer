from __future__ import annotations

import pytest

from pwa.analysis.edge import classify, expected_value
from pwa.analysis.kelly import fractional_kelly


def test_kelly_zero_when_no_edge():
    k = fractional_kelly(0.40, 0.50)
    assert k.full_kelly == 0.0
    assert k.capped == 0.0


def test_kelly_positive_when_edge():
    k = fractional_kelly(0.60, 0.50)
    # full = (0.60 - 0.50) / (1 - 0.50) = 0.2
    assert k.full_kelly == pytest.approx(0.2)
    # fractional 1/4 of that
    assert k.fractional_kelly == pytest.approx(0.05)
    # cap at 0.05 → exactly hits cap
    assert k.capped == pytest.approx(0.05)


def test_kelly_capped_at_5pct():
    k = fractional_kelly(0.90, 0.40)
    # full = 0.5/0.6 ≈ 0.833
    # frac 1/4 ≈ 0.208 → capped to 0.05
    assert k.capped == pytest.approx(0.05)


def test_kelly_invalid_prices_return_zero():
    assert fractional_kelly(0.6, 0.0).capped == 0.0
    assert fractional_kelly(0.6, 1.0).capped == 0.0


def test_ev_formula():
    assert expected_value(0.60, 0.50) == pytest.approx(0.10)
    assert expected_value(0.50, 0.50) == pytest.approx(0.0)
    assert expected_value(0.40, 0.50) == pytest.approx(-0.10)


def test_classify_thresholds():
    # price=0.3, p=0.4 → edge=0.10, ev=0.4*0.7 - 0.6*0.3 = 0.28-0.18 = 0.10
    # ev/price = 0.10/0.30 = 0.333 → STRONG BUY
    assert classify(edge=0.10, ev=0.10, price=0.30) == "STRONG BUY"
    # edge=0.05 → BUY (under 0.08)
    assert classify(edge=0.05, ev=0.05, price=0.50) == "BUY"
    # edge=0.02 → SKIP
    assert classify(edge=0.02, ev=0.02, price=0.50) == "SKIP"
