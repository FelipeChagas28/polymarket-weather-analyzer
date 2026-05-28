"""Fractional Kelly sizing for binary YES/NO outcomes on a probability market.

Buying YES at price `ask` (∈ (0,1)) wins (1 - ask) per unit staked if YES
resolves, loses `ask` per unit staked otherwise. The Kelly fraction of bankroll
maximizing log-growth is:

    f* = (p*(1-ask) - (1-p)*ask) / (1-ask) = (p - ask) / (1 - ask)

We apply a 1/8 multiplier (fractional Kelly) — empirically more robust to
estimation error in p — and cap at 2% of bankroll per bin to avoid
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
    fraction: float = 0.125,
    cap: float = 0.02,
) -> KellySize:
    if ask <= 0 or ask >= 1:
        return KellySize(0.0, 0.0, 0.0)
    if p_model <= ask:
        return KellySize(0.0, 0.0, 0.0)
    full = (p_model - ask) / (1.0 - ask)
    frac = full * fraction
    capped = max(0.0, min(frac, cap))
    return KellySize(full_kelly=full, fractional_kelly=frac, capped=capped)


_TIER_MULT: dict[str, float] = {"strong": 2.0, "moderate": 1.0, "weak": 0.5}


def compute_stake_from_tiered_flat(
    bankroll_start: float,
    available: float,
    agreement: str,
    *,
    unit_pct: float = 0.01,
    min_stake: float = 0.01,
    hard_cap: float | None = None,
) -> float:
    """Confidence-tiered flat stake sized off the *starting* bankroll.

    Unit = ``unit_pct`` of ``bankroll_start`` (default 1%). Tier multipliers:
    strong=2u, moderate=1u, weak=0.5u. Anything else (e.g. "unknown") → 0.
    """
    tier_mult = _TIER_MULT.get(agreement, 0.0)
    if tier_mult == 0.0 or bankroll_start <= 0:
        return 0.0
    stake = bankroll_start * unit_pct * tier_mult
    stake = min(stake, available)
    if hard_cap is not None:
        stake = min(stake, hard_cap)
    if stake < min_stake:
        return 0.0
    return stake
