"""Unit tests for cross-source consensus aggregation."""
from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from pwa.analysis.consensus import (
    MODERATE_MAX_SPREAD_PP,
    STRONG_MAX_SPREAD_PP,
    compute_consensus,
)
from pwa.polymarket.parser import Bin
from pwa.weather.sources import SourceForecast


def _ens(name: str, samples: list[float], unit: str = "F") -> SourceForecast:
    arr = np.asarray(samples, dtype=float)
    return SourceForecast(
        source_name=name,
        target_date=date(2026, 5, 20),
        samples=arr,
        is_ensemble=True,
        n_members=arr.size,
        unit=unit,
    )


def _det(name: str, value: float, unit: str = "F") -> SourceForecast:
    return SourceForecast(
        source_name=name,
        target_date=date(2026, 5, 20),
        samples=np.array([value], dtype=float),
        is_ensemble=False,
        n_members=1,
        unit=unit,
    )


def _bins_f() -> list[Bin]:
    return [
        Bin("60°F or below", float("-inf"), 60.5, "F"),
        Bin("61-65°F", 60.5, 65.5, "F"),
        Bin("66-70°F", 65.5, 70.5, "F"),
        Bin("71°F or above", 70.5, float("inf"), "F"),
    ]


def test_strong_agreement_when_all_sources_cluster():
    bins = _bins_f()
    # All sources put the forecast firmly in the 66-70 bin.
    sources = [
        _ens("om-ens", [67.0, 68.0, 68.5, 67.5, 68.0, 67.8, 68.2, 68.1, 67.9, 68.3, 67.7, 68.0]),
        _det("ecmwf", 68.0),
        _det("gfs", 67.5),
        _det("icon", 68.2),
        _det("yr-no", 67.8),
    ]
    rows = compute_consensus(sources, bins, [None] * 4, [None] * 4, ["—"] * 4)
    # Highest consensus is on the 66-70 bin
    p_66_70 = rows[2].consensus_prob
    assert p_66_70 > 0.7
    # Agreement is strong since spread between sources is small
    assert rows[2].agreement == "strong"
    assert rows[2].spread_pp <= STRONG_MAX_SPREAD_PP


def test_weak_agreement_flags_outlier_source():
    bins = _bins_f()
    # OM-ens (bias-corrected) sits in ≥71; everyone else in 66-70 → weak.
    rng = np.random.default_rng(42)
    sources = [
        _ens("om-ens", (72.0 + rng.normal(0, 0.5, size=12)).tolist()),
        _det("ecmwf", 67.0),
        _det("gfs", 67.5),
        _det("icon", 67.0),
        _det("yr-no", 67.2),
    ]
    rows = compute_consensus(sources, bins, [None] * 4, [None] * 4, ["—"] * 4)
    # ≥71 bin has highest spread
    top_bin = rows[3]
    assert top_bin.spread_pp > MODERATE_MAX_SPREAD_PP
    assert top_bin.agreement == "weak"


def test_consensus_side_yes_when_above_ask():
    bins = _bins_f()
    sources = [_det("ecmwf", 72.0), _det("gfs", 72.5), _det("yr-no", 71.8)]
    # 4 bins, market prices for the ≥71 bin: yes_ask=0.30, yes_bid=0.25
    yes_asks: list[float | None] = [None, None, None, 0.30]
    yes_bids: list[float | None] = [None, None, None, 0.25]
    primary_sides = ["—", "—", "—", "YES"]
    rows = compute_consensus(sources, bins, yes_asks, yes_bids, primary_sides)
    assert rows[3].side == "YES"  # consensus_prob ~1.0 >> 0.30
    assert rows[3].conflicts_with_primary is False


def test_consensus_flags_conflict_with_primary():
    bins = _bins_f()
    # Primary says YES on ≥71 (OM-ens fires), but other sources say it'll be 67 → consensus → NO.
    rng = np.random.default_rng(7)
    sources = [
        _ens("om-ens", (72.0 + rng.normal(0, 0.5, size=12)).tolist()),
        _det("ecmwf", 67.0),
        _det("gfs", 67.0),
        _det("icon", 67.0),
        _det("yr-no", 67.0),
    ]
    # yes_ask high (market thinks YES is likely too), yes_bid 0.85 → consensus prob (~0.2) < bid → side NO
    yes_asks: list[float | None] = [None, None, None, 0.90]
    yes_bids: list[float | None] = [None, None, None, 0.85]
    primary_sides = ["—", "—", "—", "YES"]
    rows = compute_consensus(sources, bins, yes_asks, yes_bids, primary_sides)
    top = rows[3]
    assert top.side == "NO"
    assert top.conflicts_with_primary is True


def test_deterministic_source_probs_sum_to_one():
    bins = _bins_f()
    sources = [_det("gfs", 68.0)]
    rows = compute_consensus(sources, bins, [None] * 4, [None] * 4, ["—"] * 4)
    total = sum(r.per_source_prob["gfs"] for r in rows)
    assert total == pytest.approx(1.0, abs=1e-6)


def test_celsius_unit_is_respected():
    # In Celsius, deterministic sigma is tighter (0.8 vs 1.5°F).
    bins = [
        Bin("19°C or below", float("-inf"), 19.5, "C"),
        Bin("20°C", 19.5, 20.5, "C"),
        Bin("21°C or above", 20.5, float("inf"), "C"),
    ]
    sources = [_det("gfs", 20.0, unit="C")]
    rows = compute_consensus(sources, bins, [None] * 3, [None] * 3, ["—"] * 3)
    # Centered exactly at 20.0 with sigma 0.8°C → middle bin should dominate
    assert rows[1].per_source_prob["gfs"] > rows[0].per_source_prob["gfs"]
    assert rows[1].per_source_prob["gfs"] > rows[2].per_source_prob["gfs"]
