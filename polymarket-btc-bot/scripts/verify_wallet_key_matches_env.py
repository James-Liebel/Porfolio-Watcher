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

    derived = Account.from_key(pk).address.lower()
    expected = addr.lower()
    if derived == expected:
        print(f"[OK] Private key matches POLYMARKET_WALLET_ADDRESS ({derived[:6]}…{derived[-4:]}).")
        return 0

    print("[X] MISMATCH: this private key does NOT control POLYMARKET_WALLET_ADDRESS.")
    print(f"    .env address: {expected}")
    print(f"    key controls: {derived}")
    print("    Export the private key for the account that shows this address in MetaMask.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
