"""Tests for the Gnosis Safe execTransaction relay used to unblock neg-risk conversion on
signature_type=2 wallets. The on-chain send needs a chain, so we unit-test the pure signing
helper (signer recovery) and the calldata encoding — the parts that must be exactly right."""
from __future__ import annotations

from eth_account import Account
from web3 import Web3

from src.arb.neg_risk_converter import (
    _NR_ADAPTER,
    _NR_ABI,
    _encode_call,
    sign_safe_tx_hash,
)


def test_sign_safe_tx_hash_recovers_owner():
    acct = Account.create()
    safe_tx_hash = Web3.keccak(text="safe-tx-hash-fixture")  # any 32-byte hash

    sig = sign_safe_tx_hash(acct.key.hex(), safe_tx_hash)

    # Safe expects a 65-byte r||s||v blob with v in {27, 28} for a direct ECDSA owner signature.
    assert len(sig) == 65
    assert sig[64] in (27, 28)

    # The Safe recovers the signer via ecrecover(safeTxHash, v, r, s); it must be our owner EOA.
    recovered = Account._recover_hash(safe_tx_hash, vrs=(sig[64], int.from_bytes(sig[0:32], "big"), int.from_bytes(sig[32:64], "big")))
    assert Web3.to_checksum_address(recovered) == Web3.to_checksum_address(acct.address)


def test_encode_call_produces_convert_positions_selector():
    w3 = Web3()
    nr = w3.eth.contract(address=Web3.to_checksum_address(_NR_ADAPTER), abi=_NR_ABI)
    market_id = bytes(32)
    data = _encode_call(nr.functions.convertPositions(market_id, 1, 1_000_000))

    assert isinstance(data, bytes)
    # convertPositions(bytes32,uint256,uint256) selector = keccak256(sig)[:4]
    selector = Web3.keccak(text="convertPositions(bytes32,uint256,uint256)")[:4]
    assert data[:4] == selector
