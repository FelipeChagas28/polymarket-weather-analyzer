"""Edge & expected-value calculation for buying YES or NO on a binary contract.

Polymarket trades two complementary tokens per market (YES and NO). The Gamma
payload exposes top-of-book for the YES token via `bestAsk` and `bestBid`.
By no-arbitrage between the complementary books, the NO token's best ask is
approximately `1 - bestBid_yes`. We use that approximation to evaluate the NO
side as well.

For the NO side at price `ask_no`:
    payoff = (1 - p_model)*(1 - ask_no) - p_model*ask_no
           = (1 - p_model) - ask_no
           = bid_yes - p_model         (substituting ask_no = 1 - bid_yes)

So edge_no = bid_yes - p_model in probability points.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pwa.analysis.kelly import KellySize, fractional_kelly
from pwa.polymarket.parser import Bin

Recommendation = Literal["STRONG BUY", "BUY", "SKIP"]
Side = Literal["YES", "NO"]


@dataclass(frozen=True, slots=True)
class EdgeRow:
    bin: Bin
    p_model: float
    yes_ask: float | None
    yes_bid: float | None
    side: Side
    side_price: float | None  # the price actually paid (ask_yes for YES, 1-bid_yes for NO)
    edge: float | None
    ev: float | None
    kelly: KellySize | None
    recommendation: Recommendation


def expected_value(p: float, ask: float) -> float:
    return p * (1.0 - ask) - (1.0 - p) * ask


def classify(edge: float | None, ev: float | None, price: float | None) -> Recommendation:
    if edge is None or ev is None or price is None or price <= 0:
        return "SKIP"
    ev_ratio = ev / price
    if edge >= 0.08 and ev_ratio >= 0.15:
        return "STRONG BUY"
    if edge >= 0.04:
        return "BUY"
    return "SKIP"


def _eval_side(p: float, price: float) -> tuple[float, float, KellySize]:
    edge = p - price
    ev = expected_value(p, price)
    kelly = fractional_kelly(p, price)
    return edge, ev, kelly


def evaluate_bin(b: Bin, p_model: float, yes_ask: float | None, yes_bid: float | None) -> EdgeRow:
    """Evaluate both sides (YES and NO) and return the row for whichever has the larger edge.

    YES side: pay `yes_ask` to win $1 if bin resolves true.
    NO side: pay `1 - yes_bid` to win $1 if bin resolves false (no-arbitrage approximation).
    """
    candidates: list[tuple[Side, float, float, float, KellySize]] = []

    if yes_ask is not None and 0 < yes_ask < 1:
        edge_y, ev_y, k_y = _eval_side(p_model, yes_ask)
        candidates.append(("YES", yes_ask, edge_y, ev_y, k_y))

    if yes_bid is not None and 0 < yes_bid < 1:
        no_price = 1.0 - yes_bid
        p_no = 1.0 - p_model
        edge_n, ev_n, k_n = _eval_side(p_no, no_price)
        candidates.append(("NO", no_price, edge_n, ev_n, k_n))

    if not candidates:
        return EdgeRow(b, p_model, yes_ask, yes_bid, "YES", None, None, None, None, "SKIP")

    # Pick whichever side has the highest (positive or least-negative) edge.
    best = max(candidates, key=lambda c: c[2])
    side, price, edge, ev, kelly = best
    rec = classify(edge, ev, price)
    return EdgeRow(b, p_model, yes_ask, yes_bid, side, price, edge, ev, kelly, rec)
