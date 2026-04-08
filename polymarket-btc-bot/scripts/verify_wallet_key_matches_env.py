#!/usr/bin/env python3
"""
Verify WALLET_PRIVATE_KEY in .env corresponds to POLYMARKET_WALLET_ADDRESS.

Run from polymarket-btc-bot:
  python scripts/verify_wallet_key_matches_env.py

Does not print the private key.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eth_account import Account

from src.config import Settings


def _normalize_pk(raw: str) -> str:
    s = raw.strip().strip('"').strip("'")
    if not s:
        return ""
    if s.startswith(("0x", "0X")):
        h = s[2:]
    else:
        h = s
    if len(h) != 64:
        return ""
    try:
        int(h, 16)
    except ValueError:
        return ""
    return "0x" + h.lower()


def main() -> int:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        print(f"[X] Missing {env_path}")
        return 1

    settings = Settings(_env_file=str(env_path))
    pk = _normalize_pk(settings.wallet_private_key or "")
    addr = (settings.polymarket_wallet_address or "").strip().lower()

    if not pk:
        print("[X] WALLET_PRIVATE_KEY missing or not 64 hex chars (see derive script help).")
        return 1
    if not addr.startswith("0x"):
        print("[X] POLYMARKET_WALLET_ADDRESS not set.")
        return 1

    derived_eoa = Account.from_key(pk).address.lower()
    expected = addr.lower()

    # Direct match: sig_type=0 where POLYMARKET_WALLET_ADDRESS is the EOA itself.
    if derived_eoa == expected:
        print(f"[OK] Private key matches POLYMARKET_WALLET_ADDRESS directly ({derived_eoa[:6]}…{derived_eoa[-4:]}).")
        return 0

    # sig_type=2 (Gnosis Safe): POLYMARKET_WALLET_ADDRESS is the Safe contract.
    # Verify by asking the CTF exchange what Safe is registered for this EOA.
    sig_type = int(getattr(settings, "clob_signature_type", 0) or 0)
    if sig_type == 2:
        try:
            from web3 import Web3

            rpc = (getattr(settings, "polygon_rpc_url", None) or "https://polygon-bor.publicnode.com").strip()
            w3 = Web3(Web3.HTTPProvider(rpc))
            CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
            abi = [{
                "inputs": [{"name": "_addr", "type": "address"}],
                "name": "getSafeAddress",
                "outputs": [{"name": "", "type": "address"}],
                "stateMutability": "view",
                "type": "function",
            }]
            exchange = w3.eth.contract(address=Web3.to_checksum_address(CTF_EXCHANGE), abi=abi)
            safe = exchange.functions.getSafeAddress(Web3.to_checksum_address(derived_eoa)).call().lower()
            if safe == expected:
                print(
                    f"[OK] sig_type=2: EOA {derived_eoa[:6]}…{derived_eoa[-4:]} owns "
                    f"Safe {expected[:6]}…{expected[-4:]} (verified on-chain)."
                )
                return 0
            print("[X] MISMATCH (sig_type=2): EOA's registered Safe does not match POLYMARKET_WALLET_ADDRESS.")
            print(f"    .env address:    {expected}")
            print(f"    registered Safe: {safe}")
            print(f"    key EOA:         {derived_eoa}")
            return 1
        except Exception as exc:
            # On-chain check failed — warn but do not block (RPC may be unreachable).
            print(f"[!] Could not verify Safe on-chain ({exc}); proceeding with caution.")
            return 0

    print("[X] MISMATCH: this private key does NOT control POLYMARKET_WALLET_ADDRESS.")
    print(f"    .env address: {expected}")
    print(f"    key controls: {derived_eoa}")
    print("    Export the private key for the account that shows this address in MetaMask.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
