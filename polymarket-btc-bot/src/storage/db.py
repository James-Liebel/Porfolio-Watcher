"""Async SQLite storage for trades and daily summaries."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import date
from typing import Any, AsyncIterator, Dict, List, Optional

import aiosqlite
import structlog

logger = structlog.get_logger(__name__)

# Match arb repository: WAL + long busy wait so API reads (deposits, /summary) do not fail
# while the engine holds the DB during long cycles (record_book, batch upserts).
_SQLITE_BUSY_TIMEOUT_MS = 30000

_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "trades.db")


def get_default_database_path() -> str:
    """Absolute path to the default legacy + arb SQLite file."""
    return os.path.abspath(_DB_PATH)

_CREATE_TRADES = """
CREATE TABLE IF NOT EXISTS trades (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp             TEXT NOT NULL,
    market_id             TEXT NOT NULL,
    question              TEXT,
    side                  TEXT NOT NULL,
    bet_size              REAL NOT NULL,
    share_quantity        REAL DEFAULT 0.0,
    limit_price           REAL NOT NULL,
    filled                INTEGER NOT NULL,
    fill_price            REAL,
    outcome               TEXT,
    pnl                   REAL,
    delta                 REAL,
    edge                  REAL,
    true_prob             REAL,
    market_prob           REAL,
    seconds_at_entry      INTEGER,
    paper_trade           INTEGER DEFAULT 0,
    asset                 TEXT DEFAULT 'BTC',
    maker_rebate_earned   REAL DEFAULT 0.0,
    order_type            TEXT DEFAULT 'maker_gtc',
    repost_count          INTEGER DEFAULT 0,
    reason                TEXT DEFAULT ''
);
"""

_CREATE_DAILY = """
CREATE TABLE IF NOT EXISTS daily_summary (
    date              TEXT PRIMARY KEY,
    trades            INTEGER,
    wins              INTEGER,
    losses            INTEGER,
    not_filled        INTEGER,
    gross_pnl         REAL,
    starting_bankroll REAL,
    ending_bankroll   REAL
);
"""

_CREATE_DEPOSITS = """
CREATE TABLE IF NOT EXISTS deposits (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    amount    REAL NOT NULL,
    note      TEXT
);
"""

# Migration: safely add new columns to existing databases
_MIGRATIONS = [
    "ALTER TABLE trades ADD COLUMN share_quantity REAL DEFAULT 0.0",
    "ALTER TABLE trades ADD COLUMN asset TEXT DEFAULT 'BTC'",
    "ALTER TABLE trades ADD COLUMN maker_rebate_earned REAL DEFAULT 0.0",
    "ALTER TABLE trades ADD COLUMN order_type TEXT DEFAULT 'maker_gtc'",
    "ALTER TABLE trades ADD COLUMN repost_count INTEGER DEFAULT 0",
    "ALTER TABLE trades ADD COLUMN reason TEXT DEFAULT ''",
]


class Database:
    def __init__(self, path: str = _DB_PATH) -> None:
        self._path = os.path.abspath(path)
        os.makedirs(os.path.dirname(self._path), exist_ok=True)

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[aiosqlite.Connection]:
        async with aiosqlite.connect(self._path, timeout=60.0) as db:
            await db.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}")
            try:
                await db.execute("PRAGMA journal_mode = WAL")
            except Exception:
                pass
            await db.execute("PRAGMA synchronous = NORMAL")
            yield db

    async def init(self) -> None:
        async with self._connect() as db:
            await db.execute(_CREATE_TRADES)
            await db.execute(_CREATE_DAILY)
            await db.execute(_CREATE_DEPOSITS)
            await db.commit()
            # Run migrations for existing databases (ignore errors for columns that already exist)
            for sql in _MIGRATIONS:
                try:
                    await db.execute(sql)
                    await db.commit()
                except Exception:
                    pass  # Column already exists — normal on fresh installs
        logger.info("db.initialized", path=self._path)

    async def insert_trade(self, trade) -> int:
        """Insert a TradeResult and return the new row id."""
        sql = """
            INSERT INTO trades
              (timestamp, market_id, question, side, bet_size, share_quantity, limit_price,
               filled, fill_price, outcome, pnl, delta, edge, true_prob,
               market_prob, seconds_at_entry, paper_trade,
               asset, maker_rebate_earned, order_type, repost_count, reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """
        params = (
            trade.timestamp.isoformat(),
            trade.market_id,
            trade.question,
            trade.side,
            float(trade.bet_size),
            float(getattr(trade, "share_quantity", 0)),
            float(trade.limit_price),
            1 if trade.filled else 0,
            float(trade.fill_price) if trade.fill_price else None,
            trade.outcome,
            trade.pnl,
            trade.delta,
            trade.edge,
            trade.true_prob,
            trade.market_prob,
            trade.seconds_at_entry,
            1 if trade.paper_trade else 0,
            getattr(trade, "asset", "BTC"),
            float(getattr(trade, "maker_rebate_earned", 0)),
            getattr(trade, "order_type", "maker_gtc"),
            getattr(trade, "repost_count", 0),
            getattr(trade, "reason", ""),
        )
        async with self._connect() as db:
            cursor = await db.execute(sql, params)
            await db.commit()
            row_id = cursor.lastrowid
        logger.info("db.trade_inserted", id=row_id, market_id=trade.market_id)
        return row_id

    async def update_trade_outcome(self, row_id: int, outcome: str, pnl: float) -> None:
        sql = "UPDATE trades SET outcome=?, pnl=? WHERE id=?"
        async with self._connect() as db:
            await db.execute(sql, (outcome, pnl, row_id))
            await db.commit()

    async def get_today_stats(self) -> Optional[Dict[str, Any]]:
        today = str(date.today())
        sql = "SELECT * FROM daily_summary WHERE date=?"
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (today,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                base = dict(row)

        # Augment with live trade-level stats not stored in daily_summary
        async with self._connect() as db:
            # Total rebates today
            async with db.execute(
                "SELECT COALESCE(SUM(maker_rebate_earned),0) FROM trades WHERE date(timestamp)=?",
                (today,),
            ) as c:
                row2 = await c.fetchone()
                base["total_rebates_earned"] = row2[0] if row2 else 0.0

            # Trades by asset today
            async with db.execute(
                "SELECT asset, COUNT(*) FROM trades WHERE date(timestamp)=? GROUP BY asset",
                (today,),
            ) as c:
                rows = await c.fetchall()
                base["trades_by_asset"] = {r[0]: r[1] for r in rows}

            # Fill rate today
            async with db.execute(
                "SELECT COUNT(*), SUM(filled) FROM trades WHERE date(timestamp)=?",
                (today,),
            ) as c:
                row3 = await c.fetchone()
                total, filled = (row3[0] or 0), (row3[1] or 0)
                base["fill_rate"] = (filled / total) if total > 0 else 0.0

        return base

    async def insert_deposit(self, amount: float, note: str = "") -> int:
        """Record a deposit of funds to the bankroll. Returns the new row id."""
        from datetime import datetime, timezone
        sql = "INSERT INTO deposits (timestamp, amount, note) VALUES (?,?,?)"
        params = (datetime.now(timezone.utc).isoformat(), amount, note)
        async with self._connect() as db:
            cursor = await db.execute(sql, params)
            await db.commit()
            row_id = cursor.lastrowid
        logger.info("db.deposit_recorded", id=row_id, amount=amount, note=note)
        return row_id

    async def get_total_deposits(self) -> float:
        """Sum of all recorded deposits (used to initialise bankroll on restart)."""
        sql = "SELECT COALESCE(SUM(amount), 0.0) FROM deposits"
        async with self._connect() as db:
            async with db.execute(sql) as cursor:
                row = await cursor.fetchone()
                return float(row[0]) if row else 0.0

    async def get_deposits(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return recent deposit records, newest first."""
        sql = "SELECT * FROM deposits ORDER BY id DESC LIMIT ?"
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (limit,)) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    async def get_all_trades(self, limit: int = 100) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM trades ORDER BY id DESC LIMIT ?"
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (limit,)) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    async def get_asset_trade_stats(self) -> Dict[str, Dict[str, Any]]:
        """Per-asset PnL and win counts for the /stats/assets endpoint."""
        today = str(date.today())
        sql = """
            SELECT asset,
                   COUNT(*) AS trades,
                   SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) AS wins,
                   COALESCE(SUM(pnl), 0) AS pnl
            FROM trades
            WHERE date(timestamp) = ?
            GROUP BY asset
        """
        async with self._connect() as db:
            async with db.execute(sql, (today,)) as cursor:
                rows = await cursor.fetchall()
        result: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            result[row[0]] = {"trades": row[1], "wins": row[2], "pnl": row[3]}
        return result

    async def get_edge_bucket_stats(self) -> List[Dict[str, Any]]:
        """Return predicted vs actual win rates by edge bucket for diagnostics."""
        sql = """
            SELECT
              CASE
                WHEN edge < 0.05 THEN '<5%'
                WHEN edge < 0.10 THEN '5-10%'
                WHEN edge < 0.15 THEN '10-15%'
                ELSE '15%+'
              END AS edge_bucket,
              COUNT(*) AS trades,
              AVG(true_prob) AS avg_pred_true_prob,
              SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) * 1.0 /
                NULLIF(SUM(CASE WHEN outcome IN ('WIN','LOSS') THEN 1 ELSE 0 END), 0)
                AS actual_win_rate
            FROM trades
            WHERE outcome IN ('WIN','LOSS')
            GROUP BY edge_bucket
            ORDER BY
              CASE edge_bucket
                WHEN '<5%' THEN 1
                WHEN '5-10%' THEN 2
                WHEN '10-15%' THEN 3
                ELSE 4
              END
        """
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    async def get_execution_quality_stats(self) -> Dict[str, Any]:
        """Return fill-rate and repost quality diagnostics."""
        sql = """
            SELECT
              COUNT(*) AS total_orders,
              SUM(filled) AS filled_orders,
              AVG(repost_count) AS avg_reposts,
              SUM(CASE WHEN reason='expired' THEN 1 ELSE 0 END) AS expired_orders
            FROM trades
        """
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return {
                        "total_orders": 0,
                        "filled_orders": 0,
                        "fill_rate": 0.0,
                        "avg_reposts": 0.0,
                        "expired_orders": 0,
                    }
                total = row["total_orders"] or 0
                filled = row["filled_orders"] or 0
                fill_rate = (filled / total) if total else 0.0
                return {
                    "total_orders": total,
                    "filled_orders": filled,
                    "fill_rate": fill_rate,
                    "avg_reposts": float(row["avg_reposts"] or 0.0),
                    "expired_orders": row["expired_orders"] or 0,
                }

    async def upsert_daily_summary(self, date_str: str, stats: Dict[str, Any]) -> None:
        sql = """
            INSERT INTO daily_summary
              (date, trades, wins, losses, not_filled, gross_pnl,
               starting_bankroll, ending_bankroll)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(date) DO UPDATE SET
              trades=excluded.trades,
              wins=excluded.wins,
              losses=excluded.losses,
              not_filled=excluded.not_filled,
              gross_pnl=excluded.gross_pnl,
              starting_bankroll=excluded.starting_bankroll,
              ending_bankroll=excluded.ending_bankroll
        """
        params = (
            date_str,
            stats.get("trades", 0),
            stats.get("wins", 0),
            stats.get("losses", 0),
            stats.get("not_filled", 0),
            stats.get("gross_pnl", 0.0),
            stats.get("starting_bankroll", 0.0),
            stats.get("ending_bankroll", 0.0),
        )
        async with self._connect() as db:
            await db.execute(sql, params)
            await db.commit()
