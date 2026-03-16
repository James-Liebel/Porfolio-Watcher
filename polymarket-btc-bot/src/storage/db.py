"""Async SQLite storage for trades and daily summaries."""
from __future__ import annotations

import os
from datetime import date
from typing import Any, Dict, List, Optional

import aiosqlite
import structlog

logger = structlog.get_logger(__name__)

_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "trades.db")

_CREATE_TRADES = """
CREATE TABLE IF NOT EXISTS trades (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp             TEXT NOT NULL,
    market_id             TEXT NOT NULL,
    question              TEXT,
    side                  TEXT NOT NULL,
    bet_size              REAL NOT NULL,
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
    repost_count          INTEGER DEFAULT 0
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

# Migration: safely add new columns to existing databases
_MIGRATIONS = [
    "ALTER TABLE trades ADD COLUMN asset TEXT DEFAULT 'BTC'",
    "ALTER TABLE trades ADD COLUMN maker_rebate_earned REAL DEFAULT 0.0",
    "ALTER TABLE trades ADD COLUMN order_type TEXT DEFAULT 'maker_gtc'",
    "ALTER TABLE trades ADD COLUMN repost_count INTEGER DEFAULT 0",
]


class Database:
    def __init__(self, path: str = _DB_PATH) -> None:
        self._path = os.path.abspath(path)
        os.makedirs(os.path.dirname(self._path), exist_ok=True)

    async def init(self) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(_CREATE_TRADES)
            await db.execute(_CREATE_DAILY)
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
              (timestamp, market_id, question, side, bet_size, limit_price,
               filled, fill_price, outcome, pnl, delta, edge, true_prob,
               market_prob, seconds_at_entry, paper_trade,
               asset, maker_rebate_earned, order_type, repost_count)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """
        params = (
            trade.timestamp.isoformat(),
            trade.market_id,
            trade.question,
            trade.side,
            float(trade.bet_size),
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
        )
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(sql, params)
            await db.commit()
            row_id = cursor.lastrowid
        logger.info("db.trade_inserted", id=row_id, market_id=trade.market_id)
        return row_id

    async def update_trade_outcome(self, row_id: int, outcome: str, pnl: float) -> None:
        sql = "UPDATE trades SET outcome=?, pnl=? WHERE id=?"
        async with aiosqlite.connect(self._path) as db:
            await db.execute(sql, (outcome, pnl, row_id))
            await db.commit()

    async def get_today_stats(self) -> Optional[Dict[str, Any]]:
        today = str(date.today())
        sql = "SELECT * FROM daily_summary WHERE date=?"
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (today,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                base = dict(row)

        # Augment with live trade-level stats not stored in daily_summary
        async with aiosqlite.connect(self._path) as db:
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

    async def get_all_trades(self, limit: int = 100) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM trades ORDER BY id DESC LIMIT ?"
        async with aiosqlite.connect(self._path) as db:
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
        async with aiosqlite.connect(self._path) as db:
            async with db.execute(sql, (today,)) as cursor:
                rows = await cursor.fetchall()
        result: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            result[row[0]] = {"trades": row[1], "wins": row[2], "pnl": row[3]}
        return result

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
        async with aiosqlite.connect(self._path) as db:
            await db.execute(sql, params)
            await db.commit()
