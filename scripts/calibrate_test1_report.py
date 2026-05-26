"""One-shot: roda `pwa.backtest.calibrate.calibrate_city` em todas as cidades
com >=5 apostas no Test 1 (~/.pwa/paper.db) e gera um markdown cruzando o
P/L atual do Test 1 com as metricas de calibracao (Brier, log-loss).

Saida: reports/calibration_test1_<YYYY-MM-DD>.md

Read-only no paper.db; nao mexe em ~/.pwa/cache.db (calibrate_city so le).
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import traceback
from datetime import date
from pathlib import Path

from pwa.backtest.calibrate import calibrate_city, summarize

MIN_BETS_PER_CITY = 5
DEFAULT_PAPER_DB = Path.home() / ".pwa" / "paper.db"


def fetch_test1_stats(db_path: Path) -> tuple[dict, list[dict]]:
    """Return (totals, per_city_rows). per_city_rows sorted by P/L asc."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    totals_row = cur.execute(
        """
        SELECT
            COUNT(*) AS n_total,
            SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END) AS losses,
            SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open_,
            COALESCE(SUM(CASE WHEN status IN ('won','lost') THEN profit_loss ELSE 0 END), 0) AS pl
        FROM bets
        """
    ).fetchone()

    bankroll_row = cur.execute(
        "SELECT value FROM state WHERE key='bankroll_current'"
    ).fetchone()
    initial_row = cur.execute(
        "SELECT value FROM state WHERE key='bankroll_start'"
    ).fetchone()

    totals = {
        "n_total": totals_row["n_total"] or 0,
        "wins": totals_row["wins"] or 0,
        "losses": totals_row["losses"] or 0,
        "open": totals_row["open_"] or 0,
        "pl": float(totals_row["pl"] or 0.0),
        "bankroll": float(bankroll_row["value"]) if bankroll_row else None,
        "initial_bankroll": float(initial_row["value"]) if initial_row else None,
    }

    rows = cur.execute(
        """
        SELECT
            city_key,
            COUNT(*) AS n_total,
            SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END) AS losses,
            SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open_,
            COALESCE(SUM(CASE WHEN status IN ('won','lost') THEN profit_loss ELSE 0 END), 0) AS pl
        FROM bets
        GROUP BY city_key
        ORDER BY pl ASC
        """
    ).fetchall()

    per_city = []
    for r in rows:
        resolved = (r["wins"] or 0) + (r["losses"] or 0)
        winrate = (r["wins"] / resolved * 100.0) if resolved else None
        per_city.append({
            "city_key": r["city_key"],
            "n_total": r["n_total"],
            "wins": r["wins"] or 0,
            "losses": r["losses"] or 0,
            "open": r["open_"] or 0,
            "pl": float(r["pl"] or 0.0),
            "winrate": winrate,
        })

    conn.close()
    return totals, per_city


def run_calibration(city_key: str, n: int, lookback: int) -> dict:
    """Wraps calibrate_city with error capture."""
    try:
        points = calibrate_city(city_key, n=n, lookback_days=lookback)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    summary = summarize(points)
    return {
        "n": int(summary["n"]),
        "mean_brier": summary["mean_brier"],
        "mean_log_loss": summary["mean_log_loss"],
        "mean_p_realized": summary["mean_p_realized"],
    }


def render_markdown(
    totals: dict,
    per_city: list[dict],
    calibration: dict[str, dict],
    today: date,
    min_bets: int,
) -> str:
    lines: list[str] = []
    lines.append(f"# Calibration Report — Test 1 (paper.db) — {today.isoformat()}")
    lines.append("")
    lines.append(
        "Cross-reference between current Test 1 P/L per city and "
        "`calibrate_city` metrics (Brier, log-loss, P(realized))."
    )
    lines.append("")
    lines.append(
        f"Filter: cities with >= {min_bets} total bets in `~/.pwa/paper.db`. "
        f"`calibrate_city` is diagnostic only — nothing in the paper DB or "
        f"bias cache was modified."
    )
    lines.append("")

    bankroll = totals.get("bankroll")
    initial = totals.get("initial_bankroll")
    roi = ((bankroll - initial) / initial * 100.0) if (bankroll and initial) else None
    lines.append("## 1. Test 1 snapshot")
    lines.append("")
    if bankroll is not None:
        roi_str = f"{roi:+.1f}%" if roi is not None else "n/a"
        lines.append(
            f"- Bankroll: **${bankroll:.2f}** (initial ${initial:.2f}, ROI {roi_str})"
        )
    lines.append(
        f"- Bets total: {totals['n_total']} | resolved: "
        f"{totals['wins']}W / {totals['losses']}L | open: {totals['open']} | "
        f"net P/L on resolved: **${totals['pl']:+.2f}**"
    )
    lines.append("")
    lines.append("| City | total | W | L | open | winrate | net P/L |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for c in per_city:
        wr = f"{c['winrate']:.0f}%" if c["winrate"] is not None else "—"
        lines.append(
            f"| {c['city_key']} | {c['n_total']} | {c['wins']} | "
            f"{c['losses']} | {c['open']} | {wr} | {c['pl']:+.2f} |"
        )
    lines.append("")

    lines.append("## 2. Calibration per city")
    lines.append("")
    lines.append(
        "Each row reconstructs the model on past resolved events for the city "
        "(historical deterministic forecast + jittered ensemble) and scores "
        "P(realized bin). Lower Brier/log-loss = better calibrated. "
        "P(realized) is the average probability the model assigned to the "
        "bin that actually won."
    )
    lines.append("")
    lines.append("| City | n events | Brier mean | log-loss mean | P(realized) mean |")
    lines.append("|---|---:|---:|---:|---:|")
    for c in per_city:
        cal = calibration.get(c["city_key"])
        if cal is None:
            continue
        if "error" in cal:
            lines.append(f"| {c['city_key']} | — | — | — | ERR: {cal['error']} |")
            continue
        if cal["n"] == 0:
            lines.append(f"| {c['city_key']} | 0 | — | — | — |")
            continue
        lines.append(
            f"| {c['city_key']} | {cal['n']} | {cal['mean_brier']:.3f} | "
            f"{cal['mean_log_loss']:.3f} | {cal['mean_p_realized']*100:.1f}% |"
        )
    lines.append("")

    lines.append("## 3. Cross-reference: calibration vs P/L")
    lines.append("")
    lines.append(
        "Sorted by Brier descending (worst-calibrated first). "
        "`[!]` marks cities where Brier > 0.30 **and** Test 1 P/L < 0 — "
        "miscalibration plausibly explains the loss, candidate to pause."
    )
    lines.append("")
    lines.append("| flag | City | Brier | log-loss | P(realized) | n events | Test 1 P/L | W/L |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|")

    rows_with_cal = []
    for c in per_city:
        cal = calibration.get(c["city_key"])
        if cal is None or "error" in cal or cal["n"] == 0:
            continue
        rows_with_cal.append((c, cal))
    rows_with_cal.sort(key=lambda x: x[1]["mean_brier"], reverse=True)

    flagged = []
    for c, cal in rows_with_cal:
        flag = "[!]" if cal["mean_brier"] > 0.30 and c["pl"] < 0 else ""
        if flag:
            flagged.append(c["city_key"])
        lines.append(
            f"| {flag} | {c['city_key']} | {cal['mean_brier']:.3f} | "
            f"{cal['mean_log_loss']:.3f} | {cal['mean_p_realized']*100:.1f}% | "
            f"{cal['n']} | {c['pl']:+.2f} | {c['wins']}/{c['losses']} |"
        )

    no_data = [c["city_key"] for c in per_city
               if c["city_key"] not in calibration
               or calibration[c["city_key"]].get("n", 0) == 0
               or "error" in calibration[c["city_key"]]]
    if no_data:
        lines.append("")
        lines.append(
            f"_Cities without usable calibration data (skipped above): "
            f"{', '.join(no_data)}._"
        )
    lines.append("")

    lines.append("## 4. Observations")
    lines.append("")
    losers_no_miscal = [
        c["city_key"] for c, cal in rows_with_cal
        if c["pl"] < 0 and cal["mean_brier"] <= 0.30
    ]
    winners_high_brier = [
        c["city_key"] for c, cal in rows_with_cal
        if c["pl"] > 0 and cal["mean_brier"] > 0.30
    ]
    if flagged:
        lines.append(
            f"- **Miscalibrated losers** (Brier > 0.30 + P/L < 0): "
            f"`{', '.join(flagged)}`. Consider pausing these in `paper run`."
        )
    else:
        lines.append("- No city hit the `Brier > 0.30 + P/L < 0` flag threshold.")
    if losers_no_miscal:
        lines.append(
            f"- **Losing but well-calibrated**: `{', '.join(losers_no_miscal)}`. "
            "Loss likely comes from price/edge gate, not from bad weather model."
        )
    if winners_high_brier:
        lines.append(
            f"- **Winning despite high Brier**: `{', '.join(winners_high_brier)}`. "
            "Could be variance — small sample."
        )
    low_n = [
        c["city_key"] for c, cal in rows_with_cal if cal["n"] < 5
    ]
    if low_n:
        lines.append(
            f"- **Low-confidence calibration** (n events < 5): "
            f"`{', '.join(low_n)}`. Treat Brier values as noisy."
        )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "_Generated by `scripts/calibrate_test1_report.py`. "
        "Calibration model uses single deterministic historical forecast + "
        "jittered residual std (not the same multi-source ensemble used live), "
        "so absolute Brier is a sanity check, not a precise audit._"
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_PAPER_DB))
    parser.add_argument("--min-bets", type=int, default=MIN_BETS_PER_CITY)
    parser.add_argument("--n", type=int, default=30,
                        help="max resolved events per city for calibrate")
    parser.add_argument("--lookback", type=int, default=60)
    parser.add_argument("--out", default=None,
                        help="output markdown path (default reports/calibration_test1_<date>.md)")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: paper DB not found at {db_path}", file=sys.stderr)
        return 2

    print(f"Reading Test 1 stats from {db_path} ...", flush=True)
    totals, per_city = fetch_test1_stats(db_path)
    print(f"  {len(per_city)} cities with bets, total {totals['n_total']}, "
          f"P/L resolved ${totals['pl']:+.2f}", flush=True)

    eligible = [c for c in per_city if c["n_total"] >= args.min_bets]
    print(f"Running calibrate_city on {len(eligible)} cities "
          f"(filter: >= {args.min_bets} bets)...", flush=True)

    calibration: dict[str, dict] = {}
    for idx, c in enumerate(eligible, start=1):
        city = c["city_key"]
        print(f"  [{idx}/{len(eligible)}] {city} ...", end="", flush=True)
        result = run_calibration(city, n=args.n, lookback=args.lookback)
        if "error" in result:
            print(f" ERR ({result['error']})", flush=True)
        else:
            print(f" n={result['n']} brier={result['mean_brier']:.3f}", flush=True)
        calibration[city] = result

    today = date.today()
    out_path = Path(args.out) if args.out else (
        Path("reports") / f"calibration_test1_{today.isoformat()}.md"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    md = render_markdown(totals, per_city, calibration, today, args.min_bets)
    out_path.write_text(md, encoding="utf-8")
    print(f"\nReport written to {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nAborted by user", file=sys.stderr)
        raise SystemExit(130)
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
