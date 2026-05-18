"""Calibration backtest: re-run the model on past resolved events for a city
and score it (Brier + log-loss) against the actual outcome bins.

For each event:
  1. Identify the resolved (closed) market within the event → its groupItemTitle
     is the realized bin (the one whose outcomePrices=["1","0"]).
  2. Reconstruct the predictive distribution using *historical forecast*
     temperatures for the target_date issued ~24h before resolution. We use
     the single-deterministic historical forecast API (no ensemble archive
     publicly available), then jitter it with the historical residual std to
     approximate a spread comparable to today's ensemble.
  3. P(realized_bin) from the model → Brier and log-loss.

This is intentionally simpler than the live pipeline because the ensemble
archive isn't open; the goal is to sanity-check that the model is not wildly
miscalibrated for the city.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

from pwa.models.bias import compute_bias
from pwa.models.kde import bins_to_probs
from pwa.polymarket.gamma import GammaClient, event_markets, parse_market_outcomes
from pwa.polymarket.parser import parse_event_bins, parse_event_title
from pwa.weather.open_meteo import historical_forecast_daily, observed_daily
from pwa.weather.stations import get_station


@dataclass(frozen=True, slots=True)
class CalibrationPoint:
    event_slug: str
    target_date: str
    realized_bin: str
    p_realized: float
    brier_contrib: float  # (p - 1)^2 for realized bin + sum p^2 for others
    log_loss: float       # -log(p_realized)


def _realized_bin_label(markets: list[dict]) -> str | None:
    for m in markets:
        _outcomes, prices = parse_market_outcomes(m)
        if not prices:
            continue
        if prices[0] >= 0.99:  # YES resolved true
            return m.get("groupItemTitle") or m.get("question")
    return None


def calibrate_city(city_key: str, n: int = 30, lookback_days: int = 60) -> list[CalibrationPoint]:
    station = get_station(city_key)
    if station is None:
        raise ValueError(f"Unknown city_key: {city_key}")

    with GammaClient() as gamma:
        search = gamma.search(f"highest temperature in {city_key.replace('-', ' ')}", limit_per_type=100)
        candidates = search.get("events") or []

    # Need fully-resolved events only.
    resolved = [e for e in candidates if e.get("closed")]
    resolved.sort(key=lambda e: e.get("endDate") or "", reverse=True)
    resolved = resolved[:n]

    # Bias correction once per city (uses last lookback days, model-only).
    bias = compute_bias(station.lat, station.lon, station.tz, "highest", lookback_days=lookback_days)

    out: list[CalibrationPoint] = []
    with GammaClient() as gamma:
        for e in resolved:
            slug = e.get("slug", "")
            try:
                full = gamma.get_event(slug)
            except Exception:
                continue
            info = parse_event_title(full.get("title", ""), end_date_iso=full.get("endDate"))
            if info is None:
                continue
            markets = list(event_markets(full))
            bin_pairs = parse_event_bins(markets)
            if not bin_pairs:
                continue
            realized_label = _realized_bin_label(markets)
            if realized_label is None:
                continue

            try:
                hist_forecast = historical_forecast_daily(
                    station.lat, station.lon, info.target_date, info.target_date,
                    station.tz, "highest",
                )
                fcst = hist_forecast.get(info.target_date)
                if fcst is None:
                    continue
            except Exception:
                continue

            # Synthesize "ensemble" by jittering the single forecast with the historical residual sd.
            sd = max(1.0, bias.std_residual)
            samples = np.random.default_rng(seed=42).normal(loc=fcst + bias.mean_bias, scale=sd, size=80)

            bins = [b for _, b in bin_pairs]
            probs = bins_to_probs(samples, bins)

            p_realized = next(
                (bp.p_model for (m, _), bp in zip(bin_pairs, probs)
                 if (m.get("groupItemTitle") or m.get("question")) == realized_label),
                None,
            )
            if p_realized is None or p_realized <= 0:
                continue

            brier = (p_realized - 1.0) ** 2 + sum(
                bp.p_model ** 2 for (m, _), bp in zip(bin_pairs, probs)
                if (m.get("groupItemTitle") or m.get("question")) != realized_label
            )
            log_loss = -float(np.log(max(p_realized, 1e-9)))

            out.append(CalibrationPoint(
                event_slug=slug,
                target_date=info.target_date.isoformat(),
                realized_bin=realized_label,
                p_realized=p_realized,
                brier_contrib=brier,
                log_loss=log_loss,
            ))

    return out


def summarize(points: list[CalibrationPoint]) -> dict[str, float]:
    if not points:
        return {"n": 0, "mean_brier": float("nan"), "mean_log_loss": float("nan"), "mean_p_realized": float("nan")}
    return {
        "n": float(len(points)),
        "mean_brier": float(np.mean([p.brier_contrib for p in points])),
        "mean_log_loss": float(np.mean([p.log_loss for p in points])),
        "mean_p_realized": float(np.mean([p.p_realized for p in points])),
    }
