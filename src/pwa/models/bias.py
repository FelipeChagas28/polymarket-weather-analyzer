"""Bias correction: shift ensemble forecast vector by (observed - forecast) mean.

We compute the residual on the last N days at the same lead time (~24h ahead),
then apply the inverse shift to today's ensemble samples. This corrects for
systematic local biases of the model (e.g. always 1.2°F too cold at LaGuardia).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

from pwa.weather.open_meteo import historical_forecast_daily, observed_daily


@dataclass(frozen=True, slots=True)
class BiasReport:
    n_days: int
    mean_bias: float  # observed - forecast (positive => model underpredicts)
    std_residual: float


def compute_bias(
    lat: float,
    lon: float,
    tz: str,
    direction: str,
    today: date | None = None,
    lookback_days: int = 60,
    model: str = "gfs_seamless",
) -> BiasReport:
    today = today or date.today()
    end = today - timedelta(days=1)
    start = end - timedelta(days=lookback_days - 1)
    observed = observed_daily(lat, lon, start, end, tz, direction)
    forecast = historical_forecast_daily(lat, lon, start, end, tz, direction, model=model)
    common = sorted(set(observed.keys()) & set(forecast.keys()))
    if not common:
        return BiasReport(0, 0.0, 0.0)
    residuals = np.array([observed[d] - forecast[d] for d in common], dtype=float)
    return BiasReport(
        n_days=len(common),
        mean_bias=float(residuals.mean()),
        std_residual=float(residuals.std(ddof=1)) if len(residuals) > 1 else 0.0,
    )


def apply_bias(samples: np.ndarray, bias: BiasReport) -> np.ndarray:
    return samples + bias.mean_bias
