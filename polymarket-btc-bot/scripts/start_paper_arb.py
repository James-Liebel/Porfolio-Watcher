#!/usr/bin/env python3
"""
Start one PAPER structural-arbitrage bot (no Ollama, no overlay, no advisor).

- Port 8765, DB data/paper_arb.db (reset on each run like before).
- Full nominal bankroll below (not split).
- CPU-aware poll / CLOB concurrency / universe cap via src.arb.host_tuning.

Usage (repo root):  python scripts/start_paper_arb.py
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.arb.host_tuning import cpu_count_safe, structural_bot_env_from_cpu  # noqa: E402

# Single bot — full nominal bankroll in one process (tune INITIAL_BANKROLL / .env).
# Basket slots, Gamma pages, and MAX_TRACKED_EVENTS come from structural_bot_env_from_cpu("paper").
_SHARED_PAPER: dict[str, str] = {
    "PAPER_TRADE": "true",
    "ARB_LIVE_EXECUTION": "false",
    "ALLOW_TAKER_EXECUTION": "true",
    "CONTROL_API_TOKEN": "",
    "INITIAL_BANKROLL": "500",
    "PAPER_TAKER_FEE_BPS": "50",
    "PAPER_SPREAD_PENALTY_BPS": "15",
    "MAX_BASKET_NOTIONAL": "120",
    "ARB_BASKET_NOTIONAL_FRACTION_OF_EQUITY": "0.34",
    "ARB_BASKET_NOTIONAL_MIN_USD": "8",
    "ARB_MAX_BASKET_NOTIONAL_QUALIFIED_MULTIPLIER": "1.2",
    "MAX_EVENT_EXPOSURE_PCT": "0.48",
    "ARB_HALT_EXECUTION_IF_SYNTHETIC_BOOKS_GE": "15",
    "MIN_COMPLETE_SET_EDGE_BPS": "18",
    "MIN_NEG_RISK_EDGE_BPS": "28",
    "ARB_MIN_EXPECTED_PROFIT_USD": "0.1",
    "ENABLE_DIRECTIONAL_OVERLAY": "false",
    "ENABLE_TRADER_FOLLOW": "false",
    "UNIVERSE_PREFER_NEG_RISK": "true",
}


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


def unlink_sqlite(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix) if suffix else path
        try:
            p.unlink(missing_ok=True)
        except OSError:
            if p.is_file():
                p.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description="Start paper structural arb (single bot).")
    parser.add_argument(
        "--log-child",
        metavar="PATH",
        help="Append child process stdout/stderr to this file (JSON logs).",
    )
    args = parser.parse_args()

    port = 8765
    rel_db = "data/paper_arb.db"
    kill_listen_ports([8765, 8767, 8780])
    time.sleep(1.5)

    (ROOT / "data").mkdir(parents=True, exist_ok=True)
    db_path = (ROOT / rel_db).resolve()
    unlink_sqlite(db_path)

    popen_kw: dict = {}
    if sys.platform == "win32":
        popen_kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    env = os.environ.copy()
    env.update(_SHARED_PAPER)
    host_tuning = structural_bot_env_from_cpu("paper")
    env.update(host_tuning)
    env.update(
        {
            "CONTROL_API_PORT": str(port),
            "ARB_SQLITE_PATH": str(db_path),
            "AGENT_DISPLAY_NAME": "Paper — structural arb",
        }
    )

    log_f = None
    if args.log_child:
        log_path = Path(args.log_child)
        if not log_path.is_absolute():
            log_path = (ROOT / log_path).resolve()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_f = open(log_path, "a", encoding="utf-8")
        popen_kw["stdout"] = log_f
        popen_kw["stderr"] = subprocess.STDOUT

    subprocess.Popen(
        [sys.executable, "-m", "src"],
        cwd=str(ROOT),
        env=env,
        **popen_kw,
    )
    # Keep log file handle open until launcher exits so the child keeps a valid stdout sink on Windows.
    n_cpu = cpu_count_safe()
    print(f"Started paper structural arb on port {port}  db={rel_db}")
    print(
        f"  Host tuning: CPUs≈{n_cpu}  CLOB_CONC={host_tuning['CLOB_BOOK_FETCH_CONCURRENCY']}  "
        f"MAX_TRACKED_EVENTS={host_tuning['MAX_TRACKED_EVENTS']}  POLL={host_tuning['ARB_POLL_SECONDS']}s"
    )
    print(f"  Dashboard: http://127.0.0.1:{port}/ui/index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
