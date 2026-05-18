"""Parse Polymarket daily-temperature event titles and bin labels.

Title shapes seen in the wild:
  - "Highest temperature in NYC on May 17?"
  - "Lowest temperature in London on May 18?"
  - "Highest temperature in San Francisco on April 16?"

Bin shapes (from market.groupItemTitle):
  - "77°F or below"
  - "78-79°F"
  - "86°F or above"
  - "X°F or higher" (synonym for "or above")

All temperatures in the Polymarket weather markets are in Fahrenheit.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from dateutil import parser as date_parser

DIRECTION = Literal["highest", "lowest"]

TITLE_RE = re.compile(
    r"^\s*(?P<direction>Highest|Lowest)\s+temperature\s+in\s+(?P<city>.+?)\s+on\s+(?P<date>.+?)\??\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class EventInfo:
    direction: DIRECTION
    city_raw: str
    city_key: str
    target_date: date


@dataclass(frozen=True, slots=True)
class Bin:
    label: str
    lower: float
    upper: float

    @property
    def is_left_open(self) -> bool:
        return math.isinf(self.lower) and self.lower < 0

    @property
    def is_right_open(self) -> bool:
        return math.isinf(self.upper) and self.upper > 0

    @property
    def midpoint(self) -> float:
        if self.is_left_open:
            return self.upper - 0.5
        if self.is_right_open:
            return self.lower + 0.5
        return (self.lower + self.upper) / 2


CITY_ALIASES: dict[str, str] = {
    "nyc": "nyc",
    "new york": "nyc",
    "new york city": "nyc",
    "ny": "nyc",
    "la": "los-angeles",
    "los angeles": "los-angeles",
    "sf": "san-francisco",
    "san francisco": "san-francisco",
    "hong kong": "hong-kong",
    "tel aviv": "tel-aviv",
    "mexico city": "mexico-city",
    "sao paulo": "sao-paulo",
    "são paulo": "sao-paulo",
    "rio": "rio-de-janeiro",
    "rio de janeiro": "rio-de-janeiro",
}


def _city_to_key(raw: str) -> str:
    norm = raw.strip().lower()
    if norm in CITY_ALIASES:
        return CITY_ALIASES[norm]
    return norm.replace(" ", "-")


def parse_event_title(title: str, end_date_iso: str | None = None) -> EventInfo | None:
    m = TITLE_RE.match(title)
    if not m:
        return None
    direction = m.group("direction").lower()
    city_raw = m.group("city").strip()
    date_str = m.group("date").strip()

    target_year = None
    if end_date_iso:
        try:
            target_year = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00")).year
        except ValueError:
            pass

    try:
        target = date_parser.parse(date_str, default=datetime(target_year or datetime.utcnow().year, 1, 1)).date()
    except (ValueError, OverflowError):
        return None

    return EventInfo(
        direction=direction,  # type: ignore[arg-type]
        city_raw=city_raw,
        city_key=_city_to_key(city_raw),
        target_date=target,
    )


BIN_RANGE_RE = re.compile(r"^\s*(?P<lo>-?\d+)\s*-\s*(?P<hi>-?\d+)\s*°?\s*F\s*$", re.IGNORECASE)
BIN_OR_BELOW_RE = re.compile(r"^\s*(?P<x>-?\d+)\s*°?\s*F\s*or\s*(below|lower)\s*$", re.IGNORECASE)
BIN_OR_ABOVE_RE = re.compile(r"^\s*(?P<x>-?\d+)\s*°?\s*F\s*or\s*(above|higher)\s*$", re.IGNORECASE)


def parse_bin_label(label: str) -> Bin | None:
    if (m := BIN_RANGE_RE.match(label)) is not None:
        lo = float(m.group("lo"))
        hi = float(m.group("hi"))
        # Closed integer interval [lo, hi] in Polymarket means: lo <= T <= hi.
        # For integration we extend to [lo - 0.5, hi + 0.5] to capture the
        # measurement-resolution width (Polymarket resolves to nearest integer °F).
        return Bin(label, lo - 0.5, hi + 0.5)
    if (m := BIN_OR_BELOW_RE.match(label)) is not None:
        x = float(m.group("x"))
        return Bin(label, float("-inf"), x + 0.5)
    if (m := BIN_OR_ABOVE_RE.match(label)) is not None:
        x = float(m.group("x"))
        return Bin(label, x - 0.5, float("inf"))
    return None


def parse_event_bins(markets: list[dict]) -> list[tuple[dict, Bin]]:
    out: list[tuple[dict, Bin]] = []
    for m in markets:
        label = m.get("groupItemTitle") or ""
        b = parse_bin_label(label)
        if b is not None:
            out.append((m, b))
    return out
