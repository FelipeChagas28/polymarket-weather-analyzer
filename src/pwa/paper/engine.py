"""Business logic: place bets, resolve open ones, compute summary metrics."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Any

from pwa.analysis.consensus import ConsensusRow
from pwa.analysis.edge import EdgeRow
from pwa.paper import db as pdb
from pwa.polymarket.gamma import GammaClient, event_markets, parse_market_outcomes


@dataclass(frozen=True, slots=True)
class PlacedBet:
    id: int
    event_slug: str
    bin_label: str
    side: str
    stake: float
    price_entry: float


@dataclass(frozen=True, slots=True)
class ResolvedBet:
    id: int
    event_slug: str
    bin_label: str
    side: str
    stake: float
    price_entry: float
    realized_bin: str | None
    status: str  # "won" | "lost" | "void"
    profit_loss: float


def _consensus_by_label(rows: list[ConsensusRow]) -> dict[str, ConsensusRow]:
    return {r.bin.label: r for r in rows}


def reserved_stake(conn: sqlite3.Connection) -> float:
    row = conn.execute("SELECT COALESCE(SUM(stake), 0) AS s FROM bets WHERE status = 'open'").fetchone()
    return float(row["s"])


def place_bets_for_event(
    conn: sqlite3.Connection,
    event_slug: str,
    event_title: str,
    city_key: str,
    target_date: date,
    edge_rows: list[EdgeRow],
    consensus_rows: list[ConsensusRow],
    mode: str = "auto",
) -> list[PlacedBet]:
    """Persist one bet per BUY/STRONG BUY recommendation. Returns list of placed bets.

    `mode`:
      - "auto"  : place every BUY/STRONG BUY surviving the consensus gate.
      - "strict": place only when agreement == 'strong'.
    """
    placed: list[PlacedBet] = []
    consensus_by_label = _consensus_by_label(consensus_rows)
    bankroll = pdb.get_bankroll(conn)
    available = max(0.0, bankroll - reserved_stake(conn))

    for er in edge_rows:
        if er.recommendation not in ("BUY", "STRONG BUY"):
            continue
        if er.side_price is None or er.kelly is None:
            continue
        crow = consensus_by_label.get(er.bin.label)
        agreement = crow.agreement if crow else "unknown"
        if mode == "strict" and agreement != "strong":
            continue

        # Stake sizing: Kelly capped, also bounded by available (unreserved) bankroll.
        stake = bankroll * er.kelly.capped
        stake = min(stake, available)
        if stake < 0.01:
            continue
        shares = stake / er.side_price

        new_id = pdb.insert_bet(
            conn,
            placed_at=pdb.now_iso(),
            event_slug=event_slug,
            event_title=event_title,
            city_key=city_key,
            target_date=target_date.isoformat(),
            bin_label=er.bin.label,
            side=er.side,
            price_entry=er.side_price,
            stake=stake,
            shares=shares,
            p_consenso=er.p_model,
            p_om_ens=(crow.per_source_prob.get("open-meteo-ensemble") if crow else None),
            agreement=agreement,
            recommendation=er.recommendation,
        )
        if new_id is None:
            continue  # blocked by UNIQUE on open dupes
        available -= stake
        placed.append(PlacedBet(
            id=new_id, event_slug=event_slug, bin_label=er.bin.label,
            side=er.side, stake=stake, price_entry=er.side_price,
        ))
    return placed


def _realized_bin_label(markets: list[dict[str, Any]]) -> str | None:
    for m in markets:
        _outcomes, prices = parse_market_outcomes(m)
        if prices and prices[0] >= 0.99:
            return m.get("groupItemTitle") or m.get("question")
    return None


def resolve_open_bets(conn: sqlite3.Connection, as_of: date) -> list[ResolvedBet]:
    """Resolve all open bets whose target_date is strictly before `as_of`."""
    due = pdb.list_bets_due(conn, as_of=as_of.isoformat())
    if not due:
        return []

    # Cache event lookups: many bets may share an event.
    event_cache: dict[str, tuple[str | None, bool]] = {}  # slug -> (realized_bin, is_void)
    resolved: list[ResolvedBet] = []

    with GammaClient() as g:
        for bet in due:
            if bet.event_slug not in event_cache:
                try:
                    ev = g.get_event(bet.event_slug)
                    markets = list(event_markets(ev))
                    closed = ev.get("closed", False)
                    if not closed:
                        # Market hasn't resolved yet — leave the bet open.
                        event_cache[bet.event_slug] = (None, False)
                        continue
                    realized = _realized_bin_label(markets)
                    event_cache[bet.event_slug] = (realized, realized is None)
                except Exception:
                    # Treat unfetchable events as void: refund stake.
                    event_cache[bet.event_slug] = (None, True)

            realized_bin, is_void = event_cache[bet.event_slug]
            if realized_bin is None and not is_void:
                continue  # still pending in the cache

            bankroll = pdb.get_bankroll(conn)
            if is_void or realized_bin is None:
                # Void: stake never escrowed in bankroll, so P/L is zero.
                pdb.update_bet_resolution(conn, bet.id, "void", None, 0.0, bankroll)
                resolved.append(ResolvedBet(
                    id=bet.id, event_slug=bet.event_slug, bin_label=bet.bin_label,
                    side=bet.side, stake=bet.stake, price_entry=bet.price_entry,
                    realized_bin=None, status="void", profit_loss=0.0,
                ))
                continue

            yes_won = realized_bin == bet.bin_label
            won = (bet.side == "YES" and yes_won) or (bet.side == "NO" and not yes_won)
            if won:
                # Bought `shares` at price_entry; each share pays $1 on win → revenue = shares.
                # Net P/L = shares - stake = stake * (1 - price)/price.
                pnl = bet.stake * (1.0 - bet.price_entry) / bet.price_entry
                status = "won"
            else:
                pnl = -bet.stake
                status = "lost"

            new_bankroll = bankroll + pnl
            pdb.update_bet_resolution(conn, bet.id, status, realized_bin, pnl, new_bankroll)
            pdb.update_bankroll(conn, new_bankroll)
            resolved.append(ResolvedBet(
                id=bet.id, event_slug=bet.event_slug, bin_label=bet.bin_label,
                side=bet.side, stake=bet.stake, price_entry=bet.price_entry,
                realized_bin=realized_bin, status=status, profit_loss=pnl,
            ))
    return resolved


@dataclass(frozen=True, slots=True)
class Summary:
    bankroll_start: float
    bankroll_current: float
    roi_pct: float
    n_open: float
    n_resolved: int
    n_won: int
    n_lost: int
    n_void: int
    winrate: float
    by_agreement: dict[str, tuple[int, int]]  # agreement -> (won, lost)


def compute_summary(conn: sqlite3.Connection) -> Summary:
    start = float(pdb.get_state(conn, "bankroll_start") or 0.0)
    current = pdb.get_bankroll(conn)
    counts = {row["status"]: row["c"] for row in conn.execute(
        "SELECT status, COUNT(*) AS c FROM bets GROUP BY status"
    ).fetchall()}
    n_won = counts.get("won", 0)
    n_lost = counts.get("lost", 0)
    n_void = counts.get("void", 0)
    n_open = counts.get("open", 0)
    n_resolved = n_won + n_lost + n_void
    winrate = n_won / (n_won + n_lost) if (n_won + n_lost) > 0 else 0.0
    roi = ((current - start) / start * 100.0) if start > 0 else 0.0

    by_agreement: dict[str, tuple[int, int]] = {}
    rows = conn.execute(
        "SELECT agreement, status, COUNT(*) AS c FROM bets "
        "WHERE status IN ('won','lost') GROUP BY agreement, status"
    ).fetchall()
    for r in rows:
        w, l = by_agreement.get(r["agreement"], (0, 0))
        if r["status"] == "won":
            w += r["c"]
        else:
            l += r["c"]
        by_agreement[r["agreement"]] = (w, l)

    return Summary(
        bankroll_start=start, bankroll_current=current, roi_pct=roi,
        n_open=n_open, n_resolved=n_resolved, n_won=n_won, n_lost=n_lost,
        n_void=n_void, winrate=winrate, by_agreement=by_agreement,
    )
