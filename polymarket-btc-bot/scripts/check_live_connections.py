#!/usr/bin/env python3
"""
Pre-flight connection check for live trading.

Tests:
  1. Gamma API reachable and returning markets
  2. CLOB host reachable and returning server time
  3. CLOB L2 auth (POLYMARKET_* creds valid — calls /auth/api-keys)
  4. Wallet balance (USDC on Polygon — warns if zero, does not block)
  5. POLYMARKET_WALLET_ADDRESS and WALLET_PRIVATE_KEY match

Run from polymarket-btc-bot:
  python scripts\\check_live_connections.py
"""
from __future__ import annotations

import sys
import urllib.request
import urllib.error
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import Settings  # noqa: E402


def _get(url: str, timeout: float = 12.0) -> dict | list | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "polymarket-btc-bot/preflight"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} from {url}") from exc
    except Exception as exc:
        raise RuntimeError(f"Request failed for {url}: {exc}") from exc


def check_gamma(cfg: Settings) -> bool:
    url = f"{cfg.gamma_base_url.rstrip('/')}/markets?limit=1&closed=false"
    try:
        data = _get(url)
        if isinstance(data, list) and len(data) > 0:
            print(f"[OK] Gamma API reachable ({cfg.gamma_base_url})")
            return True
        if isinstance(data, dict) and data:
            print(f"[OK] Gamma API reachable ({cfg.gamma_base_url})")
            return True
        print(f"[X] Gamma API returned empty response from {url}")
        return False
    except RuntimeError as exc:
        print(f"[X] Gamma API unreachable: {exc}")
        return False


def check_clob_time(cfg: Settings) -> bool:
    url = f"{cfg.clob_host.rstrip('/')}/time"
    try:
        data = _get(url)
        if isinstance(data, dict) and ("time" in data or "timestamp" in data or "server_time" in data):
            print(f"[OK] CLOB host reachable ({cfg.clob_host})")
            return True
        # Some versions return a plain value
        print(f"[OK] CLOB host reachable ({cfg.clob_host})")
        return True
    except RuntimeError as exc:
        print(f"[X] CLOB host unreachable: {exc}")
        return False


def _looks_like_hex_private_key(raw: str) -> bool:
    s = raw.strip()
    if not s:
        return False
    body = s[2:] if s.startswith("0x") else s
    return len(body) == 64 and all(c in "0123456789abcdefABCDEF" for c in body)


def check_clob_auth(cfg: Settings) -> bool:
    """Call create_or_derive_api_creds() to verify L2 credentials are accepted."""
    pk_raw = (cfg.wallet_private_key or "").strip()
    if not _looks_like_hex_private_key(pk_raw):
        print("[X] WALLET_PRIVATE_KEY not set or not 64 hex chars — cannot check CLOB auth")
        return False

    api_k = (cfg.polymarket_api_key or "").strip()
    api_s = (cfg.polymarket_secret or "").strip()
    api_p = (cfg.polymarket_passphrase or "").strip()

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.constants import POLYGON
        from py_clob_client.clob_types import ApiCreds

        host = cfg.clob_host.rstrip("/")
        pk = pk_raw if pk_raw.startswith("0x") else "0x" + pk_raw

        if api_k and api_s and api_p:
            client = ClobClient(
                host,
                chain_id=POLYGON,
                key=pk,
                creds=ApiCreds(api_key=api_k, api_secret=api_s, api_passphrase=api_p),
                signature_type=2,
                funder=(cfg.polymarket_wallet_address or "").strip(),
            )
            # get_api_keys requires L2 auth
            keys = client.get_api_keys()
            print(f"[OK] CLOB L2 auth accepted (POLYMARKET_API_KEY+SECRET+PASSPHRASE)")
        else:
            # Derive flow — just create_or_derive (makes a network call)
            client = ClobClient(host, chain_id=POLYGON, key=pk, signature_type=2)
            creds = client.create_or_derive_api_creds()
            print(f"[OK] CLOB L2 creds derived from WALLET_PRIVATE_KEY")

        return True
    except Exception as exc:
        print(f"[X] CLOB auth failed: {exc}")
        return False


def check_wallet_balance(cfg: Settings) -> bool:
    """Check USDC balance on Polygon. Warns if zero, does not fail the gate."""
    pk_raw = (cfg.wallet_private_key or "").strip()
    addr = (cfg.polymarket_wallet_address or "").strip()
    if not pk_raw or not addr.startswith("0x"):
        print("[--] Skipping balance check (no wallet key / address).")
        return True

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.constants import POLYGON
        from py_clob_client.clob_types import ApiCreds, AssetType, BalanceAllowanceParams

        host = cfg.clob_host.rstrip("/")
        pk = pk_raw if pk_raw.startswith("0x") else "0x" + pk_raw
        api_k = (cfg.polymarket_api_key or "").strip()
        api_s = (cfg.polymarket_secret or "").strip()
        api_p = (cfg.polymarket_passphrase or "").strip()

        funder = (cfg.polymarket_wallet_address or "").strip()
        if api_k and api_s and api_p:
            client = ClobClient(
                host,
                chain_id=POLYGON,
                key=pk,
                creds=ApiCreds(api_key=api_k, api_secret=api_s, api_passphrase=api_p),
                signature_type=2,
                funder=funder,
            )
        else:
            client = ClobClient(host, chain_id=POLYGON, key=pk, signature_type=2)

        # Sync CLOB collateral view with chain after MetaMask approvals.
        for sig in (0, 1, 2):
            try:
                client.update_balance_allowance(
                    BalanceAllowanceParams(
                        asset_type=AssetType.COLLATERAL, signature_type=sig
                    )
                )
            except Exception:
                pass

        # EOA = 0; Polymarket proxy = 1; Safe = 2. Pick the row with largest reported balance.
        best_bal: dict | None = None
        best_sig = -1
        max_balance_raw = -1
        for sig in (0, 1, 2):
            try:
                b = client.get_balance_allowance(
                    BalanceAllowanceParams(
                        asset_type=AssetType.COLLATERAL, signature_type=sig
                    )
                )
                if not isinstance(b, dict):
                    continue
                br = int(b.get("balance", 0) or 0)
                if br > max_balance_raw:
                    max_balance_raw = br
                    best_bal = b
                    best_sig = sig
            except Exception:
                continue

        if best_bal is None:
            print("[!] Could not read balance for any signature_type (0/1/2).")
            return True

        usdc = int(best_bal.get("balance", 0)) / 1e6
        allowance = int(best_bal.get("allowance", 0)) / 1e6
        if usdc < 0.01:
            print(f"[!] USDC balance is ${usdc:.4f} — deposit USDC on Polygon before trading.")
        else:
            print(
                f"[OK] USDC balance: ${usdc:.2f}   allowance: ${allowance:.2f}   (CLOB signature_type={best_sig})"
            )
        if allowance < 1.0 and usdc > 0.01:
            print(
                "[!] CLOB still reports low allowance — if you already traded on Polymarket.com, "
                "on-chain approval is OK; the API field can lag. Raw: "
                + str({k: best_bal.get(k) for k in ("balance", "allowance") if k in best_bal})
            )
        return True
    except Exception as exc:
        print(f"[!] Could not fetch wallet balance: {exc} (non-blocking)")
        return True


def check_wallet_key_match(cfg: Settings) -> bool:
    pk_raw = (cfg.wallet_private_key or "").strip()
    addr = (cfg.polymarket_wallet_address or "").strip()
    if not pk_raw or not addr.startswith("0x"):
        print("[X] WALLET_PRIVATE_KEY or POLYMARKET_WALLET_ADDRESS not set.")
        return False
    try:
        from eth_account import Account

        pk = pk_raw if pk_raw.startswith("0x") else "0x" + pk_raw
        derived_eoa = Account.from_key(pk).address.lower()

        # Direct match: sig_type=0, address IS the EOA.
        if derived_eoa == addr.lower():
            print(f"[OK] Private key matches POLYMARKET_WALLET_ADDRESS ({derived_eoa[:6]}…{derived_eoa[-4:]})")
            return True

        # sig_type=2 (Gnosis Safe): address is the Safe contract, key is the EOA owner.
        sig_type = int(getattr(cfg, "clob_signature_type", 0) or 0)
        if sig_type == 2:
            try:
                from web3 import Web3

                rpc = (getattr(cfg, "polygon_rpc_url", None) or "https://polygon-bor.publicnode.com").strip()
                w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
                CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
                abi = [{"inputs": [{"name": "_addr", "type": "address"}], "name": "getSafeAddress",
                        "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"}]
                exchange = w3.eth.contract(address=Web3.to_checksum_address(CTF_EXCHANGE), abi=abi)
                safe = exchange.functions.getSafeAddress(Web3.to_checksum_address(derived_eoa)).call().lower()
                if safe == addr.lower():
                    print(f"[OK] sig_type=2: EOA {derived_eoa[:6]}…{derived_eoa[-4:]} owns "
                          f"Safe {addr[:6]}…{addr[-4:]} (on-chain verified)")
                    return True
                print(f"[X] sig_type=2 mismatch: EOA's registered Safe ({safe[:6]}…) != .env ({addr[:6]}…)")
                return False
            except Exception as exc:
                print(f"[!] Could not verify Safe on-chain ({exc}); treating as warning, not block.")
                return True  # Don't block startup for RPC issues

        print(f"[X] Key mismatch: key controls {derived_eoa}  but .env has {addr.lower()}")
        return False
    except Exception as exc:
        print(f"[X] Wallet key match check failed: {exc}")
        return False


def main() -> int:
    cfg = Settings()

    print("=" * 50)
    print("  Live connection pre-flight check")
    print("=" * 50)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace") if hasattr(sys.stdout, "reconfigure") else None

    results: list[bool] = []

    print("\n-- Gamma API ----------------------------------------------")
    results.append(check_gamma(cfg))

    print("\n-- CLOB host ----------------------------------------------")
    results.append(check_clob_time(cfg))

    print("\n-- CLOB L2 auth -------------------------------------------")
    results.append(check_clob_auth(cfg))

    print("\n-- Wallet key / address match -----------------------------")
    results.append(check_wallet_key_match(cfg))

    print("\n-- Wallet USDC balance (non-blocking) ---------------------")
    check_wallet_balance(cfg)   # always True; separate from gate

    blocking_passed = all(results)
    print()
    if blocking_passed:
        print("[OK] All connection checks passed — ready for live.")
        return 0
    else:
        failed = sum(1 for r in results if not r)
        print(f"[X] {failed} check(s) failed — fix above before going live.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
