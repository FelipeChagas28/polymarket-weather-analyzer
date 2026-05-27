"""Tests for the local EOA keystore."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("eth_account")

from pwa.live.wallet import (  # noqa: E402
    WALLET_PASSWORD_ENV,
    Wallet,
    WalletError,
    create_wallet,
    load_wallet,
    wallet_address,
    wallet_exists,
)


def test_create_wallet_writes_encrypted_file(tmp_path, monkeypatch):
    monkeypatch.delenv(WALLET_PASSWORD_ENV, raising=False)
    keystore = tmp_path / "wallet.json"
    w = create_wallet(password="hunter2", keystore_path=keystore)

    assert isinstance(w, Wallet)
    assert w.address.startswith("0x") and len(w.address) == 42
    assert keystore.exists()

    raw = json.loads(keystore.read_text())
    assert "crypto" in raw or "Crypto" in raw, "expected scrypt keystore structure"
    # Plain private key MUST NOT appear in the encrypted file.
    assert w.private_key[2:].lower() not in keystore.read_text().lower()


def test_load_wallet_roundtrip(tmp_path):
    keystore = tmp_path / "wallet.json"
    created = create_wallet(password="s3cret", keystore_path=keystore)
    loaded = load_wallet(password="s3cret", keystore_path=keystore)
    assert loaded.address == created.address
    assert loaded.private_key == created.private_key


def test_load_wallet_wrong_password(tmp_path):
    keystore = tmp_path / "wallet.json"
    create_wallet(password="right", keystore_path=keystore)
    with pytest.raises(WalletError, match="Wrong password"):
        load_wallet(password="wrong", keystore_path=keystore)


def test_create_wallet_refuses_overwrite(tmp_path):
    keystore = tmp_path / "wallet.json"
    create_wallet(password="x", keystore_path=keystore)
    with pytest.raises(WalletError, match="already exists"):
        create_wallet(password="x", keystore_path=keystore)


def test_create_wallet_overwrite_true(tmp_path):
    keystore = tmp_path / "wallet.json"
    first = create_wallet(password="x", keystore_path=keystore)
    second = create_wallet(password="x", keystore_path=keystore, overwrite=True)
    assert first.address != second.address


def test_password_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv(WALLET_PASSWORD_ENV, "env-pw")
    keystore = tmp_path / "wallet.json"
    w = create_wallet(keystore_path=keystore)
    loaded = load_wallet(keystore_path=keystore)
    assert loaded.address == w.address


def test_missing_password_raises(tmp_path, monkeypatch):
    monkeypatch.delenv(WALLET_PASSWORD_ENV, raising=False)
    with pytest.raises(WalletError, match="No wallet password"):
        create_wallet(keystore_path=tmp_path / "wallet.json")


def test_wallet_address_without_decrypt(tmp_path):
    keystore = tmp_path / "wallet.json"
    w = create_wallet(password="x", keystore_path=keystore)
    seen = wallet_address(keystore)
    assert seen is not None
    assert seen.lower() == w.address.lower()


def test_wallet_address_returns_none_when_missing(tmp_path):
    assert wallet_address(tmp_path / "nope.json") is None
    assert wallet_exists(tmp_path / "nope.json") is False


def test_load_missing_keystore(tmp_path):
    with pytest.raises(WalletError, match="not found"):
        load_wallet(password="x", keystore_path=tmp_path / "missing.json")
