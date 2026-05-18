from __future__ import annotations

import pytest

from pwa.analysis.edge import evaluate_bin
from pwa.polymarket.parser import Bin


def _bin() -> Bin:
    return Bin("66°F or higher", 65.5, float("inf"))


def test_yes_side_chosen_when_yes_edge_positive():
    # p=0.6 vs yes_ask=0.5 → YES edge=+0.1; NO would have p_no=0.4 vs no_price=1-0.49=0.51 → edge=-0.11
    row = evaluate_bin(_bin(), p_model=0.6, yes_ask=0.50, yes_bid=0.49)
    assert row.side == "YES"
    assert row.edge == pytest.approx(0.1)
    assert row.recommendation == "STRONG BUY"


def test_no_side_chosen_when_market_overprices_yes():
    # Model says p=0.66 but market is at ask=0.989, bid=0.981.
    # YES edge = 0.66 - 0.989 = -0.329 (terrible)
    # NO  edge = (1 - 0.66) - (1 - 0.981) = 0.34 - 0.019 = +0.321 (great)
    row = evaluate_bin(_bin(), p_model=0.66, yes_ask=0.989, yes_bid=0.981)
    assert row.side == "NO"
    assert row.edge == pytest.approx(0.321)
    assert row.side_price == pytest.approx(0.019)
    assert row.recommendation == "STRONG BUY"


def test_skip_when_both_sides_negative():
    # p=0.5, yes_ask=0.55, yes_bid=0.53
    # YES edge = -0.05; NO edge = (1-0.5) - (1-0.53) = 0.5 - 0.47 = +0.03 → BUY threshold not met
    row = evaluate_bin(_bin(), p_model=0.5, yes_ask=0.55, yes_bid=0.53)
    assert row.recommendation == "SKIP"


def test_missing_prices_returns_skip():
    row = evaluate_bin(_bin(), p_model=0.3, yes_ask=None, yes_bid=None)
    assert row.recommendation == "SKIP"
    assert row.side_price is None
