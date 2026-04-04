"""
Summarize structural-arb paper activity from SQLite (legacy + arb_* tables).

Uses the same DB as the bot: default data/trades.db, or ARB_SQLITE_PATH / --db.

Prints:
  - Latest arb_runtime_state (cash, realized PnL, fees, rebates)
  - Per-day: opportunities seen, approved vs rejected, fills, settlement PnL, basket realized PnL

Usage (from polymarket-btc-bot):
  .venv\\Scripts\\python.exe scripts\\arb_session_report.py
  .venv\\Scripts\\python.exe scripts\\arb_session_report.py --db data/arb_agent_1.db --days 14
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.storage.db import get_default_database_path  # noqa: E402


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def _parse_day(ts: str | None) -> str | None:
    if not ts:
        return None
    t = ts.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).date().isoformat()
    except ValueError:
        return t[:10] if len(t) >= 10 else None


def _runtime_state(conn: sqlite3.Connection) -> dict[str, Any] | None:
    if not _table_exists(conn, "arb_runtime_state"):
        return None
    row = conn.execute("SELECT * FROM arb_runtime_state WHERE id = 1").fetchone()
    if not row:
        return None
    return dict(row)


def _daily_rollup(conn: sqlite3.Connection, since_day: str | None) -> dict[str, dict[str, Any]]:
    days: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "opportunities": 0,
            "approved": 0,
            "rejected": 0,
            "fills": 0,
            "fill_fees": 0.0,
            "fill_notional": 0.0,
            "settlements": 0,
            "settlement_pnl": 0.0,
            "baskets_closed": 0,
            "basket_realized_pnl": 0.0,
        }
    )

    def ok_day(d: str | None) -> bool:
        if d is None:
            return False
        if since_day is None:
            return True
        return d >= since_day

    if _table_exists(conn, "arb_opportunities"):
        for row in conn.execute("SELECT created_at, decision FROM arb_opportunities"):
            d = _parse_day(row["created_at"])
            if not ok_day(d):
                continue
            days[d]["opportunities"] += 1
            dec = (row["decision"] or "").lower()
            if dec == "approved":
                days[d]["approved"] += 1
            elif dec == "rejected":
                days[d]["rejected"] += 1

    if _table_exists(conn, "arb_fills"):
        for row in conn.execute(
            "SELECT timestamp, fee_paid, price, size FROM arb_fills",
        ):
            d = _parse_day(row["timestamp"])
            if not ok_day(d):
                continue
            days[d]["fills"] += 1
            days[d]["fill_fees"] += float(row["fee_paid"] or 0)
            days[d]["fill_notional"] += float(row["price"] or 0) * float(row["size"] or 0)

    if _table_exists(conn, "arb_settlements"):
        for row in conn.execute("SELECT timestamp, pnl_realized FROM arb_settlements"):
            d = _parse_day(row["timestamp"])
            if not ok_day(d):
                continue
            days[d]["settlements"] += 1
            days[d]["settlement_pnl"] += float(row["pnl_realized"] or 0)

    if _table_exists(conn, "arb_baskets"):
        for row in conn.execute(
            "SELECT closed_at, realized_net_pnl FROM arb_baskets WHERE closed_at IS NOT NULL",
        ):
            d = _parse_day(row["closed_at"])
            if not ok_day(d):
                continue
            days[d]["baskets_closed"] += 1
            days[d]["basket_realized_pnl"] += float(row["realized_net_pnl"] or 0)

    return dict(days)


def _deposits_since(conn: sqlite3.Connection, since_day: str | None) -> dict[str, float]:
    if not _table_exists(conn, "deposits"):
        return {}
    out: dict[str, float] = defaultdict(float)
    for row in conn.execute("SELECT timestamp, amount FROM deposits"):
        d = _parse_day(row["timestamp"])
        if d is None:
            continue
        if since_day and d < since_day:
            continue
        out[d] += float(row["amount"] or 0)
    return dict(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Arb paper session stats from SQLite.")
    ap.add_argument(
        "--db",
        type=str,
        default="",
        help="SQLite path (default: ARB_SQLITE_PATH env or data/trades.db).",
    )
    ap.add_argument(
        "--days",
        type=int,
        default=30,
        help="Only show days on or after (today - days) UTC.",
    )
    args = ap.parse_args()

    raw = (args.db or "").strip() or (os.environ.get("ARB_SQLITE_PATH") or "").strip()
    path = Path(raw) if raw else Path(get_default_database_path())
    if not path.is_file():
        print(f"Database not found: {path.resolve()}")
        return 1

    since = (datetime.now(timezone.utc) - timedelta(days=max(1, args.days))).date().isoformat()

    conn = _connect(path)
    try:
        rs = _runtime_state(conn)
        print(f"Database: {path.resolve()}\n")
        if rs:
            print(
                "arb_runtime_state (latest)",
                f"cash={rs.get('cash')!s}",
                f"realized_pnl={rs.get('realized_pnl')!s}",
                f"fees_paid={rs.get('fees_paid')!s}",
                f"rebates_earned={rs.get('rebates_earned')!s}",
                f"updated_at={rs.get('updated_at')!s}",
                sep="\n  ",
            )
            print()
        else:
            print("(no arb_runtime_state row — bot may not have run yet)\n")

        roll = _daily_rollup(conn, since)
        deposits = _deposits_since(conn, since)
        all_days = sorted(set(roll.keys()) | set(deposits.keys()))
        if not all_days:
            print(f"No arb activity on or after {since} (opportunities/fills/settlements).")
            return 0

        print(f"Daily rollup (UTC date, since {since})\n")
        hdr = (
            f"{'date':<12} {'opp':>5} {'appr':>5} {'rej':>5} "
            f"{'fills':>5} {'fees':>10} {'settle$':>10} {'basket$':>10} {'deposit':>10}"
        )
        print(hdr)
        print("-" * len(hdr))
        t_opp = t_ap = t_rj = t_fl = 0
        t_fees = t_st = t_bk = t_dep = 0.0
        for d in all_days:
            r = roll.get(
                d,
                {
                    "opportunities": 0,
                    "approved": 0,
                    "rejected": 0,
                    "fills": 0,
                    "fill_fees": 0.0,
                    "fill_notional": 0.0,
                    "settlements": 0,
                    "settlement_pnl": 0.0,
                    "baskets_closed": 0,
                    "basket_realized_pnl": 0.0,
                },
            )
            dep = deposits.get(d, 0.0)
            print(
                f"{d:<12} {r['opportunities']:>5} {r['approved']:>5} {r['rejected']:>5} "
                f"{r['fills']:>5} {r['fill_fees']:>10.4f} {r['settlement_pnl']:>10.4f} "
                f"{r['basket_realized_pnl']:>10.4f} {dep:>10.4f}"
            )
            t_opp += r["opportunities"]
            t_ap += r["approved"]
            t_rj += r["rejected"]
            t_fl += r["fills"]
            t_fees += r["fill_fees"]
            t_st += r["settlement_pnl"]
            t_bk += r["basket_realized_pnl"]
            t_dep += dep
        print("-" * len(hdr))
        print(
            f"{'TOTAL':<12} {t_opp:>5} {t_ap:>5} {t_rj:>5} {t_fl:>5} {t_fees:>10.4f} "
            f"{t_st:>10.4f} {t_bk:>10.4f} {t_dep:>10.4f}"
        )
        print(
            "\nNotes: settlement_pnl / basket_realized_pnl are from DB columns; "
            "arb_runtime_state.realized_pnl is the exchange ledger total."
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
