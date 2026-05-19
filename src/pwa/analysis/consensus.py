"""Cross-source consensus aggregation.

For each market bin, computes P(YES) under every weather source provided, then
summarizes:
  - Mean across sources (the "consensus probability").
  - Spread (max - min) across sources, in percentage points.
  - Agreement bucket (strong / moderate / weak).
  - Which side (YES or NO) the consensus supports buying given current prices.
  - Whether the consensus side conflicts with the primary recommendation side.

Per-source probability computation:
  - Ensemble sources (≥10 samples): fit Gaussian KDE via existing `bins_to_probs`.
  - Deterministic sources (1 sample): place a Gaussian centered on that point
    with sigma `det_sigma` (default 1.5°F / ~0.8°C) and integrate per bin. This
    gives a smooth, integrable density without pretending we have an ensemble.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import numpy as np
from scipy.stats import norm

from pwa.models.kde import bins_to_probs
from pwa.polymarket.parser import Bin
from pwa.weather.sources import SourceForecast

Agreement = Literal["strong", "moderate", "weak"]
ConsensusSide = Literal["YES", "NO", "—"]

STRONG_MAX_SPREAD_PP = 15.0
MODERATE_MAX_SPREAD_PP = 35.0

# sigma for the synthetic Gaussian around a deterministic forecast. Picked to
# roughly match a 24h-ahead point-forecast residual (literature ~1-2°F).
DETERMINISTIC_SIGMA_F = 1.5
DETERMINISTIC_SIGMA_C = 0.8


@dataclass(frozen=True, slots=True)
class ConsensusRow:
    bin: Bin
    per_source_prob: dict[str, float]
    consensus_prob: float
    spread_pp: float
    agreement: Agreement
    side: ConsensusSide
    conflicts_with_primary: bool


def _classify_agreement(spread_pp: float) -> Agreement:
    if spread_pp <= STRONG_MAX_SPREAD_PP:
        return "strong"
    if spread_pp <= MODERATE_MAX_SPREAD_PP:
        return "moderate"
    return "weak"


def _gaussian_bin_prob(center: float, sigma: float, lower: float, upper: float) -> float:
    if sigma <= 0:
        sigma = 1e-6
    lo = -np.inf if math.isinf(lower) and lower < 0 else lower
    hi = np.inf if math.isinf(upper) and upper > 0 else upper
    return float(norm.cdf(hi, loc=center, scale=sigma) - norm.cdf(lo, loc=center, scale=sigma))


def _probs_for_source(source: SourceForecast, bins: list[Bin]) -> list[float]:
    if source.is_ensemble and source.n_members >= 10:
        bp = bins_to_probs(source.samples, bins)
        return [r.p_model for r in bp]
    center = float(source.samples[0])
    sigma = DETERMINISTIC_SIGMA_F if source.unit.upper() == "F" else DETERMINISTIC_SIGMA_C
    raw = [_gaussian_bin_prob(center, sigma, b.lower, b.upper) for b in bins]
    total = sum(raw)
    if total <= 0:
        return [0.0 for _ in bins]
    return [p / total for p in raw]


def _consensus_side(
    consensus_prob: float,
    yes_ask: float | None,
    yes_bid: float | None,
) -> ConsensusSide:
    if yes_ask is not None and 0 < yes_ask < 1 and consensus_prob > yes_ask:
        return "YES"
    if yes_bid is not None and 0 < yes_bid < 1 and consensus_prob < yes_bid:
        return "NO"
    return "—"


def compute_consensus(
    sources: list[SourceForecast],
    bins: list[Bin],
    yes_asks: list[float | None],
    yes_bids: list[float | None],
    primary_sides: list[str],
) -> list[ConsensusRow]:
    """Returns one ConsensusRow per bin (order preserved)."""
    assert len(yes_asks) == len(bins) == len(yes_bids) == len(primary_sides), "len mismatch"
    per_source_matrix: dict[str, list[float]] = {}
    for src in sources:
        per_source_matrix[src.source_name] = _probs_for_source(src, bins)

    rows: list[ConsensusRow] = []
    for i, b in enumerate(bins):
        per_source = {name: probs[i] for name, probs in per_source_matrix.items()}
        values = list(per_source.values())
        if not values:
            rows.append(ConsensusRow(b, {}, 0.0, 0.0, "weak", "—", False))
            continue
        mean = float(np.mean(values))
        spread_pp = (max(values) - min(values)) * 100.0
        agreement = _classify_agreement(spread_pp)
        side = _consensus_side(mean, yes_asks[i], yes_bids[i])
        conflict = side != "—" and primary_sides[i] != "—" and side != primary_sides[i]
        rows.append(
            ConsensusRow(
                bin=b,
                per_source_prob=per_source,
                consensus_prob=mean,
                spread_pp=spread_pp,
                agreement=agreement,
                side=side,
                conflicts_with_primary=conflict,
            )
        )
    return rows
