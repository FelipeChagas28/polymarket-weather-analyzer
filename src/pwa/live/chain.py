"""Polygon RPC client for balance reads and transaction sending.

Polymarket itself runs the orderbook off-chain — the only on-chain calls the
bot needs are:

  * read USDC.e / pUSD / MATIC balances (sanity-check banca and gas)
  * one-time ERC-20 ``approve`` + ConditionalTokens ``setApprovalForAll``
  * ``ConditionalTokens.redeemPositions`` after a market resolves

This module owns the ``web3.Web3`` provider and offers small helpers above
it. It does **not** know what Polymarket-specific contracts to call — that's
``live/ctf.py`` and ``live/clob_trade.py``.

RPC URL resolution:
  1. ``rpc_url`` argument
  2. ``PWA_POLYGON_RPC`` env var
  3. ``DEFAULT_POLYGON_RPC`` constant (public endpoint, rate-limited)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from . import (
    DEFAULT_POLYGON_RPC,
    POLYGON_CHAIN_ID,
    PUSD_ADDRESS,
    USDC_E_ADDRESS,
)
from .wallet import Wallet

POLYGON_RPC_ENV: str = "PWA_POLYGON_RPC"

# Minimal ABI fragments — only the calls we actually use. Avoids shipping the
# full ERC-20 / CTF JSON.
ERC20_BALANCE_OF_ABI: list[dict[str, Any]] = [
    {
        "constant": True,
        "inputs": [{"name": "owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]


class ChainError(RuntimeError):
    """Polygon RPC interaction failed."""


@dataclass(frozen=True, slots=True)
class TxReceipt:
    tx_hash: str
    status: int            # 1 = success, 0 = reverted
    gas_used: int
    block_number: int
    effective_gas_price_wei: int


def _import_web3() -> Any:
    try:
        from web3 import Web3  # type: ignore
    except ImportError as e:
        raise ChainError("web3 not installed. Run: pip install '.[live]'") from e
    return Web3


def _resolve_rpc_url(rpc_url: str | None) -> str:
    if rpc_url:
        return rpc_url
    return os.environ.get(POLYGON_RPC_ENV) or DEFAULT_POLYGON_RPC


class PolygonClient:
    """Thin wrapper around ``web3.Web3`` bound to Polygon mainnet."""

    def __init__(
        self,
        wallet: Wallet,
        rpc_url: str | None = None,
        request_timeout: float = 30.0,
    ) -> None:
        Web3 = _import_web3()
        url = _resolve_rpc_url(rpc_url)
        self._w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": request_timeout}))
        self._wallet = wallet
        self._checksum_address = Web3.to_checksum_address(wallet.address)

    @property
    def w3(self) -> Any:
        return self._w3

    @property
    def address(self) -> str:
        return self._checksum_address

    def chain_id(self) -> int:
        return int(self._w3.eth.chain_id)

    def assert_polygon(self) -> None:
        cid = self.chain_id()
        if cid != POLYGON_CHAIN_ID:
            raise ChainError(f"Expected chain id {POLYGON_CHAIN_ID} (Polygon), got {cid}")

    def matic_balance_wei(self) -> int:
        return int(self._w3.eth.get_balance(self._checksum_address))

    def matic_balance(self) -> float:
        return self.matic_balance_wei() / 1e18

    def _erc20_balance(self, token_address: str) -> tuple[int, int]:
        Web3 = _import_web3()
        contract = self._w3.eth.contract(
            address=Web3.to_checksum_address(token_address),
            abi=ERC20_BALANCE_OF_ABI,
        )
        raw = int(contract.functions.balanceOf(self._checksum_address).call())
        decimals = int(contract.functions.decimals().call())
        return raw, decimals

    def usdc_e_balance(self) -> float:
        raw, decimals = self._erc20_balance(USDC_E_ADDRESS)
        return raw / (10 ** decimals)

    def pusd_balance(self) -> float:
        raw, decimals = self._erc20_balance(PUSD_ADDRESS)
        return raw / (10 ** decimals)

    def collateral_balance(self) -> float:
        """Total USD value available as Polymarket collateral.

        After the 2026-04-28 upgrade the exchange settles in pUSD (1:1 wrapper
        over USDC.e). Funds may sit in either token before the first trade, so
        we sum both for a single "spendable" number.
        """
        return self.usdc_e_balance() + self.pusd_balance()

    def send_signed_tx(self, signed_tx: Any) -> str:
        """Broadcast a pre-signed transaction; returns the tx hash hex string."""
        raw = getattr(signed_tx, "rawTransaction", None) or getattr(signed_tx, "raw_transaction", None)
        if raw is None:
            raise ChainError("signed_tx has neither rawTransaction nor raw_transaction attribute")
        tx_hash = self._w3.eth.send_raw_transaction(raw)
        return tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)

    def wait_for_receipt(self, tx_hash: str, timeout: float = 180.0) -> TxReceipt:
        receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
        return TxReceipt(
            tx_hash=tx_hash,
            status=int(receipt.get("status", 0)),
            gas_used=int(receipt.get("gasUsed", 0)),
            block_number=int(receipt.get("blockNumber", 0)),
            effective_gas_price_wei=int(receipt.get("effectiveGasPrice", 0)),
        )
