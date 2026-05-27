"""Live (real-money) trading on Polymarket via Polygon mainnet.

Constants in this module are the on-chain addresses and chain ids that the
rest of the package binds to. They are intentionally hard-coded — the
Polymarket exchange is not deployed anywhere else.

References:
  - https://docs.polymarket.com/developers/CLOB
  - https://help.polymarket.com/en/articles/14762452 (pUSD upgrade, 2026-04-28)
"""
from __future__ import annotations

from pathlib import Path

POLYGON_CHAIN_ID: int = 137

CLOB_BASE_URL: str = "https://clob.polymarket.com"

USDC_E_ADDRESS: str = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
PUSD_ADDRESS: str = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
CONDITIONAL_TOKENS_ADDRESS: str = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
CTF_EXCHANGE_ADDRESS: str = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_CTF_EXCHANGE_ADDRESS: str = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

DEFAULT_POLYGON_RPC: str = "https://polygon-rpc.com"

PWA_HOME: Path = Path.home() / ".pwa"
DEFAULT_WALLET_PATH: Path = PWA_HOME / "wallet.json"
DEFAULT_CLOB_CREDS_PATH: Path = PWA_HOME / "clob_creds.json"
