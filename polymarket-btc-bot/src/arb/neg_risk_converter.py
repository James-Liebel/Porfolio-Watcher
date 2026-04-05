"""
On-chain neg-risk position conversion via Polymarket's NegRiskAdapter.

After buying NO tokens for outcome X via CLOB, call `convert_no_to_yes` to:
  1. Burn the NO tokens
  2. Receive YES tokens on every other outcome in the same neg-risk event

The NegRiskAdapter contract atomically handles this: no collateral is required
beyond the NO tokens already in the wallet.

Reference:
  NegRiskAdapter: 0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296 (Polygon)
  Source: https://github.com/Polymarket/neg-risk-ctf-adapter
"""
from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)

# Polygon mainnet — all immutable contract addresses
_CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
_NR_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

# Polymarket exchange contracts (both need USDC spending approval)
_CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"     # standard markets
_NR_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"   # neg-risk markets

# USDC on Polygon — check both (Polymarket migrated from USDC.e to native USDC)
_USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"            # bridged USDC.e
_USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"       # native USDC

_USDC_DECIMALS = 6
_POLYGON_CHAIN_ID = 137
_MAX_UINT256 = 2**256 - 1

_ERC20_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "name": "allowance",
        "type": "function",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "name": "approve",
        "type": "function",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
    },
]

_CTF_ABI = [
    {
        "name": "setApprovalForAll",
        "type": "function",
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved", "type": "bool"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
    {
        "name": "isApprovedForAll",
        "type": "function",
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "operator", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
    },
]

_NR_ABI = [
    {
        "name": "convertPositions",
        "type": "function",
        "inputs": [
            {"name": "_marketId", "type": "bytes32"},
            {"name": "_indexSet", "type": "uint256"},
            {"name": "_amount", "type": "uint256"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
]


def _build_w3(rpc_url: str):
    from web3 import Web3
    from web3.middleware import ExtraDataToPOAMiddleware

    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 60}))
    # Polygon uses PoA — this strips the extra 97-byte `extraData` field
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3


def ensure_ctf_approved(rpc_url: str, wallet_address: str, private_key: str) -> None:
    """
    One-time per wallet: approve NegRiskAdapter as ERC1155 operator on the CTF.
    No-op if already approved. Safe to call repeatedly.
    """
    from web3 import Web3

    w3 = _build_w3(rpc_url)
    account = Web3.to_checksum_address(wallet_address)
    ctf = w3.eth.contract(address=Web3.to_checksum_address(_CTF_ADDRESS), abi=_CTF_ABI)
    nr_addr = Web3.to_checksum_address(_NR_ADAPTER)

    already = ctf.functions.isApprovedForAll(account, nr_addr).call()
    if already:
        logger.info("neg_risk_converter.ctf_already_approved")
        return

    logger.info("neg_risk_converter.approving_ctf")
    nonce = w3.eth.get_transaction_count(account, "pending")
    tx = ctf.functions.setApprovalForAll(nr_addr, True).build_transaction(
        {
            "from": account,
            "nonce": nonce,
            "chainId": _POLYGON_CHAIN_ID,
            "gas": 80_000,
            "gasPrice": int(w3.eth.gas_price * 1.2),
        }
    )
    signed = w3.eth.account.sign_transaction(tx, private_key=private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt["status"] != 1:
        raise RuntimeError(f"setApprovalForAll reverted (tx={tx_hash.hex()})")
    logger.info("neg_risk_converter.ctf_approved", tx=tx_hash.hex())


def ensure_usdc_approved(rpc_url: str, wallet_address: str, private_key: str) -> None:
    """
    Approve both Polymarket CTF exchange contracts to spend USDC from the wallet.
    Checks both native USDC and bridged USDC.e — approves whichever holds a balance.
    No-op if already fully approved. Safe to call repeatedly.
    """
    from web3 import Web3

    w3 = _build_w3(rpc_url)
    account = Web3.to_checksum_address(wallet_address)
    spenders = [
        Web3.to_checksum_address(_CTF_EXCHANGE),
        Web3.to_checksum_address(_NR_CTF_EXCHANGE),
    ]
    nonce = w3.eth.get_transaction_count(account, "pending")

    for usdc_addr_raw in [_USDC_NATIVE, _USDC_E]:
        usdc_addr = Web3.to_checksum_address(usdc_addr_raw)
        usdc = w3.eth.contract(address=usdc_addr, abi=_ERC20_ABI)
        bal = usdc.functions.balanceOf(account).call()
        if bal == 0:
            continue  # no balance on this USDC variant — skip

        logger.info("neg_risk_converter.usdc_found", address=usdc_addr_raw[:12], balance_units=bal)
        for spender in spenders:
            current = usdc.functions.allowance(account, spender).call()
            # Allow if less than $1000 remaining (max-uint may decrease over time on some tokens)
            if current >= 1_000 * 10**_USDC_DECIMALS:
                logger.info(
                    "neg_risk_converter.usdc_already_approved",
                    usdc=usdc_addr_raw[:12],
                    spender=spender[:12],
                )
                continue

            logger.info(
                "neg_risk_converter.approving_usdc",
                usdc=usdc_addr_raw[:12],
                spender=spender[:12],
            )
            tx = usdc.functions.approve(spender, _MAX_UINT256).build_transaction(
                {
                    "from": account,
                    "nonce": nonce,
                    "chainId": _POLYGON_CHAIN_ID,
                    "gas": 100_000,
                    "gasPrice": int(w3.eth.gas_price * 1.2),
                }
            )
            signed = w3.eth.account.sign_transaction(tx, private_key=private_key)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt["status"] != 1:
                raise RuntimeError(f"USDC approve reverted (tx={tx_hash.hex()})")
            logger.info("neg_risk_converter.usdc_approved", tx=tx_hash.hex())
            nonce += 1  # increment for next tx in same block


def convert_no_to_yes(
    rpc_url: str,
    wallet_address: str,
    private_key: str,
    neg_risk_market_id: str,
    question_index: int,
    amount_shares: float,
) -> str:
    """
    Call NegRiskAdapter.convertPositions() — burn NO tokens on outcome at
    `question_index`, receive YES tokens on every other outcome.

    Args:
        neg_risk_market_id: 0x-prefixed bytes32 from Gamma `negRiskMarketID`.
        question_index:     0-based index of the outcome whose NO position to burn.
        amount_shares:      Float shares (same unit as CLOB fill size).

    Returns:
        Transaction hash hex string.
    """
    from web3 import Web3

    w3 = _build_w3(rpc_url)
    account = Web3.to_checksum_address(wallet_address)
    nr = w3.eth.contract(address=Web3.to_checksum_address(_NR_ADAPTER), abi=_NR_ABI)

    market_id_bytes = bytes.fromhex(neg_risk_market_id.lstrip("0x"))
    if len(market_id_bytes) != 32:
        raise ValueError(f"neg_risk_market_id must be 32 bytes, got {len(market_id_bytes)}")

    # indexSet: bit at position question_index → the NO positions to convert
    index_set = 1 << question_index
    # On-chain amount is in USDC base units (6 decimals)
    amount_units = int(round(amount_shares * (10**_USDC_DECIMALS)))
    if amount_units <= 0:
        raise ValueError(f"amount rounds to 0 (input {amount_shares} shares)")

    logger.info(
        "neg_risk_converter.convert_start",
        market_id=neg_risk_market_id,
        question_index=question_index,
        index_set=hex(index_set),
        amount_shares=round(amount_shares, 6),
        amount_units=amount_units,
    )

    nonce = w3.eth.get_transaction_count(account, "pending")
    tx = nr.functions.convertPositions(market_id_bytes, index_set, amount_units).build_transaction(
        {
            "from": account,
            "nonce": nonce,
            "chainId": _POLYGON_CHAIN_ID,
            "gas": 600_000,
            "gasPrice": int(w3.eth.gas_price * 1.2),
        }
    )
    signed = w3.eth.account.sign_transaction(tx, private_key=private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    if receipt["status"] != 1:
        raise RuntimeError(f"convertPositions reverted (tx={tx_hash.hex()})")

    logger.info(
        "neg_risk_converter.convert_done",
        tx=tx_hash.hex(),
        question_index=question_index,
        amount_shares=round(amount_shares, 6),
    )
    return tx_hash.hex()


def question_index_from_id(question_id: str) -> int:
    """Extract the 0-based question index from a Polymarket questionID hex string.

    questionID layout: bytes32 = (marketId << 8) | questionIndex
    The last byte (2 hex chars) is the question index.
    """
    hex_str = question_id.lstrip("0x")
    if len(hex_str) != 64:
        raise ValueError(f"questionID must be 32 bytes (64 hex chars), got {len(hex_str)}")
    return int(hex_str[-2:], 16)
