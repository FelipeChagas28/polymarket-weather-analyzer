"""CLOB read-only client for price/orderbook data.

Endpoint shapes (no auth required for reads):
  - POST /prices       with body [{"token_id": "...", "side": "BUY"|"SELL"}, ...]
  - GET  /book?token_id=...
  - GET  /midpoint?token_id=...

We typically already have bestBid/bestAsk on the market object from Gamma
(`bestAsk`, `bestBid`), so fetching CLOB is only needed when we want fresher
prices or want the full orderbook for slippage estimation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

CLOB_BASE = "https://clob.polymarket.com"


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    token_id: str
    best_bid: float | None
    best_ask: float | None
    spread: float | None


class ClobClient:
    def __init__(self, timeout: float = 15.0) -> None:
        self._client = httpx.Client(base_url=CLOB_BASE, timeout=timeout, headers={"accept": "application/json"})

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ClobClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    @retry(
        retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TransportError)),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    def book(self, token_id: str) -> BookSnapshot:
        r = self._client.get("/book", params={"token_id": token_id})
        r.raise_for_status()
        data = r.json()
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        # Polymarket sorts: bids ascending in price (best = last), asks descending (best = last).
        best_bid = float(bids[-1]["price"]) if bids else None
        best_ask = float(asks[-1]["price"]) if asks else None
        spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else None
        return BookSnapshot(token_id=token_id, best_bid=best_bid, best_ask=best_ask, spread=spread)


def best_yes_ask_from_market(market: dict[str, Any]) -> float | None:
    """Extract best ask for the YES outcome from a Gamma market payload.

    Polymarket's Gamma exposes top-of-book on `bestAsk`/`bestBid` for the YES
    token. When the market has no live order on one side, the field is None.
    """
    raw = market.get("bestAsk")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def best_yes_bid_from_market(market: dict[str, Any]) -> float | None:
    raw = market.get("bestBid")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
