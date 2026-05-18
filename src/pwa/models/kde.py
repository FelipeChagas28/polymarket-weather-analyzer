"""Kernel density estimation + per-bin probability integration.

Given a vector of ensemble forecasts (already bias-corrected if applicable),
fit a Gaussian KDE and integrate the density inside each market bin to obtain
P(bin). Results are normalized to sum to 1 across the bins because the bins
fully partition the support of the resolution variable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.integrate import quad
from scipy.stats import gaussian_kde

from pwa.polymarket.parser import Bin


@dataclass(frozen=True, slots=True)
class BinProb:
    bin: Bin
    p_model: float


def fit_kde(samples: np.ndarray, min_bw: float = 0.5) -> gaussian_kde:
    """Fit Gaussian KDE with Silverman bandwidth, floored to avoid spikes."""
    if samples.size < 2:
        raise ValueError("Need at least 2 samples to fit a KDE")
    kde = gaussian_kde(samples, bw_method="silverman")
    # gaussian_kde stores bw as a *factor*; multiplied by std it gives sigma.
    # Floor the sigma to min_bw °F to avoid pathological spikes when the
    # ensemble is too tight (common when all members agree near a sharp ridge).
    sigma = float(kde.factor) * float(samples.std(ddof=1) if samples.std(ddof=1) > 0 else 1.0)
    if sigma < min_bw:
        kde.set_bandwidth(min_bw / float(samples.std(ddof=1) if samples.std(ddof=1) > 0 else 1.0))
    return kde


def _integrate(kde: gaussian_kde, lower: float, upper: float) -> float:
    if math.isinf(lower) and lower < 0:
        # P(X <= upper)
        return float(kde.integrate_box_1d(-np.inf, upper))
    if math.isinf(upper) and upper > 0:
        return float(kde.integrate_box_1d(lower, np.inf))
    return float(kde.integrate_box_1d(lower, upper))


def bins_to_probs(samples: np.ndarray, bins: list[Bin], normalize: bool = True) -> list[BinProb]:
    kde = fit_kde(samples)
    raw = [(b, max(0.0, _integrate(kde, b.lower, b.upper))) for b in bins]
    total = sum(p for _, p in raw)
    if normalize and total > 0:
        return [BinProb(b, p / total) for b, p in raw]
    return [BinProb(b, p) for b, p in raw]
