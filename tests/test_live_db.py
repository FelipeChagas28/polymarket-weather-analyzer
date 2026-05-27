"""Tests for live/db.py schema, CRUD, and chain_ops auditing."""
from __future__ import annotations

import json

import pytest

from pwa.live import db as ldb


@pytest.fixture
def conn(tmp_path):
    with ldb.session(tmp_path / "real.db") as c:
        yield c


def test_schema_creates_all_tables(conn):
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert {"state", "bets", "runs", "chain_ops"} <= tables


def test_bets_table_has_live_columns(conn):
    cols = {row[1] for row in conn.execute("PRAGMA table_info(bets)").fetchall()}
    assert {"token_id", "condition_id", "order_id", "tx_hash",
            "fees_paid", "fill_price", "shares_filled"} <= cols


def test_init_state_persists_wallet(conn):
    ldb.init_state(conn, bankroll=25.0, wallet_address="0xabc", wallet_keystore_path="/tmp/w.json")
    assert ldb.is_initialized(conn)
    assert ldb.get_bankroll(conn) == 25.0
    assert ldb.get_state(conn, "mode") == "real"
    assert ldb.get_state(conn, "wallet_address") == "0xabc"
    assert ldb.get_state(conn, "wallet_keystore_path") == "/tmp/w.json"


def test_insert_bet_and_unique_constraint(conn):
    base = dict(
        placed_at="2026-05-26T10:00:00+00:00",
        event_slug="slug-1", event_title="t", city_key="nyc",
        target_date="2026-05-27", bin_label="77°F or below", side="YES",
        price_entry=0.42, stake=2.0, shares=4.76,
        p_consenso=0.55, p_om_ens=0.55, agreement="strong",
        recommendation="BUY", token_id="tok-yes",
        condition_id="0x" + "ab" * 32, order_id="ord-1",
        fees_paid=0.0, fill_price=0.42, shares_filled=4.76,
    )
    first = ldb.insert_bet(conn, **base)
    assert first is not None
    second = ldb.insert_bet(conn, **base)
    assert second is None, "expected UNIQUE on open dupes to block second insert"


def test_list_won_bets_pending_redeem(conn):
    base = dict(
        placed_at="t", event_slug="s", event_title="t", city_key="nyc",
        target_date="2026-05-27", bin_label="b1", side="YES",
        price_entry=0.5, stake=2.0, shares=4.0,
        p_consenso=0.6, agreement="strong", recommendation="BUY",
        condition_id="0xCID",
    )
    bid = ldb.insert_bet(conn, **base)
    # Mark as won, no tx_hash yet.
    ldb.update_bet_resolution(conn, bid, "won", "b1", 2.0, 27.0)
    pending = ldb.list_won_bets_pending_redeem(conn)
    assert len(pending) == 1 and pending[0].id == bid

    ldb.update_bet_redemption(conn, bid, "0xtxhash")
    assert ldb.list_won_bets_pending_redeem(conn) == []


def test_chain_op_lifecycle(conn):
    op_id = ldb.insert_chain_op(
        conn, kind="redeem", status="pending",
        payload_json=json.dumps({"condition_id": "0xCID"}),
    )
    ldb.update_chain_op_status(
        conn, op_id, status="success", gas_used=42_000, gas_price_gwei=35.0, tx_hash="0xfeed",
    )
    ops = ldb.list_chain_ops(conn, kind="redeem")
    assert len(ops) == 1
    op = ops[0]
    assert op.status == "success"
    assert op.gas_used == 42_000
    assert op.tx_hash == "0xfeed"
    payload = json.loads(op.payload_json)
    assert payload["condition_id"] == "0xCID"


def test_chain_ops_filter_by_kind(conn):
    ldb.insert_chain_op(conn, kind="approve")
    ldb.insert_chain_op(conn, kind="redeem")
    ldb.insert_chain_op(conn, kind="approve")
    assert len(ldb.list_chain_ops(conn, kind="approve")) == 2
    assert len(ldb.list_chain_ops(conn, kind="redeem")) == 1
    assert len(ldb.list_chain_ops(conn)) == 3
