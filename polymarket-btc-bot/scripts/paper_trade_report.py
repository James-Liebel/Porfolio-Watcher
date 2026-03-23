from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "data" / "trades.db"
MIN_FILL_RATE = 0.40
MIN_WIN_RATE = 0.55
MIN_RESOLVED_FILLS = 20
DEFAULT_INITIAL_BANKROLL = 300.0

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from src.config import get_settings
except Exception:
    get_settings = None


def _q(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    cur = conn.execute(sql, params)
    return cur.fetchall()


def _one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Row:
    rows = _q(conn, sql, params)
    return rows[0]


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = _q(conn, f"PRAGMA table_info({table})")
    return any(row["name"] == column for row in rows)


def _ensure_trade_report_schema(conn: sqlite3.Connection) -> None:
    if not _has_column(conn, "trades", "reason"):
        conn.execute("ALTER TABLE trades ADD COLUMN reason TEXT DEFAULT ''")
        conn.commit()


def _current_settings():
    if get_settings is None:
        return None
    try:
        return get_settings()
    except Exception:
        return None


def _starting_bankroll(conn: sqlite3.Connection, start_date: str, configured: float) -> float:
    row = _q(
        conn,
        """
        SELECT starting_bankroll
        FROM daily_summary
        WHERE date >= ?
          AND starting_bankroll IS NOT NULL
        ORDER BY date ASC
        LIMIT 1
        """,
        (start_date,),
    )
    if row and row[0]["starting_bankroll"] is not None:
        return float(row[0]["starting_bankroll"])

    deposits = _one(conn, "SELECT COALESCE(SUM(amount), 0.0) AS total FROM deposits")["total"]
    if deposits and float(deposits) > 0:
        return float(deposits)
    return configured


def _ending_bankroll(conn: sqlite3.Connection, start_date: str, fallback: float) -> float:
    row = _q(
        conn,
        """
        SELECT ending_bankroll
        FROM daily_summary
        WHERE date >= ?
          AND ending_bankroll IS NOT NULL
        ORDER BY date DESC
        LIMIT 1
        """,
        (start_date,),
    )
    if row and row[0]["ending_bankroll"] is not None:
        return float(row[0]["ending_bankroll"])
    return fallback


def _decision_lines(
    *,
    total_orders: int,
    fill_rate: float,
    resolved_fills: int,
    win_rate: float,
    net_pnl: float,
    settings,
) -> tuple[str, list[str]]:
    lines: list[str] = []
    if resolved_fills < MIN_RESOLVED_FILLS:
        lines.append(
            f"Only {resolved_fills} resolved fills. Wait for at least {MIN_RESOLVED_FILLS} before judging live readiness."
        )
        if total_orders < MIN_RESOLVED_FILLS:
            lines.append("The sample is too small to estimate edge, fill quality, or drawdown risk.")
        if settings is not None:
            enabled = ", ".join(settings.enabled_assets())
            lines.append(
                f"Keep PAPER_TRADE=true and stay on the {settings.strategy_profile} profile ({enabled})."
            )
        return "NOT ENOUGH DATA", lines

    if net_pnl <= 0:
        lines.append("Net PnL is non-positive after settled trades.")
        lines.append("Do not move to live capital until the model is positive on settled results.")
        return "DO NOT GO LIVE", lines

    if fill_rate < MIN_FILL_RATE or win_rate < MIN_WIN_RATE:
        if fill_rate < MIN_FILL_RATE:
            lines.append(f"Fill rate {fill_rate:.1%} is below the {MIN_FILL_RATE:.0%} target.")
        if win_rate < MIN_WIN_RATE:
            lines.append(f"Win rate {win_rate:.1%} is below the {MIN_WIN_RATE:.0%} target.")
        lines.append("Tune execution or edge quality before considering live mode.")
        return "TUNE BEFORE GOING LIVE", lines

    lines.append("Settled sample, fill quality, and net PnL all clear the current thresholds.")
    lines.append("Keep the conservative asset profile and start with the same or smaller bankroll than paper.")
    return "GO LIVE", lines


def _print_table(title: str, rows: Sequence[sqlite3.Row], render) -> None:
    print(title)
    for row in rows:
        print(render(row))
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarise paper-trading performance.")
    parser.add_argument("--days", type=int, default=7, help="Trailing lookback window.")
    args = parser.parse_args(argv)

    if not DB_PATH.exists():
        print("No database found at data/trades.db")
        return 1

    settings = _current_settings()
    configured_bankroll = (
        float(settings.initial_bankroll) if settings is not None else DEFAULT_INITIAL_BANKROLL
    )

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _ensure_trade_report_schema(conn)

    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=max(args.days, 1) - 1)
    start_key = start_date.isoformat()

    overall = _one(
        conn,
        """
        SELECT
          COUNT(*) AS total_orders,
          SUM(CASE WHEN filled=1 THEN 1 ELSE 0 END) AS filled_orders,
          SUM(CASE WHEN filled=0 THEN 1 ELSE 0 END) AS not_filled_orders,
          SUM(CASE WHEN filled=1 AND outcome IN ('WIN','LOSS') THEN 1 ELSE 0 END) AS resolved_fills,
          SUM(CASE WHEN filled=1 AND outcome='PENDING' THEN 1 ELSE 0 END) AS pending_fills,
          SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) AS wins,
          SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) AS losses,
          COALESCE(SUM(pnl),0) AS gross_pnl,
          COALESCE(SUM(maker_rebate_earned),0) AS rebates,
          COALESCE(AVG(repost_count),0) AS avg_reposts
        FROM trades
        WHERE paper_trade=1
          AND date(timestamp) >= ?
        """,
        (start_key,),
    )

    by_asset = _q(
        conn,
        """
        SELECT asset,
               COUNT(*) AS trades,
               SUM(CASE WHEN filled=1 THEN 1 ELSE 0 END) AS fills,
               SUM(CASE WHEN outcome IN ('WIN','LOSS') THEN 1 ELSE 0 END) AS resolved,
               SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) AS wins,
               COALESCE(SUM(pnl),0) AS pnl,
               COALESCE(AVG(edge),0) AS avg_edge
        FROM trades
        WHERE paper_trade=1
          AND date(timestamp) >= ?
        GROUP BY asset
        ORDER BY asset
        """,
        (start_key,),
    )

    calib = _q(
        conn,
        """
        SELECT asset,
               COALESCE(AVG(true_prob),0) AS pred,
               COALESCE(
                   SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) * 1.0 /
                   NULLIF(SUM(CASE WHEN outcome IN ('WIN','LOSS') THEN 1 ELSE 0 END), 0),
                   0
               ) AS actual
        FROM trades
        WHERE paper_trade=1
          AND date(timestamp) >= ?
        GROUP BY asset
        ORDER BY asset
        """,
        (start_key,),
    )

    expired = _one(
        conn,
        """
        SELECT COUNT(*) AS expired
        FROM trades
        WHERE paper_trade=1
          AND filled=0
          AND reason='expired'
          AND date(timestamp) >= ?
        """,
        (start_key,),
    )["expired"]

    total_orders = int(overall["total_orders"] or 0)
    filled_orders = int(overall["filled_orders"] or 0)
    not_filled_orders = int(overall["not_filled_orders"] or 0)
    resolved_fills = int(overall["resolved_fills"] or 0)
    pending_fills = int(overall["pending_fills"] or 0)
    wins = int(overall["wins"] or 0)
    losses = int(overall["losses"] or 0)
    gross_pnl = float(overall["gross_pnl"] or 0.0)
    rebates = float(overall["rebates"] or 0.0)
    net_pnl = gross_pnl + rebates
    fill_rate = (filled_orders / total_orders) if total_orders else 0.0
    win_rate = (wins / resolved_fills) if resolved_fills else 0.0
    starting_bankroll = _starting_bankroll(conn, start_key, configured_bankroll)
    ending_bankroll = _ending_bankroll(conn, start_key, starting_bankroll + net_pnl)
    monthly_projection = ((net_pnl / max(args.days, 1)) * 30.0) if args.days else 0.0
    decision, decision_lines = _decision_lines(
        total_orders=total_orders,
        fill_rate=fill_rate,
        resolved_fills=resolved_fills,
        win_rate=win_rate,
        net_pnl=net_pnl,
        settings=settings,
    )
    conn.close()

    print("=======================================")
    print(f"{args.days}-DAY PAPER TRADING REPORT")
    print("=======================================")
    print(f"Period: {start_date} to {end_date}")
    if settings is not None:
        print(
            f"Profile: {settings.strategy_profile} | Auto asset selection: {settings.auto_asset_selection}"
        )
        print(f"Enabled assets now: {', '.join(settings.enabled_assets())}")
    print()
    print("OVERALL")
    print(f"Total orders:          {total_orders}")
    print(f"Fill rate:             {fill_rate:.1%}  target: >= {MIN_FILL_RATE:.0%}")
    print(f"Filled orders:         {filled_orders}")
    print(f"Resolved fills:        {resolved_fills}")
    print(f"Pending fills:         {pending_fills}")
    print(f"Not filled orders:     {not_filled_orders}")
    print(f"Win rate:              {win_rate:.1%}   target: >= {MIN_WIN_RATE:.0%}")
    print(f"Wins / losses:         {wins} / {losses}")
    print(f"Gross PnL:             ${gross_pnl:+.2f}")
    print(f"Rebates earned:        ${rebates:.4f}")
    print(f"Net PnL:               ${net_pnl:+.2f}")
    print(f"Starting bankroll:     ${starting_bankroll:.2f}")
    print(f"Ending bankroll:       ${ending_bankroll:.2f}")
    print(f"Monthly projection:    ${monthly_projection:+.2f}")
    print()

    _print_table(
        "BY ASSET",
        by_asset,
        lambda row: (
            f"{row['asset']:<5} | trades {int(row['trades'] or 0):>3} | "
            f"fills {int(row['fills'] or 0):>3} | resolved {int(row['resolved'] or 0):>3} | "
            f"win% {((int(row['wins'] or 0) / int(row['resolved'] or 1)) * 100.0 if int(row['resolved'] or 0) else 0.0):>5.1f}% | "
            f"PnL ${float(row['pnl'] or 0.0):>7.2f} | avg edge {float(row['avg_edge'] or 0.0) * 100:>5.2f}%"
        ),
    )

    _print_table(
        "SIGNAL ACCURACY",
        calib,
        lambda row: (
            f"{row['asset']:<5} | predicted {float(row['pred'] or 0.0) * 100:>5.1f}% | "
            f"actual {float(row['actual'] or 0.0) * 100:>5.1f}% | "
            f"{'good' if abs((float(row['pred'] or 0.0) - float(row['actual'] or 0.0)) * 100) <= 5.0 else 'needs_tuning'}"
        ),
    )

    print("EXECUTION QUALITY")
    print(f"Avg reposts per order: {float(overall['avg_reposts'] or 0.0):.2f}")
    print(f"Expired unfilled orders: {int(expired or 0)}")
    print("Most active hours (UTC): run custom SQL grouping by strftime('%H', timestamp)")
    print()

    print("GO / NO-GO DECISION")
    print("=======================================")
    print(decision)
    for line in decision_lines:
        print(f"  - {line}")
    print("=======================================")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
