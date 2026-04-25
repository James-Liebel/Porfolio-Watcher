"""Print recent arb_opportunities activity from data/live_arb.db (read-only)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "live_arb.db"


def main() -> None:
    if not DB.exists():
        print("missing", DB)
        return
    uri = DB.resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=60)
    c = con.cursor()

    print("=== last 7d opportunity decisions ===")
    c.execute(
        """
        SELECT decision, COUNT(*) FROM arb_opportunities
        WHERE created_at >= datetime('now', '-7 days')
        GROUP BY decision
        """
    )
    rows = c.fetchall()
    print(rows if rows else "(no rows in window)")

    print("\n=== last 7d rejected top reasons ===")
    c.execute(
        """
        SELECT reason, COUNT(*) AS n FROM arb_opportunities
        WHERE created_at >= datetime('now', '-7 days') AND decision = 'rejected'
        GROUP BY reason ORDER BY n DESC LIMIT 15
        """
    )
    for reason, n in c.fetchall():
        s = (reason or "").encode("ascii", "replace").decode("ascii")
        print(n, s[:200])

    print("\n=== last 15 opportunities ===")
    c.execute(
        """
        SELECT created_at, decision, strategy_type,
               substr(COALESCE(reason,''), 1, 90) AS rsn,
               round(json_extract(payload_json, '$.capital_required'), 2) AS cap
        FROM arb_opportunities ORDER BY created_at DESC LIMIT 15
        """
    )
    for row in c.fetchall():
        print(row)

    print("\n=== OPEN / EXECUTING baskets ===")
    c.execute(
        """
        SELECT id, status, strategy_type, round(capital_reserved, 2), created_at
        FROM arb_baskets WHERE status IN ('OPEN', 'EXECUTING')
        ORDER BY created_at DESC LIMIT 8
        """
    )
    print(c.fetchall())

    print("\n=== runtime_state ===")
    c.execute(
        "SELECT updated_at, cash, contributed_capital, realized_pnl FROM arb_runtime_state LIMIT 1"
    )
    print(c.fetchone())

    print("\n=== last 3d failed sample ===")
    c.execute(
        """
        SELECT created_at, strategy_type, substr(reason, 1, 100)
        FROM arb_opportunities
        WHERE decision = 'failed' AND created_at >= datetime('now', '-3 days')
        ORDER BY created_at DESC LIMIT 8
        """
    )
    for row in c.fetchall():
        print(row)

    con.close()


if __name__ == "__main__":
    main()
