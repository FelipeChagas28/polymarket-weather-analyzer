"""Authenticated wrapper around ``py-clob-client-v2``.

Polymarket's CLOB has two auth layers:

  * L1 (EIP-712): the raw private key signs each order struct.
  * L2 (HMAC): an api_key/secret/passphrase triplet authenticates session
    requests (list orders, cancel, balances).

``create_or_derive_api_creds`` exchanges L1 for L2 — we run it once, persist
the result, and from then on the bot uses L2.

This wrapper is intentionally thin. It only:

  * constructs the underlying ``ClobClient`` from our ``Wallet``
  * loads/saves the L2 creds JSON
  * forwards order placement, cancellation and balance queries

Order-strategy logic (the GTC→FAK hybrid) lives in ``live/execution.py``.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import CLOB_BASE_URL, DEFAULT_CLOB_CREDS_PATH, POLYGON_CHAIN_ID
from .wallet import Wallet

EOA_SIGNATURE_TYPE: int = 0


class ClobTradeError(RuntimeError):
    """CLOB trading interaction failed."""


@dataclass(frozen=True, slots=True)
class ApiCredsBundle:
    api_key: str
    secret: str
    passphrase: str
    path: Path


def _restrict_perms(path: Path) -> None:
    if os.name == "posix":
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass


def _import_clob() -> Any:
    try:
        import py_clob_client  # type: ignore
    except ImportError as e:
        raise ClobTradeError(
            "py-clob-client-v2 not installed. Run: pip install '.[live]'"
        ) from e
    return py_clob_client


def save_creds(creds: Any, path: Path | str = DEFAULT_CLOB_CREDS_PATH) -> ApiCredsBundle:
    """Persist L2 creds (api_key/secret/passphrase) to ``path``.

    Accepts either an ``ApiCreds`` dataclass from the SDK or a plain dict; we
    only read the three string fields.
    """
    api_key = getattr(creds, "api_key", None) or creds["api_key"]
    secret = getattr(creds, "api_secret", None) or getattr(creds, "secret", None) or creds.get("secret") or creds["api_secret"]
    passphrase = getattr(creds, "api_passphrase", None) or getattr(creds, "passphrase", None) or creds.get("passphrase") or creds["api_passphrase"]

    out = Path(path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"api_key": api_key, "secret": secret, "passphrase": passphrase}))
    _restrict_perms(out)
    return ApiCredsBundle(api_key=api_key, secret=secret, passphrase=passphrase, path=out)


def load_creds(path: Path | str = DEFAULT_CLOB_CREDS_PATH) -> ApiCredsBundle | None:
    p = Path(path).expanduser()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise ClobTradeError(f"CLOB creds at {p} are not valid JSON") from e
    missing = [k for k in ("api_key", "secret", "passphrase") if k not in data]
    if missing:
        raise ClobTradeError(f"CLOB creds at {p} missing keys: {missing}")
    return ApiCredsBundle(
        api_key=data["api_key"],
        secret=data["secret"],
        passphrase=data["passphrase"],
        path=p,
    )


def creds_exist(path: Path | str = DEFAULT_CLOB_CREDS_PATH) -> bool:
    return Path(path).expanduser().exists()


class ClobTradeClient:
    """Authenticated CLOB client for an EOA wallet (signature_type=0)."""

    def __init__(
        self,
        wallet: Wallet,
        creds: ApiCredsBundle | None = None,
        creds_path: Path | str = DEFAULT_CLOB_CREDS_PATH,
        host: str = CLOB_BASE_URL,
        chain_id: int = POLYGON_CHAIN_ID,
    ) -> None:
        self._wallet = wallet
        self._creds = creds
        self._creds_path = Path(creds_path).expanduser()
        self._host = host
        self._chain_id = chain_id
        self._client: Any = None  # built lazily on first call

    def _build_client(self) -> Any:
        pcc = _import_clob()
        ClobClient = pcc.client.ClobClient
        client = ClobClient(
            host=self._host,
            chain_id=self._chain_id,
            key=self._wallet.private_key,
            signature_type=EOA_SIGNATURE_TYPE,
        )
        if self._creds is not None:
            ApiCreds = pcc.clob_types.ApiCreds
            client.set_api_creds(
                ApiCreds(
                    api_key=self._creds.api_key,
                    api_secret=self._creds.secret,
                    api_passphrase=self._creds.passphrase,
                )
            )
        return client

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    def create_or_derive_api_creds(self) -> ApiCredsBundle:
        """Bootstrap L2 creds and persist them to ``self._creds_path``."""
        c = self.client
        creds = c.create_or_derive_api_creds()
        bundle = save_creds(creds, path=self._creds_path)
        self._creds = bundle
        # Rebuild client with creds attached so subsequent calls are authed.
        self._client = None
        return bundle

    def get_collateral_balance(self) -> dict[str, Any]:
        """Read USDC/pUSD balance + allowance from the CLOB-side accounting."""
        pcc = _import_clob()
        params = pcc.clob_types.BalanceAllowanceParams(
            asset_type=pcc.clob_types.AssetType.COLLATERAL,
        )
        return dict(self.client.get_balance_allowance(params))

    def get_conditional_balance(self, token_id: str) -> dict[str, Any]:
        pcc = _import_clob()
        params = pcc.clob_types.BalanceAllowanceParams(
            asset_type=pcc.clob_types.AssetType.CONDITIONAL,
            token_id=token_id,
        )
        return dict(self.client.get_balance_allowance(params))

    def post_limit_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str = "BUY",
        order_type: str = "GTC",
        expiration_unix: int | None = None,
    ) -> dict[str, Any]:
        """Submit a limit order. ``order_type`` ∈ {"GTC", "GTD", "FAK", "FOK"}.

        Returns the raw response dict from ``post_order`` (includes ``orderID``
        on success).
        """
        pcc = _import_clob()
        Side = pcc.order_builder.constants
        OrderArgs = pcc.clob_types.OrderArgs
        OrderType = pcc.clob_types.OrderType

        side_const = getattr(Side, side.upper())
        order_args_kwargs: dict[str, Any] = {
            "token_id": token_id,
            "price": price,
            "size": size,
            "side": side_const,
        }
        if expiration_unix is not None:
            order_args_kwargs["expiration"] = expiration_unix

        order_args = OrderArgs(**order_args_kwargs)
        signed = self.client.create_order(order_args)
        return dict(self.client.post_order(signed, getattr(OrderType, order_type.upper())))

    def post_market_order(
        self,
        token_id: str,
        amount_usd: float,
        side: str = "BUY",
        order_type: str = "FAK",
    ) -> dict[str, Any]:
        """Submit a market order denominated in USD (``amount_usd``)."""
        pcc = _import_clob()
        Side = pcc.order_builder.constants
        MarketOrderArgs = pcc.clob_types.MarketOrderArgs
        OrderType = pcc.clob_types.OrderType

        side_const = getattr(Side, side.upper())
        args = MarketOrderArgs(token_id=token_id, amount=amount_usd, side=side_const)
        signed = self.client.create_market_order(args)
        return dict(self.client.post_order(signed, getattr(OrderType, order_type.upper())))

    def get_orders(self) -> list[dict[str, Any]]:
        return list(self.client.get_orders() or [])

    def cancel(self, order_id: str) -> dict[str, Any]:
        return dict(self.client.cancel(order_id=order_id))

    def cancel_all(self) -> dict[str, Any]:
        return dict(self.client.cancel_all())

    def get_order_book(self, token_id: str) -> dict[str, Any]:
        return dict(self.client.get_order_book(token_id))
