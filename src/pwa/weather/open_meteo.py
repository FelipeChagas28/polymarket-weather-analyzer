"""Open-Meteo client: ensemble forecast + observed archive + historical forecast.

Endpoints (all public, no API key, non-commercial use):
  - Ensemble Forecast:    https://ensemble-api.open-meteo.com/v1/ensemble
  - Forecast (single):    https://api.open-meteo.com/v1/forecast
  - Archive (ERA5):       https://archive-api.open-meteo.com/v1/archive
  - Historical Forecast:  https://historical-forecast-api.open-meteo.com/v1/forecast

All temperatures returned by Open-Meteo are in Celsius unless explicitly
requested otherwise. We request Fahrenheit since Polymarket bins are in °F.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import httpx
import numpy as np
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"

# Independent ensemble systems that Open-Meteo aggregates. Each provides 30-50
# perturbed members; combined we typically get ~80-100 forecasts to estimate
# the predictive distribution.
ENSEMBLE_MODELS = ("gfs_seamless", "icon_seamless", "ecmwf_ifs025")


@dataclass(frozen=True, slots=True)
class EnsembleResult:
    target_date: date
    direction: str  # "highest" | "lowest"
    members_daily: np.ndarray  # shape (n_members,) — daily max or min per member
    n_members: int


@retry(
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TransportError)),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)
def _get(url: str, params: dict[str, Any]) -> dict[str, Any]:
    with httpx.Client(timeout=30.0) as client:
        r = client.get(url, params=params)
        r.raise_for_status()
        return r.json()


@retry(
    retry=retry_if_exception_type(httpx.HTTPStatusError),
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=1, max=2),
    reraise=True,
)
def _get_optional(url: str, params: dict[str, Any]) -> dict[str, Any]:
    """Short-timeout GET for best-effort endpoints (bias correction). Fails fast."""
    with httpx.Client(timeout=5.0) as client:
        r = client.get(url, params=params)
        r.raise_for_status()
        return r.json()


def _daily_aggregate(temps_hourly: np.ndarray, direction: str) -> float:
    if direction == "highest":
        return float(np.nanmax(temps_hourly))
    return float(np.nanmin(temps_hourly))


def ensemble_forecast(
    lat: float,
    lon: float,
    target_date: date,
    tz: str,
    direction: str,
    models: tuple[str, ...] = ENSEMBLE_MODELS,
    unit: str = "F",
) -> EnsembleResult:
    """Fetch ensemble hourly temperature_2m and reduce each member to daily max/min.

    Returns a vector of length n_members containing the daily aggregate
    (in °F or °C depending on `unit`) for the target_date in the local tz.
    """
    iso = target_date.isoformat()
    temp_unit = "fahrenheit" if unit.upper() == "F" else "celsius"
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m",
        "models": ",".join(models),
        "temperature_unit": temp_unit,
        "timezone": tz,
        "start_date": iso,
        "end_date": iso,
    }
    data = _get(ENSEMBLE_URL, params=params)
    hourly = data.get("hourly", {})
    member_vals: list[float] = []
    for key, series in hourly.items():
        if not key.startswith("temperature_2m"):
            continue
        arr = np.asarray(series, dtype=float)
        if arr.size == 0 or np.all(np.isnan(arr)):
            continue
        member_vals.append(_daily_aggregate(arr, direction))
    if not member_vals:
        raise RuntimeError("Open-Meteo ensemble returned no usable member series")
    return EnsembleResult(
        target_date=target_date,
        direction=direction,
        members_daily=np.asarray(member_vals, dtype=float),
        n_members=len(member_vals),
    )


def observed_daily(
    lat: float,
    lon: float,
    start: date,
    end: date,
    tz: str,
    direction: str,
    unit: str = "F",
) -> dict[date, float]:
    """Fetch observed daily max/min from ERA5 archive for [start, end] inclusive."""
    var = "temperature_2m_max" if direction == "highest" else "temperature_2m_min"
    temp_unit = "fahrenheit" if unit.upper() == "F" else "celsius"
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": var,
        "temperature_unit": temp_unit,
        "timezone": tz,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }
    data = _get(ARCHIVE_URL, params=params)
    daily = data.get("daily", {})
    times = daily.get("time", [])
    values = daily.get(var, [])
    out: dict[date, float] = {}
    for t, v in zip(times, values):
        if v is None:
            continue
        out[date.fromisoformat(t)] = float(v)
    return out


def historical_forecast_daily(
    lat: float,
    lon: float,
    start: date,
    end: date,
    tz: str,
    direction: str,
    model: str = "gfs_seamless",
    unit: str = "F",
) -> dict[date, float]:
    """Fetch *archived* model forecasts (single deterministic run) per day in window.

    Used as the predicted side of the bias-correction pair (forecast vs observed).
    """
    var = "temperature_2m_max" if direction == "highest" else "temperature_2m_min"
    temp_unit = "fahrenheit" if unit.upper() == "F" else "celsius"
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": var,
        "models": model,
        "temperature_unit": temp_unit,
        "timezone": tz,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }
    data = _get_optional(HISTORICAL_FORECAST_URL, params=params)
    daily = data.get("daily", {})
    times = daily.get("time", [])
    values = daily.get(var, [])
    out: dict[date, float] = {}
    for t, v in zip(times, values):
        if v is None:
            continue
        out[date.fromisoformat(t)] = float(v)
    return out
