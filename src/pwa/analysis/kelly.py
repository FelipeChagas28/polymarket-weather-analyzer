"""Fractional Kelly sizing for binary YES/NO outcomes on a probability market.

Buying YES at price `ask` (∈ (0,1)) wins (1 - ask) per unit staked if YES
resolves, loses `ask` per unit staked otherwise. The Kelly fraction of bankroll
maximizing log-growth is:

    f* = (p*(1-ask) - (1-p)*ask) / (1-ask) = (p - ask) / (1 - ask)

We apply a 1/4 multiplier (fractional Kelly) — empirically more robust to
estimation error in p — and cap at 5% of bankroll per market to avoid
concentration risk.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class KellySize:
    full_kelly: float
    fractional_kelly: float
    capped: float


def fractional_kelly(
    p_model: float,
    ask: float,
    fraction: float = 0.25,
    cap: float = 0.05,
) -> KellySize:
    if ask <= 0 or ask >= 1:
        return KellySize(0.0, 0.0, 0.0)
    if p_model <= ask:
        return KellySize(0.0, 0.0, 0.0)
    full = (p_model - ask) / (1.0 - ask)
    frac = full * fraction
    capped = max(0.0, min(frac, cap))
    return KellySize(full_kelly=full, fractional_kelly=frac, capped=capped)
