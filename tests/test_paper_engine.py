"""Unit tests for paper-trading engine: place_bet, summary."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from pwa.analysis.consensus import ConsensusRow
from pwa.analysis.edge import EdgeRow
from pwa.analysis.kelly import KellySize
from pwa.paper import db as pdb
from pwa.paper.engine import compute_summary, place_bets_for_event
from pwa.polymarket.parser import Bin


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "paper.db"


def _bin(label: str = "70-71F") -> Bin:
    return Bin(label=label, lower=69.5, upper=71.5, unit="F")


def _edge_row(
    bin_label: str = "70-71F",
    recommendation: str = "BUY",
    side: str = "YES",
    side_price: float = 0.20,
    kelly_capped: float = 0.02,
) -> EdgeRow:
    return EdgeRow(
        bin=_bin(bin_label),
        p_model=0.35,
        yes_ask=0.20, yes_bid=0.18,
        side=side, side_price=side_price,
        edge=0.15, ev=0.07,
        kelly=KellySize(full_kelly=0.18, fractional_kelly=0.023, capped=kelly_capped),
        recommendation=recommendation,
    )


def _consensus_row(bin_label: str = "70-71F", agreement: str = "strong") -> ConsensusRow:
    return ConsensusRow(
        bin=_bin(bin_label),
        per_source_prob={"open-meteo-ensemble": 0.35, "ecmwf_ifs025": 0.32, "yr-no": 0.30},
        consensus_prob=0.35,
        spread_pp=5.0,
        agreement=agreement,
        side="YES",
        conflicts_with_primary=False,
    )


def test_place_bets_inserts_buy_recommendations(tmp_db):
    with pdb.session(tmp_db) as conn:
        pdb.init_state(conn, bankroll=10.0)
        placed = place_bets_for_event(
            conn,
            event_slug="evt-1", event_title="t", city_key="nyc",
            target_date=date(2026, 5, 20),
            edge_rows=[_edge_row(recommendation="BUY")],
            consensus_rows=[_consensus_row()],
        )
        assert len(placed) == 1
        assert placed[0].stake == pytest.approx(10.0 * 0.02)


def test_place_bets_skips_skip_recommendations(tmp_db):
    with pdb.session(tmp_db) as conn:
        pdb.init_state(conn, bankroll=10.0)
        placed = place_bets_for_event(
            conn,
            event_slug="evt-2", event_title="t", city_key="nyc",
            target_date=date(2026, 5, 20),
            edge_rows=[_edge_row(recommendation="SKIP")],
            consensus_rows=[_consensus_row()],
        )
        assert placed == []


def test_strict_mode_drops_non_strong_agreement(tmp_db):
    with pdb.session(tmp_db) as conn:
        pdb.init_state(conn, bankroll=10.0)
        placed = place_bets_for_event(
            conn,
            event_slug="evt-3", event_title="t", city_key="nyc",
            target_date=date(2026, 5, 20),
            edge_rows=[_edge_row(bin_label="A"), _edge_row(bin_label="B")],
            consensus_rows=[_consensus_row("A", agreement="strong"), _consensus_row("B", agreement="moderate")],
            mode="strict",
        )
        assert len(placed) == 1
        assert placed[0].bin_label == "A"


def test_strongbuy_mode_drops_non_strong_buy(tmp_db):
    with pdb.session(tmp_db) as conn:
        pdb.init_state(conn, bankroll=10.0)
        placed = place_bets_for_event(
            conn,
            event_slug="evt-4", event_title="t", city_key="nyc",
            target_date=date(2026, 5, 20),
            edge_rows=[
                _edge_row(bin_label="A", recommendation="STRONG BUY"),
                _edge_row(bin_label="B", recommendation="BUY"),
            ],
            consensus_rows=[_consensus_row("A"), _consensus_row("B")],
            mode="strongbuy",
        )
        assert len(placed) == 1
        assert placed[0].bin_label == "A"


def test_strongbuy_priceband_accepts_inside_band(tmp_db):
    """STRONG BUY with 0.15 <= side_price <= 0.85 should pass."""
    with pdb.session(tmp_db) as conn:
        pdb.init_state(conn, bankroll=10.0)
        placed = place_bets_for_event(
            conn,
            event_slug="evt-pb-1", event_title="t", city_key="nyc",
            target_date=date(2026, 5, 27),
            edge_rows=[_edge_row(recommendation="STRONG BUY", side_price=0.40)],
            consensus_rows=[_consensus_row()],
            mode="strongbuy_priceband",
        )
        assert len(placed) == 1


def test_strongbuy_priceband_rejects_outside_band(tmp_db):
    """Below 0.15 or above 0.85 → skip. Plain BUY inside the band → skip too."""
    with pdb.session(tmp_db) as conn:
        pdb.init_state(conn, bankroll=10.0)
        # Three rows: STRONG BUY too cheap, STRONG BUY too expensive, BUY at a fine price.
        placed = place_bets_for_event(
            conn,
            event_slug="evt-pb-2", event_title="t", city_key="nyc",
            target_date=date(2026, 5, 27),
            edge_rows=[
                _edge_row(bin_label="LOW",  recommendation="STRONG BUY", side_price=0.10),
                _edge_row(bin_label="HIGH", recommendation="STRONG BUY", side_price=0.90),
                _edge_row(bin_label="BUY",  recommendation="BUY",        side_price=0.40),
            ],
            consensus_rows=[
                _consensus_row("LOW"), _consensus_row("HIGH"), _consensus_row("BUY"),
            ],
            mode="strongbuy_priceband",
        )
        assert placed == []


def test_strongbuy_priceband_includes_band_edges(tmp_db):
    """Inclusive bounds: exactly 0.15 and exactly 0.85 are accepted."""
    with pdb.session(tmp_db) as conn:
        pdb.init_state(conn, bankroll=10.0)
        placed = place_bets_for_event(
            conn,
            event_slug="evt-pb-3", event_title="t", city_key="nyc",
            target_date=date(2026, 5, 27),
            edge_rows=[
                _edge_row(bin_label="LO_EDGE", recommendation="STRONG BUY", side_price=0.15),
                _edge_row(bin_label="HI_EDGE", recommendation="STRONG BUY", side_price=0.85),
            ],
            consensus_rows=[_consensus_row("LO_EDGE"), _consensus_row("HI_EDGE")],
            mode="strongbuy_priceband",
        )
        assert len(placed) == 2


def test_reserved_stake_is_subtracted(tmp_db):
    """Second BUY should be sized off the bankroll minus previously reserved stake."""
    with pdb.session(tmp_db) as conn:
        pdb.init_state(conn, bankroll=10.0)
        place_bets_for_event(
            conn, event_slug="evt-A", event_title="t", city_key="nyc",
            target_date=date(2026, 5, 20),
            edge_rows=[_edge_row(bin_label="A", kelly_capped=0.5)],  # huge stake to eat bankroll
            consensus_rows=[_consensus_row("A")],
        )
        # Now bankroll is unchanged ($10) but $5 is reserved. Next bet sized by available=$5.
        placed = place_bets_for_event(
            conn, event_slug="evt-B", event_title="t", city_key="nyc",
            target_date=date(2026, 5, 20),
            edge_rows=[_edge_row(bin_label="B", kelly_capped=0.6)],
            consensus_rows=[_consensus_row("B")],
        )
        assert placed and placed[0].stake <= 5.0 + 1e-6


def test_compute_summary_empty(tmp_db):
    with pdb.session(tmp_db) as conn:
        pdb.init_state(conn, bankroll=10.0)
        s = compute_summary(conn)
        assert s.bankroll_start == 10.0
        assert s.bankroll_current == 10.0
        assert s.n_open == 0
        assert s.winrate == 0.0


def test_compute_summary_with_resolved(tmp_db):
    with pdb.session(tmp_db) as conn:
        pdb.init_state(conn, bankroll=10.0)
        # Insert won + lost manually
        won_id = pdb.insert_bet(conn, placed_at=pdb.now_iso(),
            event_slug="e1", event_title="t", city_key="nyc",
            target_date="2026-05-20", bin_label="A", side="YES",
            price_entry=0.2, stake=2.0, shares=10.0,
            p_consenso=0.4, p_om_ens=0.4, agreement="strong",
            recommendation="BUY",
        )
        pdb.update_bet_resolution(conn, won_id, "won", "A", 8.0, 18.0)
        lost_id = pdb.insert_bet(conn, placed_at=pdb.now_iso(),
            event_slug="e2", event_title="t", city_key="nyc",
            target_date="2026-05-20", bin_label="B", side="YES",
            price_entry=0.2, stake=2.0, shares=10.0,
            p_consenso=0.4, p_om_ens=0.4, agreement="moderate",
            recommendation="BUY",
        )
        pdb.update_bet_resolution(conn, lost_id, "lost", "A", -2.0, 16.0)
        pdb.update_bankroll(conn, 16.0)

        s = compute_summary(conn)
        assert s.n_won == 1
        assert s.n_lost == 1
        assert s.winrate == 0.5
        assert s.by_agreement["strong"] == (1, 0)
        assert s.by_agreement["moderate"] == (0, 1)
