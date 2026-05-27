"""Tests for the hybrid GTD→FAK execution strategy."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pwa.live.execution import ExecutionResult, submit_hybrid


class _FakeTime:
    """Deterministic clock for the hybrid loop."""

    def __init__(self) -> None:
        self.t = 1_000.0

    def now(self) -> float:
        return self.t

    def sleep(self, dt: float) -> None:
        self.t += dt


def _make_clob(post_limit_resp, get_orders_seq, post_market_resp=None):
    clob = MagicMock()
    clob.post_limit_order.return_value = post_limit_resp
    clob.get_orders.side_effect = get_orders_seq
    clob.post_market_order.return_value = post_market_resp or {}
    clob.cancel.return_value = {}
    return clob


def test_gtd_full_fill_returns_filled_no_fallback():
    clk = _FakeTime()
    # GTD is matched on the very first poll.
    clob = _make_clob(
        post_limit_resp={"orderID": "g1", "status": "live"},
        get_orders_seq=[
            [{"orderID": "g1", "status": "matched", "size_matched": 10.0, "price": 0.42}],
        ],
    )
    result = submit_hybrid(
        clob, token_id="tok", price=0.42, size=10.0,
        hold_seconds=30, now_fn=clk.now, sleep_fn=clk.sleep,
    )
    assert isinstance(result, ExecutionResult)
    assert result.status == "filled"
    assert result.shares_filled == 10.0
    assert result.fill_price == 0.42
    assert result.used_fallback is False
    assert result.fees_paid == 0.0
    clob.cancel.assert_not_called()
    clob.post_market_order.assert_not_called()


def test_gtd_partial_fill_cancels_and_returns_partial():
    clk = _FakeTime()
    clob = _make_clob(
        post_limit_resp={"orderID": "g1", "status": "live"},
        get_orders_seq=[
            [{"orderID": "g1", "status": "expired", "size_matched": 4.0, "price": 0.42}],
        ],
    )
    result = submit_hybrid(
        clob, token_id="tok", price=0.42, size=10.0,
        hold_seconds=30, now_fn=clk.now, sleep_fn=clk.sleep,
    )
    assert result.status == "partial"
    assert result.shares_filled == 4.0
    assert result.used_fallback is False
    clob.cancel.assert_called_once_with("g1")
    clob.post_market_order.assert_not_called()


def test_gtd_zero_then_fak_fills_completely():
    clk = _FakeTime()
    clob = _make_clob(
        post_limit_resp={"orderID": "g1", "status": "live"},
        get_orders_seq=[
            [{"orderID": "g1", "status": "expired", "size_matched": 0.0, "price": 0.42}],
        ],
        post_market_resp={
            "orderID": "f1",
            "status": "matched",
            "size_matched": 10.0,
            "price": 0.43,
        },
    )
    result = submit_hybrid(
        clob, token_id="tok", price=0.42, size=10.0,
        hold_seconds=30, now_fn=clk.now, sleep_fn=clk.sleep,
    )
    assert result.status == "filled"
    assert result.used_fallback is True
    assert result.shares_filled == 10.0
    assert result.fill_price == 0.43
    # Taker fee = 1.25% of notional = 10 * 0.43 * 0.0125
    assert result.fees_paid == pytest.approx(10.0 * 0.43 * 0.0125, abs=1e-6)
    clob.cancel.assert_called_once_with("g1")
    clob.post_market_order.assert_called_once()


def test_gtd_zero_then_fak_zero_returns_failed():
    clk = _FakeTime()
    clob = _make_clob(
        post_limit_resp={"orderID": "g1"},
        get_orders_seq=[[{"orderID": "g1", "status": "expired", "size_matched": 0}]],
        post_market_resp={"orderID": "f1", "status": "expired", "size_matched": 0},
    )
    result = submit_hybrid(
        clob, token_id="tok", price=0.42, size=10.0,
        hold_seconds=30, now_fn=clk.now, sleep_fn=clk.sleep,
    )
    assert result.status == "failed"
    assert result.used_fallback is True
    assert result.shares_filled == 0.0


def test_initial_post_returning_no_order_id_is_failed():
    clk = _FakeTime()
    clob = _make_clob(
        post_limit_resp={"error": "nope"},
        get_orders_seq=[[]],
    )
    result = submit_hybrid(
        clob, token_id="tok", price=0.4, size=5.0,
        hold_seconds=10, now_fn=clk.now, sleep_fn=clk.sleep,
    )
    assert result.status == "failed"
    assert result.order_id is None
    clob.get_orders.assert_not_called()


def test_order_vanishes_from_get_orders_treated_as_terminal():
    clk = _FakeTime()
    # First poll: order missing → break loop; size_matched stays zero from initial.
    clob = _make_clob(
        post_limit_resp={"orderID": "g1", "status": "live"},
        get_orders_seq=[[]],
        post_market_resp={"orderID": "f1", "status": "matched", "size_matched": 5.0, "price": 0.5},
    )
    result = submit_hybrid(
        clob, token_id="tok", price=0.5, size=5.0,
        hold_seconds=5, now_fn=clk.now, sleep_fn=clk.sleep,
    )
    # No fill seen → FAK fallback runs.
    assert result.used_fallback is True
    assert result.status == "filled"
