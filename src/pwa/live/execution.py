"""Hybrid order-placement strategy: GTD limit → FAK market fallback.

Decision tree per opportunity:

    1. Post a GTD limit at ``best_ask`` valid for ``hold_seconds`` (default 30s).
       Maker fee = 0% if it doesn't cross immediately.
    2. Poll order status while it's resting.
    3. If filled completely → done, ``status = "filled"``.
    4. If filled partially → cancel the remainder, accept the partial fill,
       ``status = "partial"``. We do **not** chase the rest with another order
       because the price already moved against us once.
    5. If zero fill after expiration → cancel, submit a FAK market order with
       USD amount = ``size * price`` (taker fee applies), ``status = "filled"``
       (or ``"failed"`` if even the FAK gets no fill).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .clob_trade import ClobTradeClient


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    status: str          # "filled" | "partial" | "failed"
    order_id: str | None
    shares_filled: float
    shares_requested: float
    fill_price: float    # average price actually paid; equal to requested on full GTD fills
    fees_paid: float
    used_fallback: bool  # True if the FAK fallback was invoked
    detail: str = ""


def _shares_filled(order_status: dict[str, Any]) -> float:
    """Extract filled-size from a CLOB order-status payload.

    Different SDK versions use slightly different field names; we cover the
    common ones.
    """
    for key in ("size_matched", "sizeMatched", "filled_size", "filledSize", "matched_amount"):
        v = order_status.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


def _is_terminal(order_status: dict[str, Any]) -> bool:
    s = str(order_status.get("status", "")).lower()
    return s in {"matched", "filled", "cancelled", "canceled", "expired", "failed"}


def submit_hybrid(
    clob: ClobTradeClient,
    token_id: str,
    price: float,
    size: float,
    *,
    hold_seconds: int = 30,
    poll_interval: float = 2.0,
    now_fn: Any = time.time,
    sleep_fn: Any = time.sleep,
) -> ExecutionResult:
    """Place ``size`` shares for ``token_id`` at ``price``, GTD with FAK fallback.

    ``now_fn`` and ``sleep_fn`` are injected so tests can run instantly without
    monkey-patching the global ``time`` module.
    """
    expiration = int(now_fn()) + hold_seconds
    initial = clob.post_limit_order(
        token_id=token_id,
        price=price,
        size=size,
        side="BUY",
        order_type="GTD",
        expiration_unix=expiration,
    )
    order_id = initial.get("orderID") or initial.get("order_id")
    if not order_id:
        return ExecutionResult(
            status="failed", order_id=None, shares_filled=0.0, shares_requested=size,
            fill_price=price, fees_paid=0.0, used_fallback=False,
            detail=f"GTD post returned no order id: {initial!r}",
        )

    deadline = now_fn() + hold_seconds + 5  # small grace beyond expiration
    last_status: dict[str, Any] = initial
    while now_fn() < deadline:
        orders = clob.get_orders()
        match = next((o for o in orders if (o.get("orderID") or o.get("order_id")) == order_id), None)
        if match is not None:
            last_status = match
            if _is_terminal(match):
                break
        else:
            # Some SDKs only return *open* orders; vanishing == terminal.
            break
        sleep_fn(poll_interval)

    filled = _shares_filled(last_status)
    if filled >= size - 1e-9:
        avg_price = float(last_status.get("price", price))
        return ExecutionResult(
            status="filled", order_id=order_id, shares_filled=filled,
            shares_requested=size, fill_price=avg_price, fees_paid=0.0,
            used_fallback=False, detail="gtd_full",
        )

    if filled > 0:
        try:
            clob.cancel(order_id)
        except Exception:
            pass
        return ExecutionResult(
            status="partial", order_id=order_id, shares_filled=filled,
            shares_requested=size, fill_price=float(last_status.get("price", price)),
            fees_paid=0.0, used_fallback=False, detail="gtd_partial",
        )

    # Zero fill → cancel and try a FAK market order.
    try:
        clob.cancel(order_id)
    except Exception:
        pass

    amount_usd = round(size * price, 4)
    fak_resp = clob.post_market_order(
        token_id=token_id, amount_usd=amount_usd, side="BUY", order_type="FAK",
    )
    fak_id = fak_resp.get("orderID") or fak_resp.get("order_id")
    fak_filled = _shares_filled(fak_resp)
    fak_price = float(fak_resp.get("price", price))
    if fak_filled <= 0:
        return ExecutionResult(
            status="failed", order_id=fak_id, shares_filled=0.0,
            shares_requested=size, fill_price=fak_price, fees_paid=0.0,
            used_fallback=True, detail=f"fak_zero: {fak_resp!r}",
        )

    fee_rate = 0.0125  # Weather category taker fee, March 2026.
    fees = round(fak_filled * fak_price * fee_rate, 6)
    final_status = "filled" if fak_filled >= size - 1e-9 else "partial"
    return ExecutionResult(
        status=final_status, order_id=fak_id, shares_filled=fak_filled,
        shares_requested=size, fill_price=fak_price, fees_paid=fees,
        used_fallback=True, detail="fak",
    )
