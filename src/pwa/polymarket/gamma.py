from __future__ import annotations

import json
from typing import Any, Iterable

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

GAMMA_BASE = "https://gamma-api.polymarket.com"

CLIMATE_TAGS = ("daily-temperature", "highest-temperature")


class GammaClient:
    def __init__(self, timeout: float = 15.0) -> None:
        self._client = httpx.Client(base_url=GAMMA_BASE, timeout=timeout, headers={"accept": "application/json"})

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GammaClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    @retry(
        retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TransportError)),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = self._client.get(path, params=params)
        if resp.status_code == 429 or resp.status_code >= 500:
            resp.raise_for_status()
        resp.raise_for_status()
        return resp.json()

    def list_events(
        self,
        tag_slug: str = "climate",
        active: bool = True,
        closed: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        params = {
            "tag_slug": tag_slug,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "limit": limit,
            "offset": offset,
        }
        data = self._get("/events", params=params)
        return list(data) if isinstance(data, list) else []

    def list_climate_events(self, limit_per_tag: int = 100) -> list[dict[str, Any]]:
        seen: dict[str, dict[str, Any]] = {}
        for tag in CLIMATE_TAGS:
            try:
                events = self.list_events(tag_slug=tag, limit=limit_per_tag)
            except httpx.HTTPError:
                continue
            for ev in events:
                eid = str(ev.get("id"))
                if eid and eid not in seen:
                    seen[eid] = ev
        return list(seen.values())

    def list_temperature_events(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.list_climate_events(limit_per_tag=limit)

    def get_event(self, event_id_or_slug: str) -> dict[str, Any]:
        if event_id_or_slug.isdigit():
            return self._get(f"/events/{event_id_or_slug}")
        return self._get(f"/events/slug/{event_id_or_slug}")

    def search(self, q: str, limit_per_type: int = 20) -> dict[str, Any]:
        return self._get("/public-search", params={"q": q, "limit_per_type": limit_per_type})


def parse_market_outcomes(market: dict[str, Any]) -> tuple[list[str], list[float]]:
    outcomes = market.get("outcomes", "[]")
    prices = market.get("outcomePrices", "[]")
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    if isinstance(prices, str):
        prices = json.loads(prices)
    return list(outcomes), [float(p) for p in prices]


def parse_clob_token_ids(market: dict[str, Any]) -> list[str]:
    raw = market.get("clobTokenIds", "[]")
    if isinstance(raw, str):
        return list(json.loads(raw))
    return list(raw) if raw else []


def event_markets(event: dict[str, Any]) -> Iterable[dict[str, Any]]:
    return event.get("markets") or []
