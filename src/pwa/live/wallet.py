"""Encrypted local keystore for the trading EOA.

The bot uses a dedicated externally-owned account (EOA, ``signature_type=0``)
to sign Polymarket orders and on-chain redeem calls. The private key never
touches disk in plaintext: we use ``eth_account``'s scrypt-encrypted JSON
keystore (the same format MetaMask uses).

Password resolution order:
  1. explicit ``password`` argument
  2. ``PWA_WALLET_PASSWORD`` env var
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import DEFAULT_WALLET_PATH

WALLET_PASSWORD_ENV: str = "PWA_WALLET_PASSWORD"


class WalletError(RuntimeError):
    """Wallet load/create failed."""


@dataclass(frozen=True, slots=True)
class Wallet:
    address: str
    private_key: str
    keystore_path: Path


def _resolve_password(password: str | None) -> str:
    if password is not None:
        return password
    env_pw = os.environ.get(WALLET_PASSWORD_ENV)
    if not env_pw:
        raise WalletError(
            f"No wallet password supplied (set ${WALLET_PASSWORD_ENV} or pass password=)"
        )
    return env_pw


def _restrict_perms(path: Path) -> None:
    if os.name == "posix":
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass


def _import_account() -> Any:
    try:
        from eth_account import Account  # type: ignore
    except ImportError as e:
        raise WalletError(
            "eth-account not installed. Run: pip install '.[live]'"
        ) from e
    return Account


def create_wallet(
    password: str | None = None,
    keystore_path: Path | str = DEFAULT_WALLET_PATH,
    overwrite: bool = False,
) -> Wallet:
    """Generate a fresh EOA, encrypt the key with ``password`` and persist.

    Raises ``WalletError`` if the keystore already exists and ``overwrite`` is
    False — we never silently clobber an existing wallet.
    """
    path = Path(keystore_path).expanduser()
    if path.exists() and not overwrite:
        raise WalletError(f"Keystore already exists at {path}; pass overwrite=True to replace")

    pw = _resolve_password(password)
    Account = _import_account()
    acct = Account.create()
    encrypted = Account.encrypt(acct.key, pw)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(encrypted))
    _restrict_perms(path)

    return Wallet(address=acct.address, private_key=acct.key.hex(), keystore_path=path)


def load_wallet(
    password: str | None = None,
    keystore_path: Path | str = DEFAULT_WALLET_PATH,
) -> Wallet:
    """Decrypt the keystore at ``keystore_path`` and return the unlocked wallet."""
    path = Path(keystore_path).expanduser()
    if not path.exists():
        raise WalletError(f"Keystore not found at {path}; run create_wallet() first")

    pw = _resolve_password(password)
    Account = _import_account()
    try:
        encrypted = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise WalletError(f"Keystore at {path} is not valid JSON") from e

    try:
        key_bytes = Account.decrypt(encrypted, pw)
    except ValueError as e:
        raise WalletError("Wrong password or corrupted keystore") from e

    acct = Account.from_key(key_bytes)
    return Wallet(address=acct.address, private_key=acct.key.hex(), keystore_path=path)


def wallet_exists(keystore_path: Path | str = DEFAULT_WALLET_PATH) -> bool:
    return Path(keystore_path).expanduser().exists()


def wallet_address(keystore_path: Path | str = DEFAULT_WALLET_PATH) -> str | None:
    """Return the address stored in a keystore without decrypting it.

    eth-account keystores expose the address as a top-level ``"address"`` key
    (lowercased, no 0x). Returns None if the file is missing or malformed.
    """
    path = Path(keystore_path).expanduser()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    raw = data.get("address")
    if not isinstance(raw, str):
        return None
    return "0x" + raw if not raw.startswith("0x") else raw
