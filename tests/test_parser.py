from __future__ import annotations

import math
from datetime import date

import pytest

from pwa.polymarket.parser import parse_bin_label, parse_event_bins, parse_event_title


def test_parse_title_highest_nyc():
    info = parse_event_title("Highest temperature in NYC on May 17?", end_date_iso="2026-05-17T12:00:00Z")
    assert info is not None
    assert info.direction == "highest"
    assert info.city_key == "nyc"
    assert info.target_date == date(2026, 5, 17)


def test_parse_title_lowest_london():
    info = parse_event_title("Lowest temperature in London on May 18?", end_date_iso="2026-05-18T12:00:00Z")
    assert info is not None
    assert info.direction == "lowest"
    assert info.city_key == "london"
    assert info.target_date == date(2026, 5, 18)


def test_parse_title_multiword_city():
    info = parse_event_title("Highest temperature in San Francisco on May 19?", end_date_iso="2026-05-19T12:00:00Z")
    assert info is not None
    assert info.city_key == "san-francisco"


def test_parse_title_unrecognized():
    assert parse_event_title("Will it rain in Paris?") is None


def test_bin_range():
    b = parse_bin_label("78-79°F")
    assert b is not None
    assert b.lower == 77.5
    assert b.upper == 79.5
    assert b.midpoint == pytest.approx(78.5)


def test_bin_or_below():
    b = parse_bin_label("77°F or below")
    assert b is not None
    assert math.isinf(b.lower) and b.lower < 0
    assert b.upper == 77.5
    assert b.is_left_open


def test_bin_or_above():
    b = parse_bin_label("96°F or above")
    assert b is not None
    assert b.lower == 95.5
    assert math.isinf(b.upper) and b.upper > 0
    assert b.is_right_open


def test_bin_or_higher_synonym():
    b = parse_bin_label("96°F or higher")
    assert b is not None
    assert b.is_right_open


def test_bin_unknown_label():
    assert parse_bin_label("between fifty and sixty degrees") is None


def test_parse_event_bins_filters_markets_without_label():
    markets = [
        {"groupItemTitle": "77°F or below"},
        {"groupItemTitle": "78-79°F"},
        {"groupItemTitle": "garbage"},
        {},
    ]
    bins = parse_event_bins(markets)
    assert len(bins) == 2
    assert bins[0][1].label == "77°F or below"
    assert bins[1][1].label == "78-79°F"
