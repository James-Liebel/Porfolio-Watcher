from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.storage.db import Database  # noqa: E402


@dataclass
class _SampleTrade:
    timestamp: datetime
    market_id: str
    question: str
    side: str
    bet_size: Decimal
    share_quantity: Decimal
    limit_price: Decimal
    filled: bool
    fill_price: Decimal | None
    outcome: str
    pnl: float | None
    delta: float
    edge: float
    true_prob: float
    market_prob: float
    seconds_at_entry: int
    paper_trade: bool
    asset: str
    maker_rebate_earned: Decimal
    order_type: str
    repost_count: int


REQUIRED_TRADE_COLUMNS = {
    "id",
    "timestamp",
    "market_id",
    "question",
    "side",
    "bet_size",
    "share_quantity",
    "limit_price",
    "filled",
    "fill_price",
    "outcome",
    "pnl",
    "delta",
    "edge",
    "true_prob",
    "market_prob",
    "seconds_at_entry",
    "paper_trade",
    "asset",
    "maker_rebate_earned",
    "order_type",
    "repost_count",
}


async def main() -> int:
    db = Database()
    await db.init()

    import aiosqlite

    missing_cols = []
    async with aiosqlite.connect(db._path) as conn:  # intentional for validation
        async with conn.execute("PRAGMA table_info(trades)") as cur:
            rows = await cur.fetchall()
            columns = {row[1] for row in rows}
        missing_cols = sorted(REQUIRED_TRADE_COLUMNS - columns)

    if missing_cols:
        print("[X] Database schema missing columns:")
        for col in missing_cols:
            print(f"  - {col}")
        return 1

    sample = _SampleTrade(
        timestamp=datetime.now(timezone.utc),
        market_id="sample_market",
        question="Sample paper trade",
        side="YES",
        bet_size=Decimal("10.50"),
        share_quantity=Decimal("21.00"),
        limit_price=Decimal("0.50"),
        filled=True,
        fill_price=Decimal("0.50"),
        outcome="PENDING",
        pnl=None,
        delta=0.003,
        edge=0.08,
        true_prob=0.60,
        market_prob=0.52,
        seconds_at_entry=18,
        paper_trade=True,
        asset="BTC",
        maker_rebate_earned=Decimal("0.0105"),
        order_type="maker_gtc",
        repost_count=1,
    )

    row_id = await db.insert_trade(sample)
    trades = await db.get_all_trades(limit=1)
    if not trades:
        print("[X] Failed to read back inserted trade")
        return 1

    t = trades[0]
    if int(t["id"]) != int(row_id) or t["asset"] != "BTC" or int(t["paper_trade"]) != 1:
        print("[X] Insert/read mismatch for sample trade")
        return 1

    stats = await db.get_today_stats()
    if stats is not None and not isinstance(stats, dict):
        print("[X] get_today_stats() returned unexpected format")
        return 1

    print("[OK] Database schema correct")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
