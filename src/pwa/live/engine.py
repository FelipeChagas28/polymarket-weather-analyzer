"""Live trading engine: place real-money bets, resolve, redeem on-chain.

Mirrors the surface of ``paper.engine`` so the orchestration in ``cli.py`` /
``paper run`` can call the same shape of functions. The differences:

  * Stakes are bounded by a hard absolute cap (default $2.00) on top of the
    Kelly-fraction cap, and obey Polymarket's $1.00 per-order minimum.
  * Bet placement goes through ``execution.submit_hybrid`` (GTD→FAK), so the
    persisted row also records ``order_id``, ``fill_price``, ``fees_paid`` and
    ``shares_filled``.
  * Resolution still uses the Gamma ``event.closed`` check (cheap), and a
    winning bet additionally triggers an on-chain ``redeemPositions`` call to
    actually pull USD back to the wallet.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Any

from pwa.analysis.consensus import ConsensusRow
from pwa.analysis.edge import EdgeRow
from pwa.paper.engine import compute_stake_from_kelly
from pwa.polymarket.gamma import GammaClient, event_markets, parse_clob_token_ids, parse_market_outcomes

from .chain import PolygonClient
from . import ctf as ctf_mod
from .clob_trade import ClobTradeClient
from . import db as ldb
from .execution import ExecutionResult, submit_hybrid

REAL_STAKE_HARD_CAP: float = 2.0
POLYMARKET_MIN_ORDER_USD: float = 1.0


@dataclass(frozen=True, slots=True)
class PlacedLiveBet:
    id: int
    event_slug: str
    bin_label: str
    side: str
    stake: float
    fill_price: float
    shares_filled: float
    order_id: str | None
    fees_paid: float
    used_fallback: bool


@dataclass(frozen=True, slots=True)
class ResolvedLiveBet:
    id: int
    event_slug: str
    bin_label: str
    side: str
    stake: float
    price_entry: float
    realized_bin: str | None
    status: str               # "won" | "lost" | "void"
    profit_loss: float
    condition_id: str | None


@dataclass(frozen=True, slots=True)
class RedeemedBet:
    bet_id: int
    condition_id: str
    tx_hash: str
    status: int               # 1 = success, 0 = reverted


def _consensus_by_label(rows: list[ConsensusRow]) -> dict[str, ConsensusRow]:
    return {r.bin.label: r for r in rows}


def _market_index_by_label(markets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Map ``groupItemTitle`` → market payload for fast lookup at bet time."""
    out: dict[str, dict[str, Any]] = {}
    for m in markets:
        label = m.get("groupItemTitle") or m.get("question")
        if label:
            out[label] = m
    return out


def _yes_token_id(market: dict[str, Any]) -> str | None:
    """The YES token is always index 0 of ``clobTokenIds`` on Polymarket binary markets."""
    ids = parse_clob_token_ids(market)
    return ids[0] if ids else None


def reserved_stake(conn: sqlite3.Connection) -> float:
    row = conn.execute("SELECT COALESCE(SUM(stake), 0) AS s FROM bets WHERE status = 'open'").fetchone()
    return float(row["s"])


def place_live_bets_for_event(
    conn: sqlite3.Connection,
    clob: ClobTradeClient,
    event_slug: str,
    event_title: str,
    city_key: str,
    target_date: date,
    edge_rows: list[EdgeRow],
    consensus_rows: list[ConsensusRow],
    markets: list[dict[str, Any]],
    *,
    hard_cap: float = REAL_STAKE_HARD_CAP,
    min_stake: float = POLYMARKET_MIN_ORDER_USD,
) -> list[PlacedLiveBet]:
    """Place every BUY/STRONG BUY recommendation that survives gates + min stake.

    Unlike the paper engine, only ``side == "YES"`` is supported for now — the
    bot buys YES on the bin it predicts. Selling NO via CLOB has slightly
    different routing we'll add in a later phase.
    """
    placed: list[PlacedLiveBet] = []
    consensus_by_label = _consensus_by_label(consensus_rows)
    market_by_label = _market_index_by_label(markets)
    bankroll = ldb.get_bankroll(conn)
    available = max(0.0, bankroll - reserved_stake(conn))

    for er in edge_rows:
        if er.recommendation not in ("BUY", "STRONG BUY"):
            continue
        if er.side != "YES":
            continue  # NO-side routing TBD; skip for now.
        if er.side_price is None or er.kelly is None:
            continue

        crow = consensus_by_label.get(er.bin.label)
        agreement = crow.agreement if crow else "unknown"

        stake = compute_stake_from_kelly(
            bankroll, available, er.kelly.capped,
            hard_cap=hard_cap, min_stake=min_stake,
        )
        if stake == 0.0:
            continue

        market = market_by_label.get(er.bin.label)
        if market is None:
            continue
        token_id = _yes_token_id(market)
        condition_id = market.get("conditionId")
        if not token_id or not condition_id:
            continue

        shares = stake / er.side_price
        result: ExecutionResult = submit_hybrid(
            clob, token_id=token_id, price=er.side_price, size=shares,
        )
        if result.shares_filled <= 0:
            continue  # nothing executed — don't persist a dud row

        actual_stake = result.shares_filled * result.fill_price
        new_id = ldb.insert_bet(
            conn,
            placed_at=ldb.now_iso(),
            event_slug=event_slug,
            event_title=event_title,
            city_key=city_key,
            target_date=target_date.isoformat(),
            bin_label=er.bin.label,
            side=er.side,
            price_entry=er.side_price,
            stake=actual_stake,
            shares=result.shares_filled,
            p_consenso=er.p_model,
            p_om_ens=(crow.per_source_prob.get("open-meteo-ensemble") if crow else None),
            agreement=agreement,
            recommendation=er.recommendation,
            token_id=token_id,
            condition_id=condition_id,
            order_id=result.order_id,
            fees_paid=result.fees_paid,
            fill_price=result.fill_price,
            shares_filled=result.shares_filled,
        )
        if new_id is None:
            continue
        available -= actual_stake
        placed.append(PlacedLiveBet(
            id=new_id, event_slug=event_slug, bin_label=er.bin.label,
            side=er.side, stake=actual_stake, fill_price=result.fill_price,
            shares_filled=result.shares_filled, order_id=result.order_id,
            fees_paid=result.fees_paid, used_fallback=result.used_fallback,
        ))
    return placed


def _realized_bin_label(markets: list[dict[str, Any]]) -> str | None:
    for m in markets:
        _outcomes, prices = parse_market_outcomes(m)
        if prices and prices[0] >= 0.99:
            return m.get("groupItemTitle") or m.get("question")
    return None


def resolve_live_bets(conn: sqlite3.Connection, as_of: date) -> list[ResolvedLiveBet]:
    """Resolve open bets whose target_date is strictly before ``as_of``.

    Uses the same Gamma ``event.closed`` flag as the paper engine — cheap, no
    on-chain reads. Actual redeem happens separately in ``redeem_resolved``.
    """
    due = ldb.list_bets_due(conn, as_of=as_of.isoformat())
    if not due:
        return []

    event_cache: dict[str, tuple[str | None, bool]] = {}
    resolved: list[ResolvedLiveBet] = []

    with GammaClient() as g:
        for bet in due:
            if bet.event_slug not in event_cache:
                try:
                    ev = g.get_event(bet.event_slug)
                    markets = list(event_markets(ev))
                    closed = ev.get("closed", False)
                    if not closed:
                        event_cache[bet.event_slug] = (None, False)
                        continue
                    realized = _realized_bin_label(markets)
                    event_cache[bet.event_slug] = (realized, realized is None)
                except Exception:
                    event_cache[bet.event_slug] = (None, True)

            realized_bin, is_void = event_cache[bet.event_slug]
            if realized_bin is None and not is_void:
                continue

            bankroll = ldb.get_bankroll(conn)
            if is_void or realized_bin is None:
                ldb.update_bet_resolution(conn, bet.id, "void", None, 0.0, bankroll)
                resolved.append(ResolvedLiveBet(
                    id=bet.id, event_slug=bet.event_slug, bin_label=bet.bin_label,
                    side=bet.side, stake=bet.stake, price_entry=bet.price_entry,
                    realized_bin=None, status="void", profit_loss=0.0,
                    condition_id=bet.condition_id,
                ))
                continue

            yes_won = realized_bin == bet.bin_label
            won = (bet.side == "YES" and yes_won) or (bet.side == "NO" and not yes_won)
            if won:
                pnl = bet.stake * (1.0 - bet.price_entry) / bet.price_entry
                status = "won"
            else:
                pnl = -bet.stake
                status = "lost"

            new_bankroll = bankroll + pnl
            ldb.update_bet_resolution(conn, bet.id, status, realized_bin, pnl, new_bankroll)
            ldb.update_bankroll(conn, new_bankroll)
            resolved.append(ResolvedLiveBet(
                id=bet.id, event_slug=bet.event_slug, bin_label=bet.bin_label,
                side=bet.side, stake=bet.stake, price_entry=bet.price_entry,
                realized_bin=realized_bin, status=status, profit_loss=pnl,
                condition_id=bet.condition_id,
            ))
    return resolved


def redeem_resolved(conn: sqlite3.Connection, chain: PolygonClient) -> list[RedeemedBet]:
    """Burn winning ERC-1155 positions to pull USD back to the wallet.

    Idempotent: we group winners by ``condition_id`` so a market with multiple
    winning bins (shouldn't happen, but defensive) only triggers one tx. Bets
    already marked with a ``tx_hash`` are skipped.
    """
    winners = ldb.list_won_bets_pending_redeem(conn)
    if not winners:
        return []

    bets_by_condition: dict[str, list[Any]] = {}
    for b in winners:
        if not b.condition_id:
            continue
        bets_by_condition.setdefault(b.condition_id, []).append(b)

    redeemed: list[RedeemedBet] = []
    for cid, bets in bets_by_condition.items():
        op_id = ldb.insert_chain_op(
            conn, kind="redeem", status="pending",
            payload_json=json.dumps({"condition_id": cid, "bet_ids": [b.id for b in bets]}),
        )
        try:
            result = ctf_mod.redeem_position(chain, cid)
        except Exception as e:
            ldb.update_chain_op_status(conn, op_id, status=f"error:{type(e).__name__}")
            continue

        ldb.update_chain_op_status(
            conn, op_id,
            status="success" if result.status == 1 else "reverted",
            gas_used=result.gas_used,
            tx_hash=result.tx_hash,
        )
        for b in bets:
            ldb.update_bet_redemption(conn, b.id, result.tx_hash)
            redeemed.append(RedeemedBet(
                bet_id=b.id, condition_id=cid, tx_hash=result.tx_hash, status=result.status,
            ))
    return redeemed
