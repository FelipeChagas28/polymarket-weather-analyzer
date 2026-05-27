"""Tests for the CLOB trading wrapper. The py-clob-client SDK is fully mocked
so these tests don't require ``[live]`` extras to be installed."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pwa.live import DEFAULT_CLOB_CREDS_PATH  # noqa: F401  (sanity import)
from pwa.live.clob_trade import (
    ApiCredsBundle,
    ClobTradeClient,
    ClobTradeError,
    creds_exist,
    load_creds,
    save_creds,
)
from pwa.live.wallet import Wallet


def _wallet() -> Wallet:
    return Wallet(
        address="0x" + "ab" * 20,
        private_key="0x" + "11" * 32,
        keystore_path=None,  # type: ignore[arg-type]
    )


def _build_fake_pcc():
    """Build a fake ``py_clob_client`` module hierarchy.

    The wrapper only touches a handful of attributes; we provide just those.
    """
    fake_client = MagicMock(name="ClobClientInstance")
    fake_client.create_or_derive_api_creds = MagicMock(
        return_value=SimpleNamespace(api_key="k", api_secret="s", api_passphrase="p")
    )
    fake_client.get_balance_allowance = MagicMock(return_value={"balance": "100", "allowance": "100"})
    fake_client.create_order = MagicMock(return_value={"signed": True})
    fake_client.create_market_order = MagicMock(return_value={"signed": True, "market": True})
    fake_client.post_order = MagicMock(return_value={"orderID": "abc-123", "status": "matched"})
    fake_client.get_orders = MagicMock(return_value=[{"orderID": "abc"}])
    fake_client.cancel = MagicMock(return_value={"cancelled": ["abc"]})
    fake_client.cancel_all = MagicMock(return_value={"cancelled": ["abc", "def"]})
    fake_client.get_order_book = MagicMock(return_value={"bids": [], "asks": []})
    fake_client.set_api_creds = MagicMock()

    ClobClient = MagicMock(return_value=fake_client)
    ApiCreds = SimpleNamespace
    AssetType = SimpleNamespace(COLLATERAL="COLLATERAL", CONDITIONAL="CONDITIONAL")
    OrderType = SimpleNamespace(GTC="GTC", GTD="GTD", FAK="FAK", FOK="FOK")

    class BalanceParams(SimpleNamespace):
        pass

    class OrderArgs(SimpleNamespace):
        pass

    class MarketOrderArgs(SimpleNamespace):
        pass

    pcc = SimpleNamespace(
        client=SimpleNamespace(ClobClient=ClobClient),
        clob_types=SimpleNamespace(
            ApiCreds=ApiCreds,
            AssetType=AssetType,
            OrderType=OrderType,
            BalanceAllowanceParams=BalanceParams,
            OrderArgs=OrderArgs,
            MarketOrderArgs=MarketOrderArgs,
        ),
        order_builder=SimpleNamespace(constants=SimpleNamespace(BUY="BUY", SELL="SELL")),
    )
    return pcc, ClobClient, fake_client


def _patch_pcc(monkeypatch):
    import pwa.live.clob_trade as ct

    pcc, ClobClient, fake_client = _build_fake_pcc()
    monkeypatch.setattr(ct, "_import_clob", lambda: pcc)
    return pcc, ClobClient, fake_client


# ---------- creds persistence -------------------------------------------------

def test_save_creds_writes_json(tmp_path):
    path = tmp_path / "creds.json"
    bundle = save_creds(
        SimpleNamespace(api_key="K", api_secret="S", api_passphrase="P"),
        path=path,
    )
    assert isinstance(bundle, ApiCredsBundle)
    data = json.loads(path.read_text())
    assert data == {"api_key": "K", "secret": "S", "passphrase": "P"}


def test_load_creds_returns_none_when_missing(tmp_path):
    assert load_creds(tmp_path / "missing.json") is None
    assert creds_exist(tmp_path / "missing.json") is False


def test_load_creds_roundtrip(tmp_path):
    path = tmp_path / "creds.json"
    save_creds(SimpleNamespace(api_key="K", api_secret="S", api_passphrase="P"), path=path)
    bundle = load_creds(path)
    assert bundle is not None
    assert (bundle.api_key, bundle.secret, bundle.passphrase) == ("K", "S", "P")


def test_load_creds_rejects_partial(tmp_path):
    path = tmp_path / "creds.json"
    path.write_text(json.dumps({"api_key": "K"}))
    with pytest.raises(ClobTradeError, match="missing keys"):
        load_creds(path)


# ---------- client construction -----------------------------------------------

def test_create_or_derive_api_creds_persists_and_rebuilds(monkeypatch, tmp_path):
    pcc, ClobClient, fake_client = _patch_pcc(monkeypatch)
    creds_path = tmp_path / "creds.json"

    c = ClobTradeClient(_wallet(), creds_path=creds_path)
    bundle = c.create_or_derive_api_creds()

    assert (bundle.api_key, bundle.secret, bundle.passphrase) == ("k", "s", "p")
    assert creds_path.exists()
    # First build → no creds → client built without set_api_creds.
    # After derive, force a rebuild and confirm set_api_creds is called.
    _ = c.client  # triggers rebuild
    fake_client.set_api_creds.assert_called()


def test_client_with_existing_creds_calls_set_api_creds(monkeypatch):
    pcc, ClobClient, fake_client = _patch_pcc(monkeypatch)
    bundle = ApiCredsBundle(api_key="K", secret="S", passphrase="P", path=None)  # type: ignore[arg-type]
    c = ClobTradeClient(_wallet(), creds=bundle)
    _ = c.client
    fake_client.set_api_creds.assert_called_once()


# ---------- order placement ---------------------------------------------------

def test_post_limit_order_passes_args(monkeypatch):
    pcc, ClobClient, fake_client = _patch_pcc(monkeypatch)
    c = ClobTradeClient(_wallet(), creds=ApiCredsBundle("K", "S", "P", None))  # type: ignore[arg-type]
    resp = c.post_limit_order(token_id="t1", price=0.42, size=10, side="BUY", order_type="GTD", expiration_unix=1234)

    assert resp["orderID"] == "abc-123"
    order_args = fake_client.create_order.call_args.args[0]
    assert order_args.token_id == "t1"
    assert order_args.price == 0.42
    assert order_args.size == 10
    assert order_args.side == "BUY"
    assert order_args.expiration == 1234
    fake_client.post_order.assert_called_once()
    assert fake_client.post_order.call_args.args[1] == "GTD"


def test_post_limit_order_omits_expiration_by_default(monkeypatch):
    pcc, ClobClient, fake_client = _patch_pcc(monkeypatch)
    c = ClobTradeClient(_wallet(), creds=ApiCredsBundle("K", "S", "P", None))  # type: ignore[arg-type]
    c.post_limit_order(token_id="t1", price=0.5, size=5)
    order_args = fake_client.create_order.call_args.args[0]
    assert not hasattr(order_args, "expiration")


def test_post_market_order_uses_market_args(monkeypatch):
    pcc, ClobClient, fake_client = _patch_pcc(monkeypatch)
    c = ClobTradeClient(_wallet(), creds=ApiCredsBundle("K", "S", "P", None))  # type: ignore[arg-type]
    c.post_market_order(token_id="t2", amount_usd=2.5, side="BUY", order_type="FAK")
    args = fake_client.create_market_order.call_args.args[0]
    assert args.token_id == "t2"
    assert args.amount == 2.5
    assert args.side == "BUY"
    assert fake_client.post_order.call_args.args[1] == "FAK"


def test_get_collateral_balance(monkeypatch):
    pcc, ClobClient, fake_client = _patch_pcc(monkeypatch)
    c = ClobTradeClient(_wallet(), creds=ApiCredsBundle("K", "S", "P", None))  # type: ignore[arg-type]
    out = c.get_collateral_balance()
    assert out == {"balance": "100", "allowance": "100"}
    params = fake_client.get_balance_allowance.call_args.args[0]
    assert params.asset_type == "COLLATERAL"


def test_get_conditional_balance_passes_token_id(monkeypatch):
    pcc, ClobClient, fake_client = _patch_pcc(monkeypatch)
    c = ClobTradeClient(_wallet(), creds=ApiCredsBundle("K", "S", "P", None))  # type: ignore[arg-type]
    c.get_conditional_balance("tok-42")
    params = fake_client.get_balance_allowance.call_args.args[0]
    assert params.asset_type == "CONDITIONAL"
    assert params.token_id == "tok-42"


def test_cancel_and_cancel_all(monkeypatch):
    pcc, ClobClient, fake_client = _patch_pcc(monkeypatch)
    c = ClobTradeClient(_wallet(), creds=ApiCredsBundle("K", "S", "P", None))  # type: ignore[arg-type]
    assert c.cancel("abc") == {"cancelled": ["abc"]}
    assert c.cancel_all() == {"cancelled": ["abc", "def"]}
    fake_client.cancel.assert_called_once_with(order_id="abc")
    fake_client.cancel_all.assert_called_once()


def test_get_orders_returns_list(monkeypatch):
    pcc, ClobClient, fake_client = _patch_pcc(monkeypatch)
    c = ClobTradeClient(_wallet(), creds=ApiCredsBundle("K", "S", "P", None))  # type: ignore[arg-type]
    out = c.get_orders()
    assert out == [{"orderID": "abc"}]


def test_missing_sdk_raises_clob_error():
    """If py_clob_client isn't installed, methods that need the SDK must
    raise ``ClobTradeError`` with an install hint — never ``ModuleNotFoundError``."""
    import pwa.live.clob_trade as ct

    def boom():
        raise ImportError("no module")

    original = ct._import_clob

    def faked():
        try:
            __import__("definitely_not_a_real_module_xyz")
        except ImportError as e:
            raise ClobTradeError("py-clob-client-v2 not installed. Run: pip install '.[live]'") from e
        return None

    ct._import_clob = faked
    try:
        c = ClobTradeClient(_wallet())
        with pytest.raises(ClobTradeError, match="not installed"):
            c.create_or_derive_api_creds()
    finally:
        ct._import_clob = original
