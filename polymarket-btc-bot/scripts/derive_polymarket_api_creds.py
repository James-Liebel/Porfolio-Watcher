#!/usr/bin/env python3
"""
Derive Polymarket CLOB API credentials (L2) from your wallet private key.

Reads WALLET_PRIVATE_KEY from polymarket-btc-bot/.env (same as the rest of the bot).
Run from repo root:

  python scripts/derive_polymarket_api_creds.py

Copy the printed POLYMARKET_* lines into .env — do not commit .env.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON

from src.config import Settings


def _normalize_private_key(raw: str) -> str:
    """Strip quotes/whitespace; ensure MetaMask-style 0x + 64 hex."""
    s = raw.strip().strip('"').strip("'")
    if not s:
        return ""
    if s.startswith("0x") or s.startswith("0X"):
        hex_part = s[2:]
    else:
        hex_part = s
    # Reject seed phrases / garbage
    if " " in s or len(hex_part) != 64:
        return ""
    try:
        int(hex_part, 16)
    except ValueError:
        return ""
    return "0x" + hex_part.lower()


def main() -> int:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        print(f"[X] Missing {env_path} — copy .env.example to .env and set WALLET_PRIVATE_KEY.")
        return 1

    print(f"[*] Loading: {env_path}")
    settings = Settings(_env_file=str(env_path))

    addr = (settings.polymarket_wallet_address or "").strip()
    if addr and not addr.lower().startswith("0x"):
        print(
            "[!] POLYMARKET_WALLET_ADDRESS should look like 0x + 40 hex chars (public address). "
            "If you pasted 64 hex digits here, that is usually a private key — put it on WALLET_PRIVATE_KEY instead."
        )
    elif addr.startswith("0x") and len(addr) == 42:
        pass  # typical EVM address
    elif addr.startswith("0x") and len(addr) != 42:
        print(f"[!] POLYMARKET_WALLET_ADDRESS length is {len(addr)}; expected 42 (0x + 40 hex).")

    pk_raw = (settings.wallet_private_key or "").strip()
    pk = _normalize_private_key(pk_raw)

    if not pk_raw:
        print("[X] WALLET_PRIVATE_KEY is empty.")
        print("    MetaMask → ⋮ → Account details → Show private key → paste into .env as one line.")
        return 1

    if not pk:
        print("[X] WALLET_PRIVATE_KEY does not look like a hex private key (need 64 hex chars, often with 0x).")
        print(f"    After stripping, length was {len(pk_raw)}. Do not paste your seed phrase — export the key for one account only.")
        if " " in pk_raw:
            print("    Tip: Your value contains spaces — use a single line with no spaces.")
        return 1

    print(f"[*] Private key format OK (length {len(pk)}). Calling Polymarket CLOB…")

    host = (settings.clob_host or "https://clob.polymarket.com").rstrip("/")
    try:
        client = ClobClient(
            host,
            chain_id=POLYGON,
            key=pk,
            signature_type=2,
        )
        creds = client.create_or_derive_api_creds()
    except Exception as exc:
        print(f"[X] create_or_derive_api_creds() failed: {exc!r}")
        print()
        print("Common fixes:")
        print("  • Network: try again (VPN/firewall blocking https://clob.polymarket.com).")
        print("  • Key: must be the EVM private key for the SAME address as POLYMARKET_WALLET_ADDRESS.")
        print("  • If the key has quotes in .env, use: WALLET_PRIVATE_KEY=\"0x....\" or no quotes.")
        return 1

    print()
    print("# Paste these into .env (replace any existing POLYMARKET_API_KEY / SECRET / PASSPHRASE lines)")
    print(f"POLYMARKET_API_KEY={creds.api_key}")
    print(f"POLYMARKET_SECRET={creds.api_secret}")
    print(f"POLYMARKET_PASSPHRASE={creds.api_passphrase}")
    print()
    print("[OK] Done. Keep .env private and out of git.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
