"""Analyze data/live_arb.db for arb tuning (run: python scripts/_analyze_arb_db.py)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "live_arb.db"


def main() -> None:
    if not DB.exists():
        print("missing", DB)
        return
    # Read-only + long timeout so analysis works while the bot holds a write lock.
    ro_uri = DB.resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(ro_uri, uri=True, timeout=60.0)
    c = con.cursor()

    print("db_bytes", DB.stat().st_size)
    for t in ("arb_opportunities", "arb_baskets", "arb_orders"):
        c.execute(f"SELECT COUNT(*) FROM {t}")
        print(t, c.fetchone()[0])

    print("\n=== by strategy_type, decision ===")
    c.execute(
        "SELECT strategy_type, decision, COUNT(*) FROM arb_opportunities "
        "GROUP BY strategy_type, decision ORDER BY strategy_type, decision"
    )
    for row in c.fetchall():
        print(row)

    print("\n=== rejected reasons (top 15) ===")
    c.execute(
        "SELECT reason, COUNT(*) AS n FROM arb_opportunities "
        "WHERE decision = 'rejected' GROUP BY reason ORDER BY n DESC LIMIT 15"
    )
    for reason, n in c.fetchall():
        s = (reason or "").encode("ascii", "replace").decode("ascii")
        print(n, s[:200])

    print("\n=== capital_required for rejected (insufficient cash) sample stats ===")
    c.execute(
        "SELECT AVG(json_extract(payload_json,'$.capital_required')), "
        "MIN(json_extract(payload_json,'$.capital_required')), "
        "MAX(json_extract(payload_json,'$.capital_required')), COUNT(*) "
        "FROM arb_opportunities WHERE decision='rejected' AND reason LIKE '%cash%'"
    )
    print(c.fetchone())

    print("\n=== open baskets by status ===")
    c.execute("SELECT status, COUNT(*) FROM arb_baskets GROUP BY status")
    print(c.fetchall())

    print("\n=== recent OPEN baskets ===")
    c.execute(
        "SELECT id, strategy_type, capital_reserved, substr(notes,1,80), created_at "
        "FROM arb_baskets WHERE status IN ('OPEN','EXECUTING') ORDER BY created_at DESC LIMIT 8"
    )
    for row in c.fetchall():
        print(row)

    print("\n=== failed: top grouped reasons ===")
    c.execute(
        "SELECT strategy_type, reason, COUNT(*) AS n FROM arb_opportunities "
        "WHERE decision = 'failed' GROUP BY strategy_type, reason ORDER BY n DESC LIMIT 25"
    )
    for strategy, reason, n in c.fetchall():
        s = (reason or "").encode("ascii", "replace").decode("ascii")
        print(n, strategy, s[:220])

    print("\n=== executed row ===")
    c.execute(
        "SELECT event_title, strategy_type, expected_profit, capital_required, reason, created_at "
        "FROM arb_opportunities WHERE decision = 'executed' ORDER BY created_at DESC LIMIT 3"
    )
    for row in c.fetchall():
        print(row)

    print("\n=== arb_runtime_state keys (truncated) ===")
    try:
        c.execute("SELECT * FROM arb_runtime_state LIMIT 1")
        row = c.fetchone()
        if row:
            cols = [d[0] for d in c.description]
            for k, v in zip(cols, row):
                s = str(v)
                if len(s) > 180:
                    s = s[:180] + "..."
                print(k, s.encode("ascii", "replace").decode("ascii"))
    except Exception as exc:
        print("runtime_state err", exc)

    con.close()


if __name__ == "__main__":
    main()
