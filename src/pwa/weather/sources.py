"""Multi-source weather forecast fetchers for cross-validation.

Each fetcher returns a `SourceForecast`: a dataclass that wraps either an
ensemble (multiple samples per source) or a deterministic forecast (one sample)
for the target_date's daily max/min temperature in the requested unit.

Sources implemented:
  - Open-Meteo ensemble (~100 members across GFS+ICON+ECMWF) — primary, reuses
    the existing `ensemble_forecast` helper.
  - Open-Meteo per-model forecast: pulls each of ECMWF IFS / GFS / ICON-EU /
    JMA-GSM / KMA-GDAPS individually so we can see model disagreement.
  - MET Norway (api.met.no / yr.no) — fully independent provider, hourly
    forecast aggregated to daily max/min on the local target_date.

All deterministic single-sample forecasts are still returned as `SourceForecast`
with `samples=np.array([value])` so downstream code can treat them uniformly.
The consensus module (`pwa.analysis.consensus`) turns each single-sample source
into a Gaussian centered on that point when integrating per-bin probabilities.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import numpy as np
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from pwa.weather.open_meteo import (
    ENSEMBLE_URL,
    FORECAST_URL,
    EnsembleResult,
    _daily_aggregate,
    ensemble_forecast,
)

YR_NO_URL = "https://api.met.no/weatherapi/locationforecast/2.0/complete"
YR_NO_USER_AGENT = "pwa/0.1 (felipeschagas28@gmail.com)"

# Per-model breakdown via the regular forecast endpoint. Each of these is a
# *single* deterministic run (not perturbed). Picked for institutional
# independence: ECMWF/EU, NOAA/US, DWD/EU, JMA/Japan, KMA/Korea.
PER_MODEL_NAMES: tuple[str, ...] = (
    "ecmwf_ifs025",
    "gfs_seamless",
    "icon_seamless",
    "jma_seamless",
    "kma_seamless",
)


@dataclass(frozen=True, slots=True)
class SourceForecast:
    source_name: str
    target_date: date
    samples: np.ndarray  # 1+ values in °F or °C
    is_ensemble: bool
    n_members: int
    unit: str  # "F" or "C"


@retry(
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TransportError)),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)
def _get(url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
    with httpx.Client(timeout=30.0) as client:
        r = client.get(url, params=params, headers=headers or {})
        r.raise_for_status()
        return r.json()


def fetch_open_meteo_ensemble(
    lat: float,
    lon: float,
    target_date: date,
    tz: str,
    direction: str,
    unit: str = "F",
) -> SourceForecast:
    ens: EnsembleResult = ensemble_forecast(lat, lon, target_date, tz, direction, unit=unit)
    return SourceForecast(
        source_name="open-meteo-ensemble",
        target_date=ens.target_date,
        samples=ens.members_daily,
        is_ensemble=True,
        n_members=ens.n_members,
        unit=unit,
    )


def fetch_open_meteo_per_model(
    lat: float,
    lon: float,
    target_date: date,
    tz: str,
    direction: str,
    unit: str = "F",
    models: tuple[str, ...] = PER_MODEL_NAMES,
) -> list[SourceForecast]:
    """One SourceForecast per model. Models that don't cover the location are skipped silently."""
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
    data = _get(FORECAST_URL, params=params)
    hourly = data.get("hourly", {})
    out: list[SourceForecast] = []
    for key, series in hourly.items():
        if not key.startswith("temperature_2m"):
            continue
        # key format: "temperature_2m_<model>" when ?models= has 2+ entries.
        # With a single model, the key is just "temperature_2m".
        if "_" in key.removeprefix("temperature_2m"):
            model_name = key.removeprefix("temperature_2m_")
        else:
            model_name = models[0] if len(models) == 1 else "unknown"
        arr = np.asarray(series, dtype=float)
        if arr.size == 0 or np.all(np.isnan(arr)):
            continue
        daily = _daily_aggregate(arr, direction)
        out.append(
            SourceForecast(
                source_name=model_name,
                target_date=target_date,
                samples=np.array([daily], dtype=float),
                is_ensemble=False,
                n_members=1,
                unit=unit,
            )
        )
    return out


def fetch_yr_no(
    lat: float,
    lon: float,
    target_date: date,
    tz: str,
    direction: str,
    unit: str = "F",
) -> SourceForecast:
    """MET Norway (yr.no) deterministic forecast.

    Returns hourly temperature in Celsius; we aggregate to daily max/min on the
    local target_date (using the requested IANA timezone), then convert to °F
    if requested.

    MET Norway requires a custom User-Agent identifying the application.
    """
    headers = {"User-Agent": YR_NO_USER_AGENT}
    params = {"lat": round(lat, 4), "lon": round(lon, 4)}
    data = _get(YR_NO_URL, params=params, headers=headers)
    series = data.get("properties", {}).get("timeseries", [])
    tzinfo = ZoneInfo(tz)
    temps_local: list[float] = []
    for entry in series:
        t_iso = entry.get("time", "")
        try:
            t_utc = datetime.fromisoformat(t_iso.replace("Z", "+00:00"))
        except ValueError:
            continue
        local_date = t_utc.astimezone(tzinfo).date()
        if local_date != target_date:
            continue
        details = (
            entry.get("data", {})
            .get("instant", {})
            .get("details", {})
        )
        t_c = details.get("air_temperature")
        if t_c is None:
            continue
        temps_local.append(float(t_c))
    if not temps_local:
        raise RuntimeError(f"yr.no returned no hourly data for target_date={target_date} (likely beyond ~10d horizon)")
    arr_c = np.asarray(temps_local, dtype=float)
    daily_c = _daily_aggregate(arr_c, direction)
    if unit.upper() == "F":
        daily = daily_c * 9.0 / 5.0 + 32.0
    else:
        daily = daily_c
    return SourceForecast(
        source_name="yr-no",
        target_date=target_date,
        samples=np.array([daily], dtype=float),
        is_ensemble=False,
        n_members=1,
        unit=unit,
    )
