"""On-chain helpers for the Gnosis Conditional Tokens Framework (CTF).

Two responsibilities:

  1. **Approvals** — one-time setup. Before the wallet can trade, it must:
       * approve USDC.e and pUSD spending by both CTF Exchange contracts
       * call ``setApprovalForAll`` on ``ConditionalTokens`` for both exchanges
  2. **Redeem** — after a market resolves, the wallet holds losing-side ERC-1155
     tokens (worth 0) and winning-side tokens (each worth 1 USDC). We burn both
     via ``ConditionalTokens.redeemPositions`` to get the USDC back.

The orderbook is off-chain — fills happen via the Polymarket operator with no
gas cost to us. The only txs we send are the approvals (once) and the redeems
(one per resolved condition).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import (
    CONDITIONAL_TOKENS_ADDRESS,
    CTF_EXCHANGE_ADDRESS,
    NEG_RISK_CTF_EXCHANGE_ADDRESS,
    PUSD_ADDRESS,
    USDC_E_ADDRESS,
)
from .chain import ChainError, PolygonClient

MAX_UINT256: int = 2**256 - 1
PARENT_COLLECTION_ID_ZERO: bytes = b"\x00" * 32
BINARY_INDEX_SETS: list[int] = [1, 2]  # YES + NO outcomes; redeem both, only the winner pays.


ERC20_APPROVE_ABI: list[dict[str, Any]] = [
    {
        "constant": False,
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

CTF_ABI: list[dict[str, Any]] = [
    {
        "constant": False,
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved", "type": "bool"},
        ],
        "name": "setApprovalForAll",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "operator", "type": "address"},
        ],
        "name": "isApprovedForAll",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "conditionId", "type": "bytes32"},
            {"name": "index", "type": "uint256"},
        ],
        "name": "payoutNumerators",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [{"name": "conditionId", "type": "bytes32"}],
        "name": "payoutDenominator",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


@dataclass(frozen=True, slots=True)
class ApprovalResult:
    kind: str         # "erc20_approve" | "ctf_set_approval"
    target: str       # token or CTF address that received the call
    spender: str      # exchange address granted permission
    tx_hash: str
    status: int       # 1 = success, 0 = reverted
    gas_used: int


@dataclass(frozen=True, slots=True)
class RedeemResult:
    condition_id: str
    tx_hash: str
    status: int
    gas_used: int


def _hex_to_bytes32(value: str) -> bytes:
    h = value[2:] if value.lower().startswith("0x") else value
    if len(h) != 64:
        raise ChainError(f"Expected 32-byte hex (64 chars), got {len(h)}: {value!r}")
    return bytes.fromhex(h)


def _build_and_send(
    client: PolygonClient,
    contract: Any,
    fn: Any,
    *,
    gas_buffer: float = 1.25,
) -> tuple[str, Any]:
    """Estimate gas, build tx, sign with the wallet key, broadcast.

    Returns ``(tx_hash, receipt)`` once the receipt arrives.
    """
    w3 = client.w3
    sender = client.address
    nonce = w3.eth.get_transaction_count(sender)

    try:
        gas_estimate = fn.estimate_gas({"from": sender})
    except Exception as e:
        raise ChainError(f"Gas estimation failed: {e}") from e

    tx = fn.build_transaction(
        {
            "from": sender,
            "nonce": nonce,
            "chainId": client.chain_id(),
            "gas": int(gas_estimate * gas_buffer),
            "maxFeePerGas": w3.eth.gas_price * 2,
            "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
        }
    )

    # The Wallet stores a hex private key; eth-account accepts that directly.
    from eth_account import Account  # type: ignore

    signed = Account.sign_transaction(tx, client._wallet.private_key)  # noqa: SLF001
    tx_hash = client.send_signed_tx(signed)
    receipt = client.wait_for_receipt(tx_hash)
    return tx_hash, receipt


def _erc20_contract(client: PolygonClient, token_address: str) -> Any:
    w3 = client.w3
    return w3.eth.contract(address=w3.to_checksum_address(token_address), abi=ERC20_APPROVE_ABI)


def _ctf_contract(client: PolygonClient) -> Any:
    w3 = client.w3
    return w3.eth.contract(address=w3.to_checksum_address(CONDITIONAL_TOKENS_ADDRESS), abi=CTF_ABI)


def get_erc20_allowance(client: PolygonClient, token_address: str, spender: str) -> int:
    contract = _erc20_contract(client, token_address)
    return int(contract.functions.allowance(client.address, client.w3.to_checksum_address(spender)).call())


def is_ctf_approved_for(client: PolygonClient, operator: str) -> bool:
    contract = _ctf_contract(client)
    return bool(contract.functions.isApprovedForAll(client.address, client.w3.to_checksum_address(operator)).call())


def approve_erc20(client: PolygonClient, token_address: str, spender: str) -> ApprovalResult:
    contract = _erc20_contract(client, token_address)
    fn = contract.functions.approve(client.w3.to_checksum_address(spender), MAX_UINT256)
    tx_hash, receipt = _build_and_send(client, contract, fn)
    return ApprovalResult(
        kind="erc20_approve",
        target=token_address,
        spender=spender,
        tx_hash=tx_hash,
        status=int(receipt.status),
        gas_used=int(receipt.gas_used),
    )


def set_ctf_approval_for(client: PolygonClient, operator: str) -> ApprovalResult:
    contract = _ctf_contract(client)
    fn = contract.functions.setApprovalForAll(client.w3.to_checksum_address(operator), True)
    tx_hash, receipt = _build_and_send(client, contract, fn)
    return ApprovalResult(
        kind="ctf_set_approval",
        target=CONDITIONAL_TOKENS_ADDRESS,
        spender=operator,
        tx_hash=tx_hash,
        status=int(receipt.status),
        gas_used=int(receipt.gas_used),
    )


def run_approvals(client: PolygonClient, *, skip_if_set: bool = True) -> list[ApprovalResult]:
    """Run the full approvals batch needed before any trading.

    With ``skip_if_set=True`` (default) we read current allowance/approval state
    and only send transactions that are actually needed — useful when
    re-running ``pwa live init`` after a partial failure.
    """
    results: list[ApprovalResult] = []
    exchanges = [CTF_EXCHANGE_ADDRESS, NEG_RISK_CTF_EXCHANGE_ADDRESS]
    erc20_tokens = [USDC_E_ADDRESS, PUSD_ADDRESS]

    for token in erc20_tokens:
        for spender in exchanges:
            if skip_if_set and get_erc20_allowance(client, token, spender) > 0:
                continue
            results.append(approve_erc20(client, token, spender))

    for operator in exchanges:
        if skip_if_set and is_ctf_approved_for(client, operator):
            continue
        results.append(set_ctf_approval_for(client, operator))

    return results


def has_resolved_on_chain(client: PolygonClient, condition_id: str) -> bool:
    """True if the oracle has reported payouts for this condition."""
    contract = _ctf_contract(client)
    cid = _hex_to_bytes32(condition_id)
    denom = int(contract.functions.payoutDenominator(cid).call())
    return denom > 0


def winning_outcome_index(client: PolygonClient, condition_id: str) -> int | None:
    """Return the index of the winning outcome (0 = YES, 1 = NO for binary), or None if not resolved.

    For multi-outcome (negRisk) markets returns the index where ``payoutNumerators`` is non-zero.
    Assumes a single winner.
    """
    contract = _ctf_contract(client)
    cid = _hex_to_bytes32(condition_id)
    if int(contract.functions.payoutDenominator(cid).call()) == 0:
        return None
    # Try outcomes 0 and 1 first (covers the binary case); fall back to wider scan only if needed.
    for i in range(8):
        try:
            payout = int(contract.functions.payoutNumerators(cid, i).call())
        except Exception:
            return None
        if payout > 0:
            return i
    return None


def redeem_position(
    client: PolygonClient,
    condition_id: str,
    *,
    collateral_token: str = PUSD_ADDRESS,
    index_sets: list[int] | None = None,
) -> RedeemResult:
    """Burn YES + NO tokens for ``condition_id`` and pull USD back to the wallet."""
    contract = _ctf_contract(client)
    cid = _hex_to_bytes32(condition_id)
    sets = index_sets if index_sets is not None else BINARY_INDEX_SETS

    fn = contract.functions.redeemPositions(
        client.w3.to_checksum_address(collateral_token),
        PARENT_COLLECTION_ID_ZERO,
        cid,
        sets,
    )
    tx_hash, receipt = _build_and_send(client, contract, fn)
    return RedeemResult(
        condition_id=condition_id,
        tx_hash=tx_hash,
        status=int(receipt.status),
        gas_used=int(receipt.gas_used),
    )
