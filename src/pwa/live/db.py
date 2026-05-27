"""SQLite persistence for real-money trading.

The schema reuses the paper-trading model (``state``, ``bets``, ``runs``) and
adds a few columns specific to on-chain execution:

  * ``token_id`` — ERC-1155 id of the outcome we bought (needed for redeem)
  * ``condition_id`` — Polymarket condition id (groups outcomes per market)
  * ``order_id`` — id returned by CLOB ``post_order``
  * ``tx_hash`` — hash of the on-chain redeem transaction (set after winning)
  * ``fees_paid`` — taker fee in USD (zero for maker fills)
  * ``fill_price`` — actual average price paid (may differ from ``price_entry``)
  * ``shares_filled`` — may be lower than ``shares`` on partial fills

Plus a new ``chain_ops`` audit table for every on-chain transaction the bot
sends (approvals, redeems, balance checks).
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

DEFAULT_DB_PATH = Path.home() / ".pwa" / "real.db"

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
  bankroll_after REAL,
  token_id TEXT,
  condition_id TEXT,
  order_id TEXT,
  tx_hash TEXT,
  fees_paid REAL,
  fill_price REAL,
  shares_filled REAL
);

CREATE INDEX IF NOT EXISTS idx_bets_status ON bets(status);
CREATE INDEX IF NOT EXISTS idx_bets_target_date ON bets(target_date);
CREATE INDEX IF NOT EXISTS idx_bets_condition_id ON bets(condition_id);

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

CREATE TABLE IF NOT EXISTS chain_ops (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  tx_hash TEXT,
  gas_used INTEGER,
  gas_price_gwei REAL,
  status TEXT,
  payload_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_chain_ops_kind ON chain_ops(kind);
"""


@dataclass(frozen=True, slots=True)
class LiveBet:
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
    token_id: str | None
    condition_id: str | None
    order_id: str | None
    tx_hash: str | None
    fees_paid: float | None
    fill_price: float | None
    shares_filled: float | None


@dataclass(frozen=True, slots=True)
class ChainOp:
    id: int
    ts: str
    kind: str
    tx_hash: str | None
    gas_used: int | None
    gas_price_gwei: float | None
    status: str | None
    payload_json: str | None


def _row_to_bet(r: sqlite3.Row) -> LiveBet:
    return LiveBet(**{k: r[k] for k in r.keys()})


def _row_to_chain_op(r: sqlite3.Row) -> ChainOp:
    return ChainOp(**{k: r[k] for k in r.keys()})


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_SQL)
    cur = conn.execute("SELECT value FROM state WHERE key = 'schema_version'")
    row = cur.fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO state(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
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


def init_state(
    conn: sqlite3.Connection,
    bankroll: float,
    wallet_address: str,
    wallet_keystore_path: str,
    mode: str = "real",
) -> None:
    set_state(conn, "bankroll_start", f"{bankroll:.6f}")
    set_state(conn, "bankroll_current", f"{bankroll:.6f}")
    set_state(conn, "started_at", now_iso())
    set_state(conn, "mode", mode)
    set_state(conn, "wallet_address", wallet_address)
    set_state(conn, "wallet_keystore_path", wallet_keystore_path)


def get_bankroll(conn: sqlite3.Connection) -> float:
    v = get_state(conn, "bankroll_current")
    return float(v) if v is not None else 0.0


def update_bankroll(conn: sqlite3.Connection, new_value: float) -> None:
    set_state(conn, "bankroll_current", f"{new_value:.6f}")


def insert_bet(conn: sqlite3.Connection, **fields: Any) -> int | None:
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


def list_open_bets(conn: sqlite3.Connection) -> list[LiveBet]:
    rows = conn.execute("SELECT * FROM bets WHERE status = 'open' ORDER BY target_date").fetchall()
    return [_row_to_bet(r) for r in rows]


def list_bets_due(conn: sqlite3.Connection, as_of: str) -> list[LiveBet]:
    rows = conn.execute(
        "SELECT * FROM bets WHERE status = 'open' AND target_date < ? ORDER BY target_date",
        (as_of,),
    ).fetchall()
    return [_row_to_bet(r) for r in rows]


def list_won_bets_pending_redeem(conn: sqlite3.Connection) -> list[LiveBet]:
    """Bets that resolved as winners but have no tx_hash recorded yet."""
    rows = conn.execute(
        "SELECT * FROM bets WHERE status = 'won' AND tx_hash IS NULL ORDER BY resolved_at"
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


def update_bet_redemption(conn: sqlite3.Connection, bet_id: int, tx_hash: str) -> None:
    conn.execute("UPDATE bets SET tx_hash = ? WHERE id = ?", (tx_hash, bet_id))


def all_bets(conn: sqlite3.Connection, limit: int | None = None) -> list[LiveBet]:
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


def insert_chain_op(
    conn: sqlite3.Connection,
    kind: str,
    *,
    tx_hash: str | None = None,
    gas_used: int | None = None,
    gas_price_gwei: float | None = None,
    status: str | None = None,
    payload_json: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO chain_ops(ts, kind, tx_hash, gas_used, gas_price_gwei, status, payload_json) "
        "VALUES(?, ?, ?, ?, ?, ?, ?)",
        (now_iso(), kind, tx_hash, gas_used, gas_price_gwei, status, payload_json),
    )
    return cur.lastrowid


def update_chain_op_status(
    conn: sqlite3.Connection,
    op_id: int,
    status: str,
    *,
    gas_used: int | None = None,
    gas_price_gwei: float | None = None,
    tx_hash: str | None = None,
) -> None:
    sets = ["status = ?"]
    args: list[Any] = [status]
    if gas_used is not None:
        sets.append("gas_used = ?")
        args.append(gas_used)
    if gas_price_gwei is not None:
        sets.append("gas_price_gwei = ?")
        args.append(gas_price_gwei)
    if tx_hash is not None:
        sets.append("tx_hash = ?")
        args.append(tx_hash)
    args.append(op_id)
    conn.execute(f"UPDATE chain_ops SET {', '.join(sets)} WHERE id = ?", tuple(args))


def list_chain_ops(conn: sqlite3.Connection, kind: str | None = None) -> list[ChainOp]:
    if kind:
        rows = conn.execute("SELECT * FROM chain_ops WHERE kind = ? ORDER BY ts DESC", (kind,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM chain_ops ORDER BY ts DESC").fetchall()
    return [_row_to_chain_op(r) for r in rows]
