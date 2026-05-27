"""Tests for the Polygon RPC client. All RPC calls are mocked."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("web3")
pytest.importorskip("eth_account")

from pwa.live import POLYGON_CHAIN_ID, PUSD_ADDRESS, USDC_E_ADDRESS  # noqa: E402
from pwa.live.chain import ChainError, PolygonClient  # noqa: E402
from pwa.live.wallet import Wallet  # noqa: E402


def _make_wallet() -> Wallet:
    from eth_account import Account

    acct = Account.create()
    return Wallet(address=acct.address, private_key=acct.key.hex(), keystore_path=None)  # type: ignore[arg-type]


def _build_client(monkeypatch, w3_mock) -> PolygonClient:
    """Patch web3.Web3 so PolygonClient construction does no real RPC."""
    import pwa.live.chain as chain_mod

    fake_web3_cls = MagicMock(name="Web3")
    fake_web3_cls.return_value = w3_mock
    fake_web3_cls.HTTPProvider = MagicMock(return_value="provider")
    fake_web3_cls.to_checksum_address = staticmethod(lambda a: a if a.startswith("0x") else "0x" + a)

    monkeypatch.setattr(chain_mod, "_import_web3", lambda: fake_web3_cls)
    return PolygonClient(_make_wallet())


def test_chain_id_returns_polygon(monkeypatch):
    w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=POLYGON_CHAIN_ID))
    client = _build_client(monkeypatch, w3)
    assert client.chain_id() == POLYGON_CHAIN_ID
    client.assert_polygon()  # should not raise


def test_assert_polygon_rejects_wrong_chain(monkeypatch):
    w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=1))
    client = _build_client(monkeypatch, w3)
    with pytest.raises(ChainError, match="Expected chain id 137"):
        client.assert_polygon()


def test_matic_balance_conversion(monkeypatch):
    w3 = SimpleNamespace(eth=SimpleNamespace(get_balance=lambda _addr: 3_500_000_000_000_000_000))
    client = _build_client(monkeypatch, w3)
    assert client.matic_balance_wei() == 3_500_000_000_000_000_000
    assert client.matic_balance() == pytest.approx(3.5)


def test_erc20_balances_use_decimals(monkeypatch):
    captured_addresses = []

    def make_contract(*, address, abi):
        captured_addresses.append(address)
        balance_call = MagicMock(return_value=MagicMock(call=MagicMock(return_value=1_500_000)))
        decimals_call = MagicMock(return_value=MagicMock(call=MagicMock(return_value=6)))
        funcs = SimpleNamespace(balanceOf=lambda _a: balance_call.return_value, decimals=lambda: decimals_call.return_value)
        return SimpleNamespace(functions=funcs)

    w3 = SimpleNamespace(
        eth=SimpleNamespace(
            chain_id=POLYGON_CHAIN_ID,
            contract=make_contract,
        )
    )
    client = _build_client(monkeypatch, w3)

    assert client.usdc_e_balance() == pytest.approx(1.5)
    assert client.pusd_balance() == pytest.approx(1.5)
    assert client.collateral_balance() == pytest.approx(3.0)

    # Both ERC-20 reads must have hit the right contracts.
    assert USDC_E_ADDRESS in captured_addresses
    assert PUSD_ADDRESS in captured_addresses


def test_send_signed_tx_uses_raw_transaction(monkeypatch):
    sent_raw = []
    w3 = SimpleNamespace(
        eth=SimpleNamespace(
            send_raw_transaction=lambda raw: sent_raw.append(raw) or SimpleNamespace(hex=lambda: "0xdeadbeef"),
        )
    )
    client = _build_client(monkeypatch, w3)
    signed = SimpleNamespace(rawTransaction=b"\x01\x02")
    assert client.send_signed_tx(signed) == "0xdeadbeef"
    assert sent_raw == [b"\x01\x02"]


def test_send_signed_tx_falls_back_to_snake_case(monkeypatch):
    """web3.py v7 renamed ``rawTransaction`` to ``raw_transaction``."""
    sent_raw = []
    w3 = SimpleNamespace(
        eth=SimpleNamespace(
            send_raw_transaction=lambda raw: sent_raw.append(raw) or SimpleNamespace(hex=lambda: "0xabc"),
        )
    )
    client = _build_client(monkeypatch, w3)
    signed = SimpleNamespace(raw_transaction=b"\xff")
    assert client.send_signed_tx(signed) == "0xabc"
    assert sent_raw == [b"\xff"]


def test_send_signed_tx_raises_without_raw_attr(monkeypatch):
    w3 = SimpleNamespace(eth=SimpleNamespace(send_raw_transaction=lambda raw: None))
    client = _build_client(monkeypatch, w3)
    with pytest.raises(ChainError, match="neither rawTransaction"):
        client.send_signed_tx(SimpleNamespace())


def test_wait_for_receipt_parses_status(monkeypatch):
    fake_receipt = {
        "status": 1,
        "gasUsed": 21_000,
        "blockNumber": 555,
        "effectiveGasPrice": 30_000_000_000,
    }
    w3 = SimpleNamespace(eth=SimpleNamespace(wait_for_transaction_receipt=lambda h, timeout: fake_receipt))
    client = _build_client(monkeypatch, w3)
    receipt = client.wait_for_receipt("0xfeed")
    assert receipt.tx_hash == "0xfeed"
    assert receipt.status == 1
    assert receipt.gas_used == 21_000
    assert receipt.block_number == 555
    assert receipt.effective_gas_price_wei == 30_000_000_000
