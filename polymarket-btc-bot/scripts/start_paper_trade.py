from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "scripts"


def _run_script(name: str, allow_no_markets: bool = False) -> None:
    script = SCRIPTS / name
    result = subprocess.run([sys.executable, str(script)], cwd=str(PROJECT_ROOT))
    if result.returncode != 0:
        raise RuntimeError(f"{name} failed")
    if allow_no_markets:
        return


def main() -> int:
    env = dotenv_values(PROJECT_ROOT / ".env")
    paper_mode = str(env.get("PAPER_TRADE", "false")).lower() == "true"
    if not paper_mode:
        print(
            "Safety check: PAPER_TRADE is not set to true.\n"
            "Set PAPER_TRADE=true in .env before paper trading."
        )
        return 1

    checks = [
        "check_env.py",
        "test_feeds.py",
        "test_scanner.py",
        "test_signal.py",
        "test_db.py",
        "test_telegram.py",
    ]

    for check in checks:
        try:
            _run_script(check, allow_no_markets=(check == "test_scanner.py"))
        except Exception as exc:
            print(f"Verification failed: {exc}")
            print("Fix the failing script output above before starting paper trading.")
            return 1

    end_date = (datetime.utcnow() + timedelta(days=7)).strftime("%Y-%m-%d")
    print("====================================")
    print("PAPER TRADING WEEK — starting now")
    print("====================================")
    print("Capital:     $300.00 (simulated)")
    print("Strategy:    BTC/ETH/SOL/XRP 5-min maker")
    print("Edge threshold: 7%")
    print("Kelly fraction: 25%")
    print("Max bet:     10% of bankroll ($30.00)")
    print("Daily loss cap: 15% ($45.00)")
    print(f"Duration:    7 days (ends {end_date})")
    print()
    print("What to watch in Telegram:")
    print("  Fill rate target:  > 40% of posted orders fill")
    print("  Win rate target:   > 55% of filled trades win")
    print("  Edge accuracy:     actual win rate within 5%")
    print("                     of predicted true_prob")
    print()
    print("Metrics to check daily:")
    print("  SELECT asset, COUNT(*),")
    print("         ROUND(AVG(edge)*100,1) as avg_edge_pct,")
    print("         ROUND(SUM(CASE WHEN outcome='WIN'")
    print("             THEN 1.0 ELSE 0 END)/")
    print("             COUNT(*)*100,1) as win_rate_pct,")
    print("         ROUND(SUM(pnl),2) as total_pnl")
    print("  FROM trades")
    print("  WHERE paper_trade=1")
    print("    AND outcome != 'PENDING'")
    print("  GROUP BY asset;")
    print("====================================")

    return subprocess.run([sys.executable, "-m", "src"], cwd=str(PROJECT_ROOT)).returncode


if __name__ == "__main__":
    raise SystemExit(main())
