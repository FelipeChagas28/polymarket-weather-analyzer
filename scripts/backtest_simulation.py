"""Backtest simulation: apply the consensus-gated betting strategy to past
closed temperature markets and report P&L starting from a $10 bankroll.

Limitations:
  - Polymarket CLOB's /prices-history is empty for most short-lived markets,
    so we approximate the entry price ~24h before resolution as
    `lastTradePrice - oneDayPriceChange` (clipped to [0.005, 0.995]).
  - We can't reconstruct the multi-source consensus historically (per-model
    Open-Meteo and yr.no don't expose archived forecasts the same way), so we
    treat the historical single-source Open-Meteo as the model and apply
    a "moderate" agreement gate (caps BUY, no STRONG BUY) to stay conservative.
  - Bias correction is computed once per (city, target_date) using the
    60-day window preceding that date.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np

from pwa.analysis.edge import evaluate_bin
from pwa.analysis.kelly import fractional_kelly
from pwa.models.bias import compute_bias
from pwa.models.kde import bins_to_probs
from pwa.polymarket.gamma import GammaClient, event_markets, parse_market_outcomes
from pwa.polymarket.parser import detect_unit, parse_event_bins, parse_event_title
from pwa.weather.open_meteo import historical_forecast_daily
from pwa.weather.stations import get_station


def _entry_price(market: dict[str, Any]) -> float | None:
    """Estimate price ~24h before resolution = lastTradePrice - oneDayPriceChange."""
    lt = market.get("lastTradePrice")
    chg = market.get("oneDayPriceChange") or 0.0
    if lt is None:
        return None
    p = float(lt) - float(chg)
    return max(0.005, min(0.995, p))


def _realized_bin(markets: list[dict[str, Any]]) -> str | None:
    for m in markets:
        _outcomes, prices = parse_market_outcomes(m)
        if prices and prices[0] >= 0.99:
            return m.get("groupItemTitle") or m.get("question")
    return None


def simulate(
    cities: list[str],
    n_per_city: int,
    bankroll_start: float = 10.0,
    kelly_fraction: float = 0.125,
    kelly_cap: float = 0.02,
    bias_lookback_days: int = 60,
) -> dict[str, Any]:
    bankroll = bankroll_start
    bets: list[dict[str, Any]] = []
    skipped_events = 0
    inspected_events = 0

    with GammaClient() as g:
        for city in cities:
            try:
                results = g.search(f"highest temperature in {city.replace('-', ' ')}", limit_per_type=80)
            except Exception:
                continue
            events = [e for e in (results.get("events") or []) if e.get("closed")]
            events.sort(key=lambda e: e.get("endDate") or "", reverse=True)
            events = events[:n_per_city]
            for ev_summary in events:
                inspected_events += 1
                slug = ev_summary.get("slug", "")
                try:
                    ev = g.get_event(slug)
                except Exception:
                    skipped_events += 1
                    continue
                info = parse_event_title(ev.get("title", ""), end_date_iso=ev.get("endDate"))
                station = get_station(info.city_key) if info else None
                if info is None or station is None:
                    skipped_events += 1
                    continue
                markets = list(event_markets(ev))
                bin_pairs = parse_event_bins(markets)
                if not bin_pairs:
                    skipped_events += 1
                    continue
                realized = _realized_bin(markets)
                if realized is None:
                    skipped_events += 1
                    continue
                bins = [b for _, b in bin_pairs]
                unit = detect_unit(bins)

                # Historical forecast model: single deterministic + bias-corrected jitter.
                try:
                    hf = historical_forecast_daily(
                        station.lat, station.lon, info.target_date, info.target_date,
                        station.tz, info.direction, unit=unit,
                    )
                    fcst = hf.get(info.target_date)
                    if fcst is None:
                        skipped_events += 1
                        continue
                    bias = compute_bias(
                        station.lat, station.lon, station.tz, info.direction,
                        today=info.target_date, lookback_days=bias_lookback_days, unit=unit,
                    )
                except Exception:
                    skipped_events += 1
                    continue
                sd = max(0.8 if unit == "C" else 1.5, bias.std_residual)
                samples = np.random.default_rng(seed=42).normal(
                    loc=fcst + bias.mean_bias, scale=sd, size=80,
                )
                try:
                    probs = bins_to_probs(samples, bins)
                except Exception:
                    skipped_events += 1
                    continue

                # Evaluate each bin and place Kelly bets (consensus gate = "moderate" to stay
                # conservative since we can't reconstruct multi-source consensus historically).
                for (market, b), bp in zip(bin_pairs, probs):
                    entry_yes = _entry_price(market)
                    if entry_yes is None:
                        continue
                    # Approximate bid as ask - 0.02 spread (typical Polymarket spread for these markets)
                    entry_bid = max(0.001, entry_yes - 0.02)
                    edge_row = evaluate_bin(b, bp.p_model, entry_yes, entry_bid, agreement="moderate")
                    if edge_row.recommendation == "SKIP":
                        continue
                    stake = bankroll * edge_row.kelly.capped
                    if stake < 0.01:
                        continue
                    # Resolve
                    bin_label = b.label
                    won = (realized == bin_label and edge_row.side == "YES") or \
                          (realized != bin_label and edge_row.side == "NO")
                    if edge_row.side == "YES":
                        price_paid = entry_yes
                    else:
                        price_paid = 1.0 - entry_bid
                    if won:
                        profit = stake * (1.0 - price_paid) / price_paid
                    else:
                        profit = -stake
                    bankroll += profit
                    bets.append({
                        "event": slug,
                        "bin": bin_label,
                        "side": edge_row.side,
                        "p_model": bp.p_model,
                        "price": price_paid,
                        "stake": stake,
                        "won": won,
                        "profit": profit,
                        "bankroll_after": bankroll,
                        "realized": realized,
                    })

    wins = sum(1 for b in bets if b["won"])
    losses = len(bets) - wins
    total_staked = sum(b["stake"] for b in bets)
    return {
        "bankroll_start": bankroll_start,
        "bankroll_end": bankroll,
        "n_events_inspected": inspected_events,
        "n_events_skipped": skipped_events,
        "n_bets": len(bets),
        "n_wins": wins,
        "n_losses": losses,
        "winrate": wins / len(bets) if bets else 0.0,
        "total_staked": total_staked,
        "roi_total": (bankroll - bankroll_start) / bankroll_start,
        "bets": bets,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cities", default="nyc,london,miami,chicago,seoul,tokyo,paris,madrid,toronto,buenos-aires")
    ap.add_argument("--n", type=int, default=10, help="N closed events per city")
    ap.add_argument("--bankroll", type=float, default=10.0)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    cities = [c.strip() for c in args.cities.split(",") if c.strip()]
    result = simulate(cities, n_per_city=args.n, bankroll_start=args.bankroll)
    print(f"\n{'=' * 60}")
    print(f"Backtest: {len(cities)} cidades, até {args.n} eventos cada")
    print(f"{'=' * 60}")
    print(f"Eventos inspecionados : {result['n_events_inspected']}")
    print(f"Eventos pulados       : {result['n_events_skipped']}")
    print(f"Apostas colocadas     : {result['n_bets']}")
    print(f"Vitórias / Derrotas   : {result['n_wins']} / {result['n_losses']}")
    print(f"Winrate               : {result['winrate']*100:.1f}%")
    print(f"Total apostado        : ${result['total_staked']:.2f}")
    print(f"Banca inicial         : ${result['bankroll_start']:.2f}")
    print(f"Banca final           : ${result['bankroll_end']:.2f}")
    print(f"ROI total             : {result['roi_total']*100:+.1f}%")
    if args.verbose and result["bets"]:
        print(f"\n{'─' * 60}")
        print(f"{'Event':40} {'Bin':10} {'Side':4} {'Price':>6} {'Stake':>6} {'P/L':>7}")
        print(f"{'─' * 60}")
        for b in result["bets"]:
            mark = "+" if b["won"] else "-"
            print(f"{b['event'][:40]:40} {b['bin'][:10]:10} {b['side']:4} "
                  f"{b['price']:.3f} ${b['stake']:5.2f} {mark}${abs(b['profit']):5.2f}")


if __name__ == "__main__":
    main()
