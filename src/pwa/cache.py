"""Local SQLite cache for slow-changing artifacts (bias correction).

The bias correction for a (city, model) pair is a property that changes on
the scale of weeks/months — recomputing it on every `pwa paper run` wastes
~40 heavy HTTP calls per run (60d observed + 60d historical forecast per
city). This module caches the BiasReport per (city_key, direction, unit)
with a TTL (default 7 days) in ~/.pwa/cache.db so all entry points
(paper auto, paper strongbuy, analyze, calibrate) share the same cache.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pwa.models.bias import BiasReport

DEFAULT_CACHE_PATH = Path.home() / ".pwa" / "cache.db"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS bias_cache (
  city_key TEXT NOT NULL,
  direction TEXT NOT NULL,
  unit TEXT NOT NULL,
  n_days INTEGER NOT NULL,
  mean_bias REAL NOT NULL,
  std_residual REAL NOT NULL,
  computed_at TEXT NOT NULL,
  PRIMARY KEY (city_key, direction, unit)
);
"""


def _connect(db_path: Path | str = DEFAULT_CACHE_PATH) -> sqlite3.Connection:
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA_SQL)
    return conn


def get_cached_bias(
    city_key: str,
    direction: str,
    unit: str,
    max_age_days: int = 7,
    db_path: Path | str = DEFAULT_CACHE_PATH,
) -> BiasReport | None:
    """Return the cached BiasReport if fresh, else None."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT n_days, mean_bias, std_residual, computed_at "
            "FROM bias_cache WHERE city_key = ? AND direction = ? AND unit = ?",
            (city_key, direction, unit.upper()),
        ).fetchone()
    if row is None:
        return None
    try:
        computed_at = datetime.fromisoformat(row["computed_at"])
    except ValueError:
        return None
    if computed_at.tzinfo is None:
        computed_at = computed_at.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - computed_at
    if age > timedelta(days=max_age_days):
        return None
    return BiasReport(
        n_days=int(row["n_days"]),
        mean_bias=float(row["mean_bias"]),
        std_residual=float(row["std_residual"]),
    )


def put_cached_bias(
    city_key: str,
    direction: str,
    unit: str,
    report: BiasReport,
    db_path: Path | str = DEFAULT_CACHE_PATH,
) -> None:
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO bias_cache(city_key, direction, unit, n_days, mean_bias, std_residual, computed_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(city_key, direction, unit) DO UPDATE SET "
            "n_days = excluded.n_days, mean_bias = excluded.mean_bias, "
            "std_residual = excluded.std_residual, computed_at = excluded.computed_at",
            (
                city_key,
                direction,
                unit.upper(),
                report.n_days,
                report.mean_bias,
                report.std_residual,
                now_iso,
            ),
        )
        conn.commit()
