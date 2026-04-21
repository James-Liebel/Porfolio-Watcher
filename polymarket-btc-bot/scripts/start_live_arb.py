#!/usr/bin/env python3
"""
Start one LIVE structural-arbitrage bot (complete-set on Polymarket CLOB).

- One process, one dashboard port, one SQLite ledger (default data/live_arb.db).
- Full INITIAL_BANKROLL from .env (or pass --bankroll), not split across agents.
- No Ollama / directional overlay / second agent.

Usage (from polymarket-btc-bot):
  python scripts\\start_live_arb.py [--bankroll USDC] [--port N] [--db PATH] [--yes]

Migrating from the old two-agent setup: copy data/live_agent_a.db -> data/live_arb.db
if you want to keep the prior arb-only ledger; otherwise a fresh DB starts clean.

Safety gates: check_env, verify_wallet_key_matches_env, check_live_connections.
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.arb.host_tuning import cpu_count_safe, structural_bot_env_from_cpu  # noqa: E402

# Overrides .env for this launcher — tuned for one bot using the full nominal bankroll.
# Poll interval, CLOB concurrency, tracked-events cap, and basket slots are set by
# structural_bot_env_from_cpu("live") from src/arb/host_tuning.py (CPU-aware).
_SHARED_LIVE: dict[str, str] = {
    "PAPER_TRADE": "false",
    "ARB_LIVE_EXECUTION": "true",
    "ALLOW_TAKER_EXECUTION": "true",
    "PAPER_SPREAD_PENALTY_BPS": "0",
    "PAPER_TAKER_FEE_BPS": "50",
    "ARB_CYCLE_ERROR_BACKOFF_SECONDS": "8",
    # Single bot: deploy full nominal bankroll (fraction × equity, capped below).
    "MAX_BASKET_NOTIONAL": "200",
    "ARB_BASKET_NOTIONAL_FRACTION_OF_EQUITY": "0.38",
    "ARB_BASKET_NOTIONAL_MIN_USD": "12",
    "ARB_MAX_BASKET_NOTIONAL_QUALIFIED_MULTIPLIER": "1.35",
    "ARB_MAX_BASKET_NOTIONAL_QUALIFIED_ABS_MAX": "0",
    "MAX_EVENT_EXPOSURE_PCT": "0.55",
    # complete_set = CLOB-only YES legs (works with Gnosis Safe). neg_risk needs on-chain convert;
    # disabled for Safe by default — see ARB_ALLOW_NEG_RISK_LIVE_WITH_SAFE in config.
    "ARB_STRATEGY_MODE": "both",
    "ARB_ADAPTIVE_EVENT_BUDGET_ENABLED": "true",
    "ARB_ADAPTIVE_EVENT_BUDGET_MIN": "360",
    "ARB_ADAPTIVE_EVENT_BUDGET_MAX": "1400",
    "ARB_ADAPTIVE_EVENT_TARGET_CYCLE_SECONDS": "85",
    # Keep live bot resilient during thin/fast books: do not hard-halt after a few failed baskets.
    "ARB_CONSECUTIVE_EXECUTION_FAILURES_HALT": "0",
    "ARB_HALT_EXECUTION_IF_SYNTHETIC_BOOKS_GE": "0",
    # Probe-friendly floors: markets often show negative theoretical edge until a brief dislocation appears.
    "MAX_ARB_LEG_SPREAD_BPS": "800",
    "MIN_COMPLETE_SET_EDGE_BPS": "20",
    "MIN_NEG_RISK_EDGE_BPS": "32",
    "ARB_MIN_EXPECTED_PROFIT_USD": "0.12",
    "UNIVERSE_MAX_HOURS_TO_RESOLUTION": "96",
    "UNIVERSE_MIN_HOURS_TO_RESOLUTION": "1",
    "UNIVERSE_PREFER_SHORTER_RESOLUTION": "true",
    "OPPORTUNITY_COOLDOWN_SECONDS": "60",
    "AUTO_SETTLE_RESOLVED_EVENTS": "true",
    "COMPLETE_SET_AUTO_UNWIND": "true",
    "COMPLETE_SET_UNWIND_VS_RESOLUTION": "true",
    "COMPLETE_SET_UNWIND_VS_RESOLUTION_EPSILON_USD": "0.05",
    "ARB_LOG_COMPLETE_SET_HOLD_INTERVAL_SECONDS": "120",
    "ENABLE_DIRECTIONAL_OVERLAY": "false",
    "ENABLE_TRADER_FOLLOW": "false",
    "ARB_SYNC_CLOB_COLLATERAL_EACH_CYCLE": "true",
    "ARB_SUMMARY_CLOB_STALE_SECONDS": "5",
    "LOG_LEVEL": "INFO",
}


def _popen_kwargs() -> dict:
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {}


def _win_kill_port(port: int) -> None:
    ps = (
        f"$c = Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue "
        f"| Select-Object -First 1; if ($c) {{ Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue }}"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )


def kill_existing_bot_processes() -> None:
    """Stop stray `python -m src` runtimes for this repo only.

    Do not match `start_live_arb.py` / `start_paper_arb.py` — those command lines must never
    be killed from inside this launcher (WMI/PowerShell edge cases can still terminate the
    launcher if it appears in the same filter set).
    """
    if sys.platform != "win32":
        return
    my_pid = int(os.getpid())
    ps = (
        "$p = Get-CimInstance Win32_Process | "
        f"Where-Object {{ $_.Name -eq 'python.exe' -and $_.ProcessId -ne {my_pid} -and "
        "$_.CommandLine -like '*polymarket-btc-bot*' -and $_.CommandLine -like '* -m src*' }; "
        "$p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def kill_listen_ports(ports: list[int]) -> None:
    if sys.platform == "win32":
        for p in ports:
            _win_kill_port(p)
    else:
        for p in ports:
            subprocess.run(["sh", "-c", f"fuser -k {p}/tcp 2>/dev/null || true"], cwd=str(ROOT))


def _run_gate(script: list[str], label: str) -> bool:
    result = subprocess.run([sys.executable] + script, cwd=str(ROOT))
    if result.returncode != 0:
        print(f"\n[BLOCKED] {label} failed (exit {result.returncode}). Fix above before running live.")
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Start one live structural-arb bot.")
    parser.add_argument(
        "--bankroll",
        type=float,
        default=0.0,
        help="Nominal USDC for this bot (default: INITIAL_BANKROLL from .env).",
    )
    parser.add_argument("--port", type=int, default=8765, help="Control API / dashboard port.")
    parser.add_argument(
        "--db",
        type=str,
        default="data/live_arb.db",
        help="SQLite path relative to repo (default: data/live_arb.db).",
    )
    parser.add_argument("--yes", action="store_true", help="Skip the CONFIRM prompt.")
    args = parser.parse_args()

    print("=" * 60)
    print("  Polymarket — LIVE structural arbitrage (single bot)")
    print("=" * 60)

    bankroll = float(args.bankroll)
    if bankroll <= 0:
        try:
            from src.config import Settings  # noqa: E402

            cfg = Settings()
            bankroll = float(cfg.initial_bankroll)
            print(f"[*] INITIAL_BANKROLL from .env = ${bankroll:.2f}")
        except Exception as exc:
            print(f"[X] Could not read Settings from .env: {exc}")
            return 1
    else:
        print(f"[*] Using --bankroll = ${bankroll:.2f}")

    print("\n[1/3] Checking environment…")
    if not _run_gate(["scripts/check_env.py"], "check_env"):
        return 1

    print("\n[2/3] Verifying wallet key matches address…")
    if not _run_gate(["scripts/verify_wallet_key_matches_env.py"], "verify_wallet_key_matches_env"):
        return 1

    print("\n[3/3] Checking live API connections…")
    if not _run_gate(["scripts/check_live_connections.py"], "check_live_connections"):
        return 1

    port = int(args.port)
    db_path = (ROOT / args.db).resolve()

    print()
    print("=" * 60)
    print(f"  Starting ONE live bot  bankroll ${bankroll:.2f}  port {port}")
    print(f"  SQLite: {db_path}")
    print(
        "  Caps: _SHARED_LIVE + CPU-aware tuning (basket / exposure / poll / CLOB concurrency)."
    )
    print("  If Safe USDC >> nominal bankroll, raise INITIAL_BANKROLL or pass --bankroll.")
    print("=" * 60)
    if args.yes:
        print("--yes: skipping CONFIRM.")
    else:
        answer = input("\nType CONFIRM to start live, or anything else to cancel: ").strip()
        if answer != "CONFIRM":
            print("Cancelled.")
            return 0

    # Ensure a single runtime: kill old `python -m src`, then listeners (restart script also cleans).
    print("\nStopping any existing `python -m src` for this repo…", flush=True)
    try:
        kill_existing_bot_processes()
    except OSError as exc:
        print(f"[WARN] Process cleanup failed (continuing): {exc}", flush=True)
    time.sleep(2)
    print(f"Clearing ports {port} and 8767 (legacy second agent)…", flush=True)
    try:
        kill_listen_ports([port, 8767])
    except OSError as exc:
        print(f"[WARN] Port cleanup failed (continuing): {exc}", flush=True)
    time.sleep(1.5)

    (ROOT / "data").mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.update(_SHARED_LIVE)
    host_tuning = structural_bot_env_from_cpu("live")
    env.update(host_tuning)
    env.update(
        {
            "CONTROL_API_PORT": str(port),
            "ARB_SQLITE_PATH": str(db_path),
            "AGENT_DISPLAY_NAME": "Live — structural arb",
            "INITIAL_BANKROLL": str(round(bankroll, 2)),
            "CONTROL_API_TOKEN": os.environ.get("CONTROL_API_TOKEN", ""),
        }
    )
    n_cpu = cpu_count_safe()
    print(
        f"  Host tuning: CPUs~{n_cpu}  CLOB_CONC={host_tuning['CLOB_BOOK_FETCH_CONCURRENCY']}  "
        f"MAX_TRACKED_EVENTS={host_tuning['MAX_TRACKED_EVENTS']}  POLL={host_tuning['ARB_POLL_SECONDS']}s"
    )

    proc = subprocess.Popen(
        [sys.executable, "-m", "src"],
        cwd=str(ROOT),
        env=env,
        **_popen_kwargs(),
    )
    print(f"[OK] Started live arb  child PID {proc.pid}", flush=True)
    print()
    print(f"  Dashboard: http://127.0.0.1:{port}/ui/index.html")
    print(f"  Summary:   http://127.0.0.1:{port}/summary")
    print("  Ctrl+C stops the bot.")
    print()

    try:
        while True:
            time.sleep(2)
            code = proc.poll()
            if code is not None:
                print(f"\n[!] Bot exited with code {code}.")
                return code
    except KeyboardInterrupt:
        print("\nStopping…")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
