"""Unit tests for paper-trading DB layer."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pwa.paper import db as pdb


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "paper.db"


def _common_bet(**overrides):
    fields = dict(
        placed_at=pdb.now_iso(),
        event_slug="highest-temperature-in-nyc-on-may-20-2026",
        event_title="Highest temperature in NYC on May 20?",
        city_key="nyc",
        target_date="2026-05-20",
        bin_label="70-71F",
        side="YES",
        price_entry=0.20,
        stake=2.0,
        shares=10.0,
        p_consenso=0.35,
        p_om_ens=0.30,
        agreement="strong",
        recommendation="BUY",
    )
    fields.update(overrides)
    return fields


def test_init_creates_schema(tmp_db):
    with pdb.session(tmp_db) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    assert {"state", "bets", "runs"}.issubset(tables)


def test_init_state_and_bankroll_roundtrip(tmp_db):
    with pdb.session(tmp_db) as conn:
        assert not pdb.is_initialized(conn)
        pdb.init_state(conn, bankroll=10.0)
        assert pdb.is_initialized(conn)
        assert pdb.get_bankroll(conn) == pytest.approx(10.0)
        pdb.update_bankroll(conn, 12.5)
        assert pdb.get_bankroll(conn) == pytest.approx(12.5)


def test_insert_bet_returns_id(tmp_db):
    with pdb.session(tmp_db) as conn:
        pdb.init_state(conn, bankroll=10.0)
        bet_id = pdb.insert_bet(conn, **_common_bet())
        assert bet_id is not None and bet_id > 0


def test_open_bet_unique_constraint(tmp_db):
    """Two open bets on same (event, bin, side) → second is rejected."""
    with pdb.session(tmp_db) as conn:
        pdb.init_state(conn, bankroll=10.0)
        first = pdb.insert_bet(conn, **_common_bet())
        second = pdb.insert_bet(conn, **_common_bet())
        assert first is not None
        assert second is None  # blocked


def test_resolved_bet_does_not_block_new_open(tmp_db):
    """After a bet on (event, bin, side) is resolved, a fresh one can be placed."""
    with pdb.session(tmp_db) as conn:
        pdb.init_state(conn, bankroll=10.0)
        first = pdb.insert_bet(conn, **_common_bet())
        assert first is not None
        pdb.update_bet_resolution(conn, first, "won", "70-71F", 8.0, 18.0)
        second = pdb.insert_bet(conn, **_common_bet(placed_at=pdb.now_iso()))
        assert second is not None  # not blocked since first is now 'won', not 'open'


def test_list_open_bets(tmp_db):
    with pdb.session(tmp_db) as conn:
        pdb.init_state(conn, bankroll=10.0)
        pdb.insert_bet(conn, **_common_bet(bin_label="70F"))
        pdb.insert_bet(conn, **_common_bet(bin_label="71F"))
        opens = pdb.list_open_bets(conn)
        assert len(opens) == 2
        assert {b.bin_label for b in opens} == {"70F", "71F"}


def test_list_bets_due_filters_by_target_date(tmp_db):
    with pdb.session(tmp_db) as conn:
        pdb.init_state(conn, bankroll=10.0)
        pdb.insert_bet(conn, **_common_bet(target_date="2026-05-18", bin_label="A"))
        pdb.insert_bet(conn, **_common_bet(target_date="2026-05-22", bin_label="B"))
        due = pdb.list_bets_due(conn, as_of="2026-05-20")
        assert len(due) == 1
        assert due[0].bin_label == "A"


def test_runs_table_insert(tmp_db):
    with pdb.session(tmp_db) as conn:
        pdb.init_state(conn, bankroll=10.0)
        run_id = pdb.insert_run(conn, 5, 3, 2, 10.0, 11.5)
        assert run_id is not None
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        assert row["n_events_analyzed"] == 5
        assert row["n_bets_placed"] == 3
        assert row["bankroll_after"] == pytest.approx(11.5)
