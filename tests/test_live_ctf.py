"""Tests for live/ctf.py (approvals + redeem) with web3 fully mocked."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("eth_account")

from pwa.live import (  # noqa: E402
    CONDITIONAL_TOKENS_ADDRESS,
    CTF_EXCHANGE_ADDRESS,
    NEG_RISK_CTF_EXCHANGE_ADDRESS,
    PUSD_ADDRESS,
    USDC_E_ADDRESS,
)
from pwa.live import ctf as ctf_mod  # noqa: E402
from pwa.live.chain import PolygonClient  # noqa: E402
from pwa.live.wallet import Wallet  # noqa: E402


def _wallet():
    from eth_account import Account

    acct = Account.create()
    return Wallet(address=acct.address, private_key=acct.key.hex(), keystore_path=None)  # type: ignore[arg-type]


class _ContractRegistry:
    """Tracks (address, abi) pairs handed to ``w3.eth.contract`` and dispatches
    to per-address fake contracts the test sets up."""

    def __init__(self):
        self.contracts = {}

    def register(self, address, contract):
        self.contracts[address.lower()] = contract

    def factory(self, *, address, abi):
        return self.contracts[address.lower()]


def _build_client(monkeypatch, registry: _ContractRegistry, *, chain_id: int = 137):
    fake_web3_cls = MagicMock(name="Web3")
    fake_w3 = MagicMock()
    fake_w3.eth.chain_id = chain_id
    fake_w3.eth.contract = registry.factory
    fake_w3.eth.get_transaction_count = MagicMock(return_value=7)
    fake_w3.eth.gas_price = 30_000_000_000
    fake_w3.eth.send_raw_transaction = MagicMock(return_value=SimpleNamespace(hex=lambda: "0xfeed"))
    fake_w3.eth.wait_for_transaction_receipt = MagicMock(return_value={
        "status": 1, "gasUsed": 50_000, "blockNumber": 99, "effectiveGasPrice": 30_000_000_000,
    })
    fake_w3.to_checksum_address = staticmethod(lambda a: a if a.startswith("0x") else "0x" + a)
    fake_w3.to_wei = staticmethod(lambda v, unit: int(v) * 10**9 if unit == "gwei" else int(v))

    fake_web3_cls.return_value = fake_w3
    fake_web3_cls.HTTPProvider = MagicMock(return_value="provider")
    fake_web3_cls.to_checksum_address = staticmethod(lambda a: a if a.startswith("0x") else "0x" + a)

    import pwa.live.chain as chain_mod
    monkeypatch.setattr(chain_mod, "_import_web3", lambda: fake_web3_cls)

    return PolygonClient(_wallet()), fake_w3


def _erc20_contract(allowance: int = 0):
    """Build a fake ERC-20 contract whose ``allowance`` returns the given value."""
    contract = MagicMock()
    contract.functions.allowance = MagicMock(return_value=MagicMock(call=MagicMock(return_value=allowance)))

    sent_args = []

    def approve_fn(spender, amount):
        sent_args.append((spender, amount))
        approve_call = MagicMock()
        approve_call.estimate_gas = MagicMock(return_value=40_000)
        approve_call.build_transaction = MagicMock(return_value={
            "from": "0x", "nonce": 7, "chainId": 137, "gas": 1, "maxFeePerGas": 1, "maxPriorityFeePerGas": 1,
        })
        return approve_call

    contract.functions.approve = approve_fn
    contract._sent = sent_args
    return contract


def _ctf_contract(*, approved_for: set | None = None, payout_denom: int = 0, winning_index: int | None = None):
    approved_for = approved_for or set()
    contract = MagicMock()

    def is_approved_call(owner, operator):
        return MagicMock(call=MagicMock(return_value=operator.lower() in {a.lower() for a in approved_for}))

    contract.functions.isApprovedForAll = is_approved_call

    set_args = []
    def set_approval(operator, approved):
        set_args.append((operator, approved))
        call = MagicMock()
        call.estimate_gas = MagicMock(return_value=60_000)
        call.build_transaction = MagicMock(return_value={
            "from": "0x", "nonce": 7, "chainId": 137, "gas": 1, "maxFeePerGas": 1, "maxPriorityFeePerGas": 1,
        })
        return call
    contract.functions.setApprovalForAll = set_approval
    contract._set_approval_args = set_args

    contract.functions.payoutDenominator = MagicMock(return_value=MagicMock(call=MagicMock(return_value=payout_denom)))

    def payout_num(cid, idx):
        return MagicMock(call=MagicMock(return_value=1 if idx == winning_index else 0))
    contract.functions.payoutNumerators = payout_num

    redeem_args = []
    def redeem(collateral, parent, cid, sets):
        redeem_args.append((collateral, parent, cid, sets))
        call = MagicMock()
        call.estimate_gas = MagicMock(return_value=120_000)
        call.build_transaction = MagicMock(return_value={
            "from": "0x", "nonce": 7, "chainId": 137, "gas": 1, "maxFeePerGas": 1, "maxPriorityFeePerGas": 1,
        })
        return call
    contract.functions.redeemPositions = redeem
    contract._redeem_args = redeem_args

    return contract


def _patch_signer(monkeypatch):
    """Stub eth_account.Account.sign_transaction so tx building doesn't need a real key."""
    import eth_account
    fake_signed = SimpleNamespace(rawTransaction=b"\x00")
    monkeypatch.setattr(eth_account.Account, "sign_transaction", staticmethod(lambda tx, key: fake_signed))


# ---------------------------------------------------------------------------

def test_run_approvals_skips_when_all_set(monkeypatch):
    """If every allowance is non-zero and CTF is approved, no tx is sent."""
    registry = _ContractRegistry()
    usdc = _erc20_contract(allowance=10**30)
    pusd = _erc20_contract(allowance=10**30)
    registry.register(USDC_E_ADDRESS, usdc)
    registry.register(PUSD_ADDRESS, pusd)
    ctf = _ctf_contract(approved_for={CTF_EXCHANGE_ADDRESS, NEG_RISK_CTF_EXCHANGE_ADDRESS})
    registry.register(CONDITIONAL_TOKENS_ADDRESS, ctf)

    client, fake_w3 = _build_client(monkeypatch, registry)
    _patch_signer(monkeypatch)

    results = ctf_mod.run_approvals(client, skip_if_set=True)
    assert results == []
    assert not usdc._sent
    assert not pusd._sent
    assert not ctf._set_approval_args
    fake_w3.eth.send_raw_transaction.assert_not_called()


def test_run_approvals_sends_all_six_when_nothing_set(monkeypatch):
    """Cold wallet: 2 tokens × 2 exchanges + 2 CTF approvals = 6 txs."""
    registry = _ContractRegistry()
    usdc = _erc20_contract(allowance=0)
    pusd = _erc20_contract(allowance=0)
    registry.register(USDC_E_ADDRESS, usdc)
    registry.register(PUSD_ADDRESS, pusd)
    ctf = _ctf_contract(approved_for=set())
    registry.register(CONDITIONAL_TOKENS_ADDRESS, ctf)

    client, fake_w3 = _build_client(monkeypatch, registry)
    _patch_signer(monkeypatch)

    results = ctf_mod.run_approvals(client, skip_if_set=True)
    assert len(results) == 6
    assert sum(1 for r in results if r.kind == "erc20_approve") == 4
    assert sum(1 for r in results if r.kind == "ctf_set_approval") == 2
    assert all(r.status == 1 for r in results)
    assert fake_w3.eth.send_raw_transaction.call_count == 6


def test_has_resolved_on_chain_false_when_denom_zero(monkeypatch):
    registry = _ContractRegistry()
    registry.register(CONDITIONAL_TOKENS_ADDRESS, _ctf_contract(payout_denom=0))
    client, _ = _build_client(monkeypatch, registry)
    assert ctf_mod.has_resolved_on_chain(client, "0x" + "ab" * 32) is False


def test_has_resolved_on_chain_true_when_denom_positive(monkeypatch):
    registry = _ContractRegistry()
    registry.register(CONDITIONAL_TOKENS_ADDRESS, _ctf_contract(payout_denom=1))
    client, _ = _build_client(monkeypatch, registry)
    assert ctf_mod.has_resolved_on_chain(client, "0x" + "ab" * 32) is True


def test_winning_outcome_index(monkeypatch):
    registry = _ContractRegistry()
    registry.register(CONDITIONAL_TOKENS_ADDRESS, _ctf_contract(payout_denom=1, winning_index=1))
    client, _ = _build_client(monkeypatch, registry)
    assert ctf_mod.winning_outcome_index(client, "0x" + "cd" * 32) == 1


def test_redeem_position_calls_contract(monkeypatch):
    registry = _ContractRegistry()
    ctf = _ctf_contract(payout_denom=1, winning_index=0)
    registry.register(CONDITIONAL_TOKENS_ADDRESS, ctf)
    client, _ = _build_client(monkeypatch, registry)
    _patch_signer(monkeypatch)

    cid = "0x" + "ab" * 32
    result = ctf_mod.redeem_position(client, cid)
    assert result.status == 1
    assert result.tx_hash == "0xfeed"
    assert result.condition_id == cid

    # We pass pUSD as collateral, parent collection zero, both index sets [1, 2].
    assert len(ctf._redeem_args) == 1
    _collateral, parent, _bytes_cid, sets = ctf._redeem_args[0]
    assert parent == b"\x00" * 32
    assert sets == [1, 2]
