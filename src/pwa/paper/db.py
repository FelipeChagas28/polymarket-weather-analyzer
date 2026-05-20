"""SQLite persistence for paper-trading bets."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

DEFAULT_DB_PATH = Path.home() / ".pwa" / "paper.db"

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  placed_at TEXT NOT NULL,
  event_slug TEXT NOT NULL,
  event_title TEXT NOT NULL,
  city_key TEXT NOT NULL,
  target_date TEXT NOT NULL,
  bin_label TEXT NOT NULL,
  side TEXT NOT NULL,
  price_entry REAL NOT NULL,
  stake REAL NOT NULL,
  shares REAL NOT NULL,
  p_consenso REAL NOT NULL,
  p_om_ens REAL,
  agreement TEXT NOT NULL,
  recommendation TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  resolved_at TEXT,
  realized_bin TEXT,
  profit_loss REAL,
  bankroll_after REAL
);

CREATE INDEX IF NOT EXISTS idx_bets_status ON bets(status);
CREATE INDEX IF NOT EXISTS idx_bets_target_date ON bets(target_date);

-- Partial-index UNIQUE: at most one open bet per (event, bin, side).
CREATE UNIQUE INDEX IF NOT EXISTS uq_bets_open
  ON bets(event_slug, bin_label, side)
  WHERE status = 'open';

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ran_at TEXT NOT NULL,
  n_events_analyzed INTEGER DEFAULT 0,
  n_bets_placed INTEGER DEFAULT 0,
  n_bets_resolved INTEGER DEFAULT 0,
  bankroll_before REAL,
  bankroll_after REAL
);
"""


@dataclass(frozen=True, slots=True)
class Bet:
    id: int
    placed_at: str
    event_slug: str
    event_title: str
    city_key: str
    target_date: str
    bin_label: str
    side: str
    price_entry: float
    stake: float
    shares: float
    p_consenso: float
    p_om_ens: float | None
    agreement: str
    recommendation: str
    status: str
    resolved_at: str | None
    realized_bin: str | None
    profit_loss: float | None
    bankroll_after: float | None


def _row_to_bet(r: sqlite3.Row) -> Bet:
    return Bet(**{k: r[k] for k in r.keys()})


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_SQL)
    # Track schema version for future migrations.
    cur = conn.execute("SELECT value FROM state WHERE key = 'schema_version'")
    row = cur.fetchone()
    if row is None:
        conn.execute("INSERT INTO state(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        conn.commit()
    return conn


@contextmanager
def session(db_path: Path | str = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO state(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def is_initialized(conn: sqlite3.Connection) -> bool:
    return get_state(conn, "bankroll_start") is not None


def init_state(conn: sqlite3.Connection, bankroll: float, mode: str = "auto") -> None:
    set_state(conn, "bankroll_start", f"{bankroll:.6f}")
    set_state(conn, "bankroll_current", f"{bankroll:.6f}")
    set_state(conn, "started_at", now_iso())
    set_state(conn, "mode", mode)


def get_bankroll(conn: sqlite3.Connection) -> float:
    v = get_state(conn, "bankroll_current")
    return float(v) if v is not None else 0.0


def update_bankroll(conn: sqlite3.Connection, new_value: float) -> None:
    set_state(conn, "bankroll_current", f"{new_value:.6f}")


def insert_bet(conn: sqlite3.Connection, **fields: Any) -> int | None:
    """Returns the new bet id, or None if blocked by UNIQUE constraint."""
    cols = ", ".join(fields.keys())
    placeholders = ", ".join(["?"] * len(fields))
    try:
        cur = conn.execute(
            f"INSERT INTO bets({cols}) VALUES({placeholders})",
            tuple(fields.values()),
        )
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def list_open_bets(conn: sqlite3.Connection) -> list[Bet]:
    rows = conn.execute("SELECT * FROM bets WHERE status = 'open' ORDER BY target_date").fetchall()
    return [_row_to_bet(r) for r in rows]


def list_bets_due(conn: sqlite3.Connection, as_of: str) -> list[Bet]:
    rows = conn.execute(
        "SELECT * FROM bets WHERE status = 'open' AND target_date < ? ORDER BY target_date",
        (as_of,),
    ).fetchall()
    return [_row_to_bet(r) for r in rows]


def update_bet_resolution(
    conn: sqlite3.Connection,
    bet_id: int,
    status: str,
    realized_bin: str | None,
    profit_loss: float,
    bankroll_after: float,
) -> None:
    conn.execute(
        "UPDATE bets SET status = ?, resolved_at = ?, realized_bin = ?, "
        "profit_loss = ?, bankroll_after = ? WHERE id = ?",
        (status, now_iso(), realized_bin, profit_loss, bankroll_after, bet_id),
    )


def all_bets(conn: sqlite3.Connection, limit: int | None = None) -> list[Bet]:
    sql = "SELECT * FROM bets ORDER BY placed_at DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    return [_row_to_bet(r) for r in rows]


def insert_run(
    conn: sqlite3.Connection,
    n_events_analyzed: int,
    n_bets_placed: int,
    n_bets_resolved: int,
    bankroll_before: float,
    bankroll_after: float,
) -> int:
    cur = conn.execute(
        "INSERT INTO runs(ran_at, n_events_analyzed, n_bets_placed, n_bets_resolved, "
        "bankroll_before, bankroll_after) VALUES(?, ?, ?, ?, ?, ?)",
        (now_iso(), n_events_analyzed, n_bets_placed, n_bets_resolved, bankroll_before, bankroll_after),
    )
    return cur.lastrowid
