"""Tests for live/engine.py — bet placement, resolution, redeem orchestration."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pwa.analysis.consensus import ConsensusRow
from pwa.analysis.edge import EdgeRow
from pwa.analysis.kelly import KellySize
from pwa.live import db as ldb
from pwa.live import engine as lengine
from pwa.polymarket.parser import Bin


@pytest.fixture
def conn(tmp_path):
    with ldb.session(tmp_path / "real.db") as c:
        ldb.init_state(c, bankroll=25.0, wallet_address="0xwallet", wallet_keystore_path="/tmp/w")
        yield c


def _bin(label: str) -> Bin:
    return Bin(label=label, lower=70.0, upper=72.0, unit="F")


def _edge_row(label: str, *, p=0.6, ask=0.4, rec="BUY", side="YES", kelly_capped=0.05) -> EdgeRow:
    return EdgeRow(
        bin=_bin(label),
        p_model=p, yes_ask=ask, yes_bid=ask - 0.01,
        side=side, side_price=ask, edge=p - ask, ev=p * (1 - ask) - (1 - p) * ask,
        kelly=KellySize(full_kelly=0.1, fractional_kelly=0.02, capped=kelly_capped),
        recommendation=rec,
    )


def _consensus_row(label: str, *, agreement="strong") -> ConsensusRow:
    return ConsensusRow(
        bin=_bin(label),
        per_source_prob={"open-meteo-ensemble": 0.6},
        consensus_prob=0.6, spread_pp=10.0, agreement=agreement,
        side="YES", conflicts_with_primary=False,
    )


def _market(label: str, *, condition_id: str = "0xCID", yes_token: str = "tok-yes") -> dict:
    return {
        "groupItemTitle": label,
        "conditionId": condition_id,
        "clobTokenIds": f'["{yes_token}", "tok-no"]',
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.4", "0.6"]',
    }


def _mock_clob(*, fill_price=0.4, shares_filled=None, order_id="ord-1"):
    """Build a ClobTradeClient mock that returns a successful hybrid execution."""
    clob = MagicMock()
    return clob, order_id


# ---------- place_live_bets ---------------------------------------------------

def test_place_live_bets_happy_path(conn, monkeypatch):
    er = _edge_row("70-72°F")
    cr = _consensus_row("70-72°F")
    mkts = [_market("70-72°F")]

    # Bankroll $25, kelly.capped=0.05 → stake = $1.25; shares = 1.25 / 0.4 = 3.125.
    fake_result = SimpleNamespace(
        status="filled", order_id="ord-1", shares_filled=3.125,
        shares_requested=3.125, fill_price=0.4, fees_paid=0.0,
        used_fallback=False, detail="gtd_full",
    )
    monkeypatch.setattr(lengine, "submit_hybrid", lambda *a, **kw: fake_result)

    placed = lengine.place_live_bets_for_event(
        conn=conn, clob=MagicMock(), event_slug="ev-1",
        event_title="Highest temp NYC", city_key="nyc",
        target_date=date(2026, 5, 27),
        edge_rows=[er], consensus_rows=[cr], markets=mkts,
    )
    assert len(placed) == 1
    b = placed[0]
    assert b.stake == pytest.approx(1.25, abs=1e-9)
    assert b.fill_price == 0.4
    assert b.shares_filled == 3.125

    # Row persisted with on-chain fields.
    row = conn.execute("SELECT * FROM bets WHERE id = ?", (b.id,)).fetchone()
    assert row["token_id"] == "tok-yes"
    assert row["condition_id"] == "0xCID"
    assert row["order_id"] == "ord-1"


def test_place_skips_no_side(conn, monkeypatch):
    """Phase-1 live engine only routes YES — NO-side recommendations must skip."""
    er = _edge_row("70-72°F", side="NO")
    monkeypatch.setattr(lengine, "submit_hybrid", lambda *a, **kw: pytest.fail("should not be called"))
    placed = lengine.place_live_bets_for_event(
        conn=conn, clob=MagicMock(), event_slug="ev-1", event_title="t",
        city_key="nyc", target_date=date(2026, 5, 27),
        edge_rows=[er], consensus_rows=[_consensus_row("70-72°F")],
        markets=[_market("70-72°F")],
    )
    assert placed == []


def test_place_skips_below_min_stake(conn, monkeypatch):
    """A Kelly that produces $0.50 < $1 min should be skipped silently."""
    # kelly.capped = 0.01 → stake = 25 * 0.01 = $0.25 < $1
    er = _edge_row("70-72°F", kelly_capped=0.01)
    monkeypatch.setattr(lengine, "submit_hybrid", lambda *a, **kw: pytest.fail("not reached"))
    placed = lengine.place_live_bets_for_event(
        conn=conn, clob=MagicMock(), event_slug="ev", event_title="t",
        city_key="nyc", target_date=date(2026, 5, 27),
        edge_rows=[er], consensus_rows=[_consensus_row("70-72°F")],
        markets=[_market("70-72°F")],
    )
    assert placed == []


def test_place_applies_hard_cap(conn, monkeypatch):
    """A huge Kelly should be clamped to the $2 hard cap."""
    er = _edge_row("70-72°F", kelly_capped=0.5)  # 25 * 0.5 = $12.50 raw
    captured = {}

    def fake_submit(clob, *, token_id, price, size, **kw):
        captured["size"] = size
        captured["price"] = price
        return SimpleNamespace(
            status="filled", order_id="o1", shares_filled=size,
            shares_requested=size, fill_price=price, fees_paid=0.0,
            used_fallback=False, detail="",
        )

    monkeypatch.setattr(lengine, "submit_hybrid", fake_submit)
    placed = lengine.place_live_bets_for_event(
        conn=conn, clob=MagicMock(), event_slug="ev", event_title="t",
        city_key="nyc", target_date=date(2026, 5, 27),
        edge_rows=[er], consensus_rows=[_consensus_row("70-72°F")],
        markets=[_market("70-72°F")],
    )
    assert len(placed) == 1
    # Stake should be exactly $2.00 (the hard cap), not $12.50.
    assert placed[0].stake == pytest.approx(2.0, abs=1e-6)
    assert captured["size"] == pytest.approx(2.0 / 0.4, abs=1e-6)


def test_place_skips_when_zero_filled(conn, monkeypatch):
    """If execution reports zero shares filled, no row is persisted."""
    er = _edge_row("70-72°F")
    monkeypatch.setattr(lengine, "submit_hybrid", lambda *a, **kw: SimpleNamespace(
        status="failed", order_id="o1", shares_filled=0.0,
        shares_requested=5.0, fill_price=0.4, fees_paid=0.0,
        used_fallback=True, detail="fak_zero",
    ))
    placed = lengine.place_live_bets_for_event(
        conn=conn, clob=MagicMock(), event_slug="ev", event_title="t",
        city_key="nyc", target_date=date(2026, 5, 27),
        edge_rows=[er], consensus_rows=[_consensus_row("70-72°F")],
        markets=[_market("70-72°F")],
    )
    assert placed == []
    assert conn.execute("SELECT COUNT(*) FROM bets").fetchone()[0] == 0


# ---------- resolve_live_bets -------------------------------------------------

def test_resolve_wins_lose_and_void(conn, monkeypatch):
    """Three open bets across two events: one wins, one loses, one void (event unfetchable)."""
    # Setup: three open bets on three different events to avoid UNIQUE conflict.
    for i, (label, event_slug, target) in enumerate([
        ("70-72°F", "ev-win", "2026-05-25"),
        ("73-75°F", "ev-lose", "2026-05-25"),
        ("76-78°F", "ev-void", "2026-05-25"),
    ]):
        ldb.insert_bet(
            conn,
            placed_at="2026-05-24T00:00:00+00:00",
            event_slug=event_slug, event_title="t",
            city_key="nyc", target_date=target,
            bin_label=label, side="YES",
            price_entry=0.4, stake=2.0, shares=5.0,
            p_consenso=0.6, agreement="strong", recommendation="BUY",
            condition_id=f"0x{i:064d}",
        )

    def fake_get_event(slug):
        if slug == "ev-win":
            return {"closed": True, "markets": [
                {"groupItemTitle": "70-72°F", "outcomePrices": '["1.0", "0.0"]'},
                {"groupItemTitle": "other",    "outcomePrices": '["0.0", "1.0"]'},
            ]}
        if slug == "ev-lose":
            return {"closed": True, "markets": [
                {"groupItemTitle": "different", "outcomePrices": '["1.0", "0.0"]'},
            ]}
        raise RuntimeError("network failure")  # ev-void

    fake_gamma = MagicMock()
    fake_gamma.__enter__ = MagicMock(return_value=fake_gamma)
    fake_gamma.__exit__ = MagicMock(return_value=False)
    fake_gamma.get_event = fake_get_event
    monkeypatch.setattr(lengine, "GammaClient", lambda: fake_gamma)

    resolved = lengine.resolve_live_bets(conn, as_of=date(2026, 5, 26))
    statuses = {r.event_slug: r.status for r in resolved}
    assert statuses == {"ev-win": "won", "ev-lose": "lost", "ev-void": "void"}

    # Bankroll math: start 25; won pays 2 * (1-0.4)/0.4 = 3; lost = -2; void = 0; net = 26.
    assert ldb.get_bankroll(conn) == pytest.approx(26.0, abs=1e-6)


def test_resolve_leaves_open_when_event_not_closed(conn, monkeypatch):
    ldb.insert_bet(
        conn,
        placed_at="t", event_slug="ev", event_title="t",
        city_key="nyc", target_date="2026-05-25",
        bin_label="70-72°F", side="YES",
        price_entry=0.4, stake=2.0, shares=5.0,
        p_consenso=0.6, agreement="strong", recommendation="BUY",
        condition_id="0xCID",
    )
    fake_gamma = MagicMock()
    fake_gamma.__enter__ = MagicMock(return_value=fake_gamma)
    fake_gamma.__exit__ = MagicMock(return_value=False)
    fake_gamma.get_event = MagicMock(return_value={"closed": False, "markets": []})
    monkeypatch.setattr(lengine, "GammaClient", lambda: fake_gamma)

    resolved = lengine.resolve_live_bets(conn, as_of=date(2026, 5, 26))
    assert resolved == []
    open_bets = ldb.list_open_bets(conn)
    assert len(open_bets) == 1


# ---------- redeem_resolved ---------------------------------------------------

def test_redeem_resolved_calls_redeem_per_condition(conn, monkeypatch):
    """Three winning bets across 2 condition_ids should produce 2 redeem txs."""
    cid_a = "0x" + "aa" * 32
    cid_b = "0x" + "bb" * 32
    for i, cid in enumerate([cid_a, cid_a, cid_b]):
        bid = ldb.insert_bet(
            conn,
            placed_at="t", event_slug=f"ev-{i}", event_title="t",
            city_key="nyc", target_date="2026-05-25",
            bin_label=f"bin-{i}", side="YES",
            price_entry=0.4, stake=2.0, shares=5.0,
            p_consenso=0.6, agreement="strong", recommendation="BUY",
            condition_id=cid,
        )
        ldb.update_bet_resolution(conn, bid, "won", f"bin-{i}", 3.0, 28.0 + i)

    seen_conditions = []

    def fake_redeem(client, condition_id, **kw):
        seen_conditions.append(condition_id)
        return SimpleNamespace(
            condition_id=condition_id, tx_hash=f"0x{condition_id[-4:]}",
            status=1, gas_used=120_000,
        )

    monkeypatch.setattr(lengine.ctf_mod, "redeem_position", fake_redeem)

    redeemed = lengine.redeem_resolved(conn, chain=MagicMock())
    assert len(redeemed) == 3
    assert sorted(set(seen_conditions)) == sorted([cid_a, cid_b])
    # Audit row per condition.
    ops = ldb.list_chain_ops(conn, kind="redeem")
    assert len(ops) == 2
    assert all(o.status == "success" for o in ops)


def test_redeem_resolved_skips_when_no_winners(conn):
    redeemed = lengine.redeem_resolved(conn, chain=MagicMock())
    assert redeemed == []


def test_redeem_resolved_records_error(conn, monkeypatch):
    bid = ldb.insert_bet(
        conn,
        placed_at="t", event_slug="ev", event_title="t",
        city_key="nyc", target_date="2026-05-25",
        bin_label="b", side="YES",
        price_entry=0.4, stake=2.0, shares=5.0,
        p_consenso=0.6, agreement="strong", recommendation="BUY",
        condition_id="0x" + "ff" * 32,
    )
    ldb.update_bet_resolution(conn, bid, "won", "b", 3.0, 28.0)

    def boom(client, condition_id, **kw):
        raise RuntimeError("rpc down")

    monkeypatch.setattr(lengine.ctf_mod, "redeem_position", boom)
    redeemed = lengine.redeem_resolved(conn, chain=MagicMock())
    assert redeemed == []
    ops = ldb.list_chain_ops(conn, kind="redeem")
    assert len(ops) == 1 and ops[0].status.startswith("error:")
