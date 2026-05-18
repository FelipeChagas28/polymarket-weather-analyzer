"""Edge & expected-value calculation for buying YES on a binary contract."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pwa.analysis.kelly import KellySize, fractional_kelly
from pwa.polymarket.parser import Bin

Recommendation = Literal["STRONG BUY", "BUY", "SKIP"]


@dataclass(frozen=True, slots=True)
class EdgeRow:
    bin: Bin
    p_model: float
    ask: float | None
    bid: float | None
    edge: float | None
    ev: float | None
    kelly: KellySize | None
    recommendation: Recommendation


def expected_value(p: float, ask: float) -> float:
    return p * (1.0 - ask) - (1.0 - p) * ask


def classify(edge: float | None, ev: float | None, ask: float | None) -> Recommendation:
    if edge is None or ev is None or ask is None or ask <= 0:
        return "SKIP"
    ev_ratio = ev / ask
    if edge >= 0.08 and ev_ratio >= 0.15:
        return "STRONG BUY"
    if edge >= 0.04:
        return "BUY"
    return "SKIP"


def evaluate_bin(b: Bin, p_model: float, ask: float | None, bid: float | None) -> EdgeRow:
    if ask is None or ask <= 0 or ask >= 1:
        return EdgeRow(b, p_model, ask, bid, None, None, None, "SKIP")
    edge = p_model - ask
    ev = expected_value(p_model, ask)
    kelly = fractional_kelly(p_model, ask)
    rec = classify(edge, ev, ask)
    return EdgeRow(b, p_model, ask, bid, edge, ev, kelly, rec)
