#!/usr/bin/env python3
"""
Start two LIVE structural-arb agents, each with half the INITIAL_BANKROLL.

  Agent A (port 8765) — structural arb only, no overlay
  Agent B (port 8767) — structural arb + directional overlay

Both agents share the same wallet/API keys from .env and post real FOK orders
to Polymarket CLOB (ARB_LIVE_EXECUTION=true, PAPER_TRADE=false).

Usage (from polymarket-btc-bot):
  python scripts\\start_live_split.py [--bankroll TOTAL]

--bankroll defaults to 2 × INITIAL_BANKROLL in .env.
Each bot receives half (--bankroll / 2).

Safety gates enforced before launch:
  - check_env.py must exit 0
  - verify_wallet_key_matches_env.py must exit 0
  - check_live_connections.py must exit 0 (CLOB + Gamma reachable)
  - User must type CONFIRM to proceed
"""
from __future__ import annotations

import argparse
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── Shared live settings ─────────────────────────────────────────────────────
# These override anything in .env for the live multi-agent run.
# Sizes are deliberately conservative for a first live session.
_SHARED_LIVE: dict[str, str] = {
    "PAPER_TRADE": "false",
    "ARB_LIVE_EXECUTION": "true",
    "ALLOW_TAKER_EXECUTION": "true",
    # No paper spread penalty on live fills (real prices come from CLOB response).
    "PAPER_SPREAD_PENALTY_BPS": "0",
    # Use real taker fee rate (Polymarket charges 0.5 % on fee-enabled markets).
    "PAPER_TAKER_FEE_BPS": "50",
    "ARB_POLL_SECONDS": "20",
    "ARB_CYCLE_ERROR_BACKOFF_SECONDS": "10",
    # Per-basket size cap — conservative to limit max loss on a single arb failure.
    "MAX_BASKET_NOTIONAL": "15",
    # Allow up to 35% of bankroll per event so a $15 basket fits on a $48 agent.
    # MAX_BASKET_NOTIONAL is the true position size cap; event cap is just a second floor.
    "MAX_EVENT_EXPOSURE_PCT": "0.35",
    "MAX_TOTAL_OPEN_BASKETS": "2",
    "MAX_BASKETS_PER_STRATEGY": "1",
    "MAX_OPPORTUNITIES_PER_CYCLE": "1",
    # Halt if 2 consecutive baskets fail (fill errors).
    "ARB_CONSECUTIVE_EXECUTION_FAILURES_HALT": "2",
    # 0 = off. With ~300 events some tokens lack CLOB books (Gamma stale) — 10–30 synthetic
    # per cycle is normal; do not block execution on that alone.
    "ARB_HALT_EXECUTION_IF_SYNTHETIC_BOOKS_GE": "0",
    # Require stronger edge on first live session.
    "MIN_COMPLETE_SET_EDGE_BPS": "30",
    # Neg-risk conversion requires calling NegRiskAdapter on-chain from the proxy wallet,
    # which is not directly supported via EOA private key in Polymarket's proxy model.
    # Keep disabled until this is resolved (MATIC + proxy call routing).
    "MIN_NEG_RISK_EDGE_BPS": "999999",
    "ARB_MIN_EXPECTED_PROFIT_USD": "0.20",
    "MAX_TRACKED_EVENTS": "300",
    # Auto-settle resolved events so positions close cleanly.
    "AUTO_SETTLE_RESOLVED_EVENTS": "true",
    # Directional overlay is paper-only; guard is in overlay.py but force off anyway.
    "ENABLE_DIRECTIONAL_OVERLAY": "false",
    # Log at INFO so startup is legible.
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


def kill_listen_ports(ports: list[int]) -> None:
    if sys.platform == "win32":
        for p in ports:
            _win_kill_port(p)
    else:
        for p in ports:
            subprocess.run(["sh", "-c", f"fuser -k {p}/tcp 2>/dev/null || true"], cwd=str(ROOT))


def _tcp_listening(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _run_gate(script: list[str], label: str) -> bool:
    """Run a safety check; return True on success, print error and return False on failure."""
    result = subprocess.run([sys.executable] + script, cwd=str(ROOT))
    if result.returncode != 0:
        print(f"\n[BLOCKED] {label} failed (exit {result.returncode}). Fix above before running live.")
        return False
    return True


def _make_agent_env(
    port: int,
    db_path: str,
    display_name: str,
    half_bankroll: float,
    api_token: str,
) -> dict[str, str]:
    env = os.environ.copy()
    env.update(_SHARED_LIVE)
    env.update({
        "CONTROL_API_PORT": str(port),
        "ARB_SQLITE_PATH": str((ROOT / db_path).resolve()),
        "AGENT_DISPLAY_NAME": display_name,
        "INITIAL_BANKROLL": str(round(half_bankroll, 2)),
        "CONTROL_API_TOKEN": api_token,
    })
    return env


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Start two live arb agents with split bankroll."
    )
    parser.add_argument(
        "--bankroll",
        type=float,
        default=0.0,
        help="Total USDC to split between both agents (default: 2 × INITIAL_BANKROLL from .env).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the CONFIRM prompt (for scripted restarts).",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  Polymarket Live Split — REAL MONEY")
    print("=" * 60)

    # ── Derive bankroll from .env if not specified ────────────────
    total_bankroll = args.bankroll
    if total_bankroll <= 0:
        try:
            from src.config import Settings  # noqa: E402 — after sys.path

            cfg = Settings()
            total_bankroll = float(cfg.initial_bankroll) * 2
            print(f"[*] INITIAL_BANKROLL from .env = {cfg.initial_bankroll}  →  total split: ${total_bankroll:.2f}")
        except Exception as exc:
            print(f"[X] Could not read Settings from .env: {exc}")
            return 1

    half = total_bankroll / 2.0
    print(f"[*] Agent A: ${half:.2f}   Agent B: ${half:.2f}")

    # ── Safety gates ─────────────────────────────────────────────
    print("\n[1/3] Checking environment…")
    if not _run_gate(["scripts/check_env.py"], "check_env"):
        return 1

    print("\n[2/3] Verifying wallet key matches address…")
    if not _run_gate(["scripts/verify_wallet_key_matches_env.py"], "verify_wallet_key_matches_env"):
        return 1

    print("\n[3/3] Checking live API connections…")
    if not _run_gate(["scripts/check_live_connections.py"], "check_live_connections"):
        return 1

    # ── Human confirmation ────────────────────────────────────────
    print()
    print("=" * 60)
    print(f"  About to start 2 LIVE bots with ${half:.2f} each (${total_bankroll:.2f} total).")
    print("  Real USDC will be used for Polymarket orders.")
    print("  Max per-basket: $15   Max per-event: 35% of bankroll")
    print("  Strategy: complete-set arb only (neg-risk pending proxy resolution)")
    print("=" * 60)
    if args.yes:
        print("--yes flag set; skipping confirmation prompt.")
    else:
        answer = input("\nType CONFIRM to start live, or anything else to cancel: ").strip()
        if answer != "CONFIRM":
            print("Cancelled.")
            return 0

    # ── Port cleanup ──────────────────────────────────────────────
    print("\nClearing ports 8765 and 8767…")
    kill_listen_ports([8765, 8767])
    time.sleep(1.5)

    # No CONTROL_API_TOKEN — API is loopback-only so no auth needed for the browser dashboard.
    # If you expose the port externally, set CONTROL_API_TOKEN in .env manually.
    api_token = ""

    # ── Launch agents ─────────────────────────────────────────────
    (ROOT / "data").mkdir(parents=True, exist_ok=True)

    agents = [
        (8765, "data/live_agent_a.db", "Live A — Arb only"),
        (8767, "data/live_agent_b.db", "Live B — Arb + overlay(paper)"),
    ]

    procs: list[subprocess.Popen] = []
    for port, db_path, display in agents:
        env = _make_agent_env(port, db_path, display, half, api_token)
        proc = subprocess.Popen(
            [sys.executable, "-m", "src"],
            cwd=str(ROOT),
            env=env,
            **_popen_kwargs(),
        )
        procs.append(proc)
        print(f"[OK] Started {display!r} — port {port}  PID {proc.pid}  db {db_path}")

    print()
    print("Dashboards (open in browser after a few seconds):")
    print("  Agent A: http://127.0.0.1:8765/ui/index.html")
    print("  Agent B: http://127.0.0.1:8767/ui/index.html")
    print("  Split:   http://127.0.0.1:8765/ui/agents-split.html?left=8765&right=8767")
    print()
    print("Control (add header  X-Control-Token: <token above>):")
    print("  GET  http://127.0.0.1:8765/summary")
    print("  POST http://127.0.0.1:8765/halt   {\"reason\": \"manual\"}")
    print("  POST http://127.0.0.1:8765/resume")
    print()
    print("Press Ctrl+C to stop both agents.")
    print()

    # ── Monitor ───────────────────────────────────────────────────
    try:
        while True:
            time.sleep(2)
            for i, proc in enumerate(procs):
                code = proc.poll()
                if code is not None:
                    port, db, display = agents[i]
                    print(f"\n[!] {display!r} (port {port}) exited with code {code}.")
                    print("    Stopping other agent…")
                    for p in procs:
                        if p.poll() is None:
                            p.terminate()
                    return code
    except KeyboardInterrupt:
        print("\nStopping agents…")
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
