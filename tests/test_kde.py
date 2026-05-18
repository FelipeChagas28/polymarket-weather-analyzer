from __future__ import annotations

import numpy as np

from pwa.models.kde import bins_to_probs
from pwa.polymarket.parser import Bin


def _full_partition(lo: int = 70, hi: int = 90) -> list[Bin]:
    bins: list[Bin] = [Bin(f"{lo}°F or below", float("-inf"), lo + 0.5)]
    for x in range(lo + 1, hi, 2):
        bins.append(Bin(f"{x}-{x+1}°F", x - 0.5, x + 1 + 0.5))
    bins.append(Bin(f"{hi}°F or above", hi - 0.5, float("inf")))
    return bins


def test_probs_sum_to_one_on_full_partition():
    rng = np.random.default_rng(7)
    samples = rng.normal(loc=82.0, scale=2.0, size=100)
    bins = _full_partition(70, 90)
    probs = bins_to_probs(samples, bins, normalize=True)
    total = sum(p.p_model for p in probs)
    assert 0.99 <= total <= 1.01


def test_centered_bin_has_largest_probability():
    rng = np.random.default_rng(42)
    samples = rng.normal(loc=83.0, scale=1.0, size=200)
    bins = _full_partition(70, 90)
    probs = bins_to_probs(samples, bins)
    argmax = max(probs, key=lambda p: p.p_model)
    # The mean is 83 → ideal bin is "82-83°F" or "83-84°F". Confirm chosen bin contains 83.
    assert argmax.bin.lower <= 83.0 <= argmax.bin.upper


def test_tight_ensemble_is_floored_no_spike():
    # All members very close → KDE could spike to ~1 inside one bin.
    samples = np.array([82.0, 82.1, 82.0, 81.95, 82.05, 82.0])
    bins = _full_partition(70, 90)
    probs = bins_to_probs(samples, bins)
    max_p = max(p.p_model for p in probs)
    # With our 0.5°F bandwidth floor, no single 2°F bin should absorb >0.99.
    assert max_p < 0.99
