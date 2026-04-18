from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import aiosqlite
import structlog

from ..storage.db import _DB_PATH
from .models import ArbEvent, ArbOpportunity, BasketRecord, FillRecord, OrderRecord, PositionRecord, TokenBook

logger = structlog.get_logger(__name__)
_SQLITE_BUSY_TIMEOUT_MS = 30000


@asynccontextmanager
async def _connect(path: str):
    async with aiosqlite.connect(path, timeout=30.0) as db:
        await db.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}")
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("PRAGMA synchronous = NORMAL")
        yield db

_CREATE_EVENTS = """
CREATE TABLE IF NOT EXISTS arb_events (
    id                  TEXT PRIMARY KEY,
    title               TEXT NOT NULL,
    category            TEXT,
    neg_risk            INTEGER DEFAULT 0,
    enable_neg_risk     INTEGER DEFAULT 0,
    neg_risk_augmented  INTEGER DEFAULT 0,
    status              TEXT,
    liquidity           REAL DEFAULT 0.0,
    rules_text          TEXT,
    end_time            TEXT,
    raw_json            TEXT,
    updated_at          TEXT NOT NULL
);
"""

_CREATE_MARKETS = """
CREATE TABLE IF NOT EXISTS arb_markets (
    id                  TEXT PRIMARY KEY,
    event_id            TEXT NOT NULL,
    question            TEXT,
    outcome_name        TEXT,
    yes_token_id        TEXT,
    no_token_id         TEXT,
    current_yes_price   REAL DEFAULT 0.0,
    current_no_price    REAL DEFAULT 0.0,
    liquidity           REAL DEFAULT 0.0,
    tick_size           REAL DEFAULT 0.01,
    fees_enabled        INTEGER DEFAULT 0,
    status              TEXT,
    raw_json            TEXT,
    updated_at          TEXT NOT NULL
);
"""

_CREATE_BOOKS = """
CREATE TABLE IF NOT EXISTS arb_books (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id        TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    best_bid        REAL DEFAULT 0.0,
    best_ask        REAL DEFAULT 0.0,
    mid             REAL DEFAULT 0.0,
    spread          REAL DEFAULT 0.0,
    fees_enabled    INTEGER DEFAULT 0,
    tick_size       REAL DEFAULT 0.01,
    source          TEXT,
    depth_json      TEXT
);
"""

_CREATE_OPPORTUNITIES = """
CREATE TABLE IF NOT EXISTS arb_opportunities (
    id                   TEXT PRIMARY KEY,
    strategy_type        TEXT NOT NULL,
    event_id             TEXT NOT NULL,
    event_title          TEXT,
    gross_edge_bps       REAL,
    net_edge_bps         REAL,
    capital_required     REAL,
    expected_profit      REAL,
    decision             TEXT,
    reason               TEXT,
    requires_conversion  INTEGER DEFAULT 0,
    settle_on_resolution INTEGER DEFAULT 0,
    created_at           TEXT NOT NULL,
    payload_json         TEXT
);
"""

_CREATE_BASKETS = """
CREATE TABLE IF NOT EXISTS arb_baskets (
    id                  TEXT PRIMARY KEY,
    opportunity_id      TEXT NOT NULL,
    event_id            TEXT NOT NULL,
    strategy_type       TEXT NOT NULL,
    status              TEXT NOT NULL,
    capital_reserved    REAL DEFAULT 0.0,
    target_net_edge_bps REAL DEFAULT 0.0,
    realized_net_pnl    REAL DEFAULT 0.0,
    notes               TEXT,
    created_at          TEXT NOT NULL,
    closed_at           TEXT,
    payload_json        TEXT
);
"""

_CREATE_ORDERS = """
CREATE TABLE IF NOT EXISTS arb_orders (
    id              TEXT PRIMARY KEY,
    basket_id       TEXT NOT NULL,
    opportunity_id  TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    market_id       TEXT NOT NULL,
    side            TEXT NOT NULL,
    price           REAL NOT NULL,
    size            REAL NOT NULL,
    order_type      TEXT NOT NULL,
    maker_or_taker  TEXT NOT NULL,
    status          TEXT NOT NULL,
    filled_size     REAL DEFAULT 0.0,
    average_price   REAL DEFAULT 0.0,
    fees_enabled    INTEGER DEFAULT 0,
    reason          TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    payload_json    TEXT
);
"""

_CREATE_FILLS = """
CREATE TABLE IF NOT EXISTS arb_fills (
    id              TEXT PRIMARY KEY,
    order_id        TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    market_id       TEXT NOT NULL,
    event_id        TEXT NOT NULL,
    side            TEXT NOT NULL,
    price           REAL NOT NULL,
    size            REAL NOT NULL,
    fee_paid        REAL DEFAULT 0.0,
    rebate_earned   REAL DEFAULT 0.0,
    timestamp       TEXT NOT NULL
);
"""

_CREATE_POSITIONS = """
CREATE TABLE IF NOT EXISTS arb_positions (
    token_id        TEXT PRIMARY KEY,
    market_id       TEXT NOT NULL,
    event_id        TEXT NOT NULL,
    outcome_name    TEXT,
    contract_side   TEXT NOT NULL,
    size            REAL NOT NULL,
    avg_price       REAL NOT NULL,
    updated_at      TEXT NOT NULL
);
"""

_CREATE_CONVERSIONS = """
CREATE TABLE IF NOT EXISTS arb_conversions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    basket_id       TEXT NOT NULL,
    event_id        TEXT NOT NULL,
    input_market_id TEXT NOT NULL,
    input_token_id  TEXT NOT NULL,
    outputs_json    TEXT NOT NULL,
    size            REAL NOT NULL,
    requested_at    TEXT NOT NULL,
    completed_at    TEXT NOT NULL,
    status          TEXT NOT NULL
);
"""

_CREATE_SETTLEMENTS = """
CREATE TABLE IF NOT EXISTS arb_settlements (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id             TEXT NOT NULL,
    resolution_market_id TEXT NOT NULL,
    pnl_realized         REAL NOT NULL,
    timestamp            TEXT NOT NULL
);
"""

_CREATE_RUNTIME_STATE = """
CREATE TABLE IF NOT EXISTS arb_runtime_state (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    cash                REAL NOT NULL,
    contributed_capital REAL NOT NULL,
    realized_pnl        REAL NOT NULL,
    fees_paid           REAL NOT NULL,
    rebates_earned      REAL NOT NULL,
    updated_at          TEXT NOT NULL
);
"""

_CREATE_COOLDOWNS = """
CREATE TABLE IF NOT EXISTS arb_cooldowns (
    cooldown_key TEXT PRIMARY KEY,
    expires_at   TEXT NOT NULL
);
"""

_CREATE_TRADER_FOLLOW_SEEN = """
CREATE TABLE IF NOT EXISTS arb_trader_follow_seen (
    tx_hash       TEXT PRIMARY KEY,
    leader_wallet TEXT,
    token_id      TEXT,
    side          TEXT,
    created_at    TEXT NOT NULL
);
"""


class ArbRepository:
    def __init__(self, path: str = _DB_PATH) -> None:
        self._path = os.path.abspath(path)
        os.makedirs(os.path.dirname(self._path), exist_ok=True)

    async def init(self) -> None:
        async with _connect(self._path) as db:
            for sql in (
                _CREATE_EVENTS,
                _CREATE_MARKETS,
                _CREATE_BOOKS,
                _CREATE_OPPORTUNITIES,
                _CREATE_BASKETS,
                _CREATE_ORDERS,
                _CREATE_FILLS,
                _CREATE_POSITIONS,
                _CREATE_CONVERSIONS,
                _CREATE_SETTLEMENTS,
                _CREATE_RUNTIME_STATE,
                _CREATE_COOLDOWNS,
                _CREATE_TRADER_FOLLOW_SEEN,
            ):
                await db.execute(sql)
            await db.commit()
        logger.info("arb_repository.initialized", path=self._path)

    async def trader_follow_seen(self, tx_hash: str) -> bool:
        async with _connect(self._path) as db:
            cur = await db.execute(
                "SELECT 1 FROM arb_trader_follow_seen WHERE tx_hash = ? LIMIT 1",
                (tx_hash,),
            )
            row = await cur.fetchone()
            return row is not None

    async def record_trader_follow_seen(
        self,
        *,
        tx_hash: str,
        leader_wallet: str,
        token_id: str,
        side: str,
    ) -> None:
        async with _connect(self._path) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO arb_trader_follow_seen
                (tx_hash, leader_wallet, token_id, side, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    tx_hash,
                    leader_wallet,
                    token_id,
                    side,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            await db.commit()

    async def upsert_event(self, event: ArbEvent) -> None:
        sql = """
        INSERT INTO arb_events (
            id, title, category, neg_risk, enable_neg_risk, neg_risk_augmented,
            status, liquidity, rules_text, end_time, raw_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title=excluded.title,
            category=excluded.category,
            neg_risk=excluded.neg_risk,
            enable_neg_risk=excluded.enable_neg_risk,
            neg_risk_augmented=excluded.neg_risk_augmented,
            status=excluded.status,
            liquidity=excluded.liquidity,
            rules_text=excluded.rules_text,
            end_time=excluded.end_time,
            raw_json=excluded.raw_json,
            updated_at=excluded.updated_at
        """
        updated_at = event.raw.get("updatedAt", "") or event.raw.get("updated_at", "") or event.end_time or "unknown"
        async with _connect(self._path) as db:
            await db.execute(
                sql,
                (
                    event.event_id,
                    event.title,
                    event.category,
                    1 if event.neg_risk else 0,
                    1 if event.enable_neg_risk else 0,
                    1 if event.neg_risk_augmented else 0,
                    event.status,
                    float(event.liquidity),
                    event.rules_text,
                    event.end_time,
                    json.dumps(event.raw, default=str),
                    updated_at,
                ),
            )
            for market in event.markets:
                await db.execute(
                    """
                    INSERT INTO arb_markets (
                        id, event_id, question, outcome_name, yes_token_id, no_token_id,
                        current_yes_price, current_no_price, liquidity, tick_size,
                        fees_enabled, status, raw_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        event_id=excluded.event_id,
                        question=excluded.question,
                        outcome_name=excluded.outcome_name,
                        yes_token_id=excluded.yes_token_id,
                        no_token_id=excluded.no_token_id,
                        current_yes_price=excluded.current_yes_price,
                        current_no_price=excluded.current_no_price,
                        liquidity=excluded.liquidity,
                        tick_size=excluded.tick_size,
                        fees_enabled=excluded.fees_enabled,
                        status=excluded.status,
                        raw_json=excluded.raw_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        market.market_id,
                        market.event_id,
                        market.question,
                        market.outcome_name,
                        market.yes_token_id,
                        market.no_token_id,
                        float(market.current_yes_price),
                        float(market.current_no_price),
                        float(market.liquidity),
                        float(market.tick_size),
                        1 if market.fees_enabled else 0,
                        market.status,
                        json.dumps(market.raw, default=str),
                        updated_at,
                    ),
                )
            await db.commit()

    async def upsert_events_batch(self, events: list[ArbEvent]) -> None:
        """Persist universe snapshot with one SQLite transaction (avoids per-event connect/commit)."""
        if not events:
            return
        sql_event = """
        INSERT INTO arb_events (
            id, title, category, neg_risk, enable_neg_risk, neg_risk_augmented,
            status, liquidity, rules_text, end_time, raw_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title=excluded.title,
            category=excluded.category,
            neg_risk=excluded.neg_risk,
            enable_neg_risk=excluded.enable_neg_risk,
            neg_risk_augmented=excluded.neg_risk_augmented,
            status=excluded.status,
            liquidity=excluded.liquidity,
            rules_text=excluded.rules_text,
            end_time=excluded.end_time,
            raw_json=excluded.raw_json,
            updated_at=excluded.updated_at
        """
        sql_market = """
                    INSERT INTO arb_markets (
                        id, event_id, question, outcome_name, yes_token_id, no_token_id,
                        current_yes_price, current_no_price, liquidity, tick_size,
                        fees_enabled, status, raw_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        event_id=excluded.event_id,
                        question=excluded.question,
                        outcome_name=excluded.outcome_name,
                        yes_token_id=excluded.yes_token_id,
                        no_token_id=excluded.no_token_id,
                        current_yes_price=excluded.current_yes_price,
                        current_no_price=excluded.current_no_price,
                        liquidity=excluded.liquidity,
                        tick_size=excluded.tick_size,
                        fees_enabled=excluded.fees_enabled,
                        status=excluded.status,
                        raw_json=excluded.raw_json,
                        updated_at=excluded.updated_at
                    """
        async with _connect(self._path) as db:
            for event in events:
                updated_at = (
                    event.raw.get("updatedAt", "")
                    or event.raw.get("updated_at", "")
                    or event.end_time
                    or "unknown"
                )
                await db.execute(
                    sql_event,
                    (
                        event.event_id,
                        event.title,
                        event.category,
                        1 if event.neg_risk else 0,
                        1 if event.enable_neg_risk else 0,
                        1 if event.neg_risk_augmented else 0,
                        event.status,
                        float(event.liquidity),
                        event.rules_text,
                        event.end_time,
                        json.dumps(event.raw, default=str),
                        updated_at,
                    ),
                )
                for market in event.markets:
                    await db.execute(
                        sql_market,
                        (
                            market.market_id,
                            market.event_id,
                            market.question,
                            market.outcome_name,
                            market.yes_token_id,
                            market.no_token_id,
                            float(market.current_yes_price),
                            float(market.current_no_price),
                            float(market.liquidity),
                            float(market.tick_size),
                            1 if market.fees_enabled else 0,
                            market.status,
                            json.dumps(market.raw, default=str),
                            updated_at,
                        ),
                    )
            await db.commit()

    async def record_book(self, book: TokenBook) -> None:
        async with _connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO arb_books (
                    token_id, timestamp, best_bid, best_ask, mid, spread,
                    fees_enabled, tick_size, source, depth_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    book.token_id,
                    book.timestamp.isoformat(),
                    float(book.best_bid),
                    float(book.best_ask),
                    float(book.mid),
                    float(book.spread),
                    1 if book.fees_enabled else 0,
                    float(book.tick_size),
                    book.source,
                    json.dumps(book.as_dict(), default=str),
                ),
            )
            await db.commit()

    async def record_opportunity(self, opportunity: ArbOpportunity, decision: str | None = None, reason: str = "") -> None:
        payload = opportunity.as_dict()
        payload["decision"] = decision or payload["decision"]
        payload["reason"] = reason or payload["reason"]
        async with _connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO arb_opportunities (
                    id, strategy_type, event_id, event_title, gross_edge_bps,
                    net_edge_bps, capital_required, expected_profit, decision,
                    reason, requires_conversion, settle_on_resolution, created_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    decision=excluded.decision,
                    reason=excluded.reason,
                    payload_json=excluded.payload_json
                """,
                (
                    opportunity.opportunity_id,
                    opportunity.strategy_type,
                    opportunity.event_id,
                    opportunity.event_title,
                    float(opportunity.gross_edge_bps),
                    float(opportunity.net_edge_bps),
                    float(opportunity.capital_required),
                    float(opportunity.expected_profit),
                    decision or opportunity.decision,
                    reason or opportunity.reason,
                    1 if opportunity.requires_conversion else 0,
                    1 if opportunity.settle_on_resolution else 0,
                    opportunity.created_at.isoformat(),
                    json.dumps(payload, default=str),
                ),
            )
            await db.commit()

    async def create_basket(self, basket: BasketRecord) -> None:
        await self._upsert_basket(basket)

    async def update_basket(self, basket: BasketRecord) -> None:
        await self._upsert_basket(basket)

    async def _upsert_basket(self, basket: BasketRecord) -> None:
        async with _connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO arb_baskets (
                    id, opportunity_id, event_id, strategy_type, status, capital_reserved,
                    target_net_edge_bps, realized_net_pnl, notes, created_at, closed_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    capital_reserved=excluded.capital_reserved,
                    target_net_edge_bps=excluded.target_net_edge_bps,
                    realized_net_pnl=excluded.realized_net_pnl,
                    notes=excluded.notes,
                    closed_at=excluded.closed_at,
                    payload_json=excluded.payload_json
                """,
                (
                    basket.basket_id,
                    basket.opportunity_id,
                    basket.event_id,
                    basket.strategy_type,
                    basket.status,
                    float(basket.capital_reserved),
                    float(basket.target_net_edge_bps),
                    float(basket.realized_net_pnl),
                    basket.notes,
                    basket.created_at.isoformat(),
                    basket.closed_at.isoformat() if basket.closed_at else None,
                    json.dumps(basket.as_dict(), default=str),
                ),
            )
            await db.commit()

    async def record_order(self, order: OrderRecord) -> None:
        async with _connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO arb_orders (
                    id, basket_id, opportunity_id, token_id, market_id, side, price, size,
                    order_type, maker_or_taker, status, filled_size, average_price,
                    fees_enabled, reason, created_at, updated_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    filled_size=excluded.filled_size,
                    average_price=excluded.average_price,
                    reason=excluded.reason,
                    updated_at=excluded.updated_at,
                    payload_json=excluded.payload_json
                """,
                (
                    order.order_id,
                    order.basket_id,
                    order.opportunity_id,
                    order.token_id,
                    order.market_id,
                    order.side,
                    float(order.price),
                    float(order.size),
                    order.order_type,
                    order.maker_or_taker,
                    order.status,
                    float(order.filled_size),
                    float(order.average_price),
                    1 if order.fees_enabled else 0,
                    order.reason,
                    order.created_at.isoformat(),
                    order.updated_at.isoformat(),
                    json.dumps(order.as_dict(), default=str),
                ),
            )
            await db.commit()

    async def record_fill(self, fill: FillRecord) -> None:
        async with _connect(self._path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO arb_fills (
                    id, order_id, token_id, market_id, event_id, side, price, size,
                    fee_paid, rebate_earned, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fill.fill_id,
                    fill.order_id,
                    fill.token_id,
                    fill.market_id,
                    fill.event_id,
                    fill.side,
                    float(fill.price),
                    float(fill.size),
                    float(fill.fee_paid),
                    float(fill.rebate_earned),
                    fill.timestamp.isoformat(),
                ),
            )
            await db.commit()

    async def replace_positions(self, positions: list[PositionRecord]) -> None:
        async with _connect(self._path) as db:
            await db.execute("DELETE FROM arb_positions")
            for position in positions:
                await db.execute(
                    """
                    INSERT INTO arb_positions (
                        token_id, market_id, event_id, outcome_name,
                        contract_side, size, avg_price, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        position.token_id,
                        position.market_id,
                        position.event_id,
                        position.outcome_name,
                        position.contract_side,
                        float(position.size),
                        float(position.avg_price),
                        position.updated_at.isoformat(),
                    ),
                )
            await db.commit()

    async def load_positions(self) -> list[PositionRecord]:
        rows = await self._fetch_all(
            "SELECT token_id, market_id, event_id, outcome_name, contract_side, size, avg_price, updated_at "
            "FROM arb_positions ORDER BY event_id, market_id"
        )
        return [
            PositionRecord(
                token_id=row["token_id"],
                market_id=row["market_id"],
                event_id=row["event_id"],
                outcome_name=row["outcome_name"],
                contract_side=row["contract_side"],
                size=float(row["size"]),
                avg_price=float(row["avg_price"]),
            )
            for row in rows
        ]

    async def record_conversion(
        self,
        basket_id: str,
        event_id: str,
        input_market_id: str,
        input_token_id: str,
        outputs: list[dict[str, Any]],
        size: float,
        requested_at: str,
        completed_at: str,
    ) -> None:
        async with _connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO arb_conversions (
                    basket_id, event_id, input_market_id, input_token_id, outputs_json,
                    size, requested_at, completed_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    basket_id,
                    event_id,
                    input_market_id,
                    input_token_id,
                    json.dumps(outputs, default=str),
                    float(size),
                    requested_at,
                    completed_at,
                    "completed",
                ),
            )
            await db.commit()

    async def record_settlement(self, event_id: str, resolution_market_id: str, pnl_realized: float, timestamp: str) -> None:
        async with _connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO arb_settlements (event_id, resolution_market_id, pnl_realized, timestamp)
                VALUES (?, ?, ?, ?)
                """,
                (event_id, resolution_market_id, float(pnl_realized), timestamp),
            )
            await db.commit()

    async def save_runtime_state(
        self,
        *,
        cash: float,
        contributed_capital: float,
        realized_pnl: float,
        fees_paid: float,
        rebates_earned: float,
        updated_at: str,
    ) -> None:
        async with _connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO arb_runtime_state (
                    id, cash, contributed_capital, realized_pnl, fees_paid, rebates_earned, updated_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    cash=excluded.cash,
                    contributed_capital=excluded.contributed_capital,
                    realized_pnl=excluded.realized_pnl,
                    fees_paid=excluded.fees_paid,
                    rebates_earned=excluded.rebates_earned,
                    updated_at=excluded.updated_at
                """,
                (
                    float(cash),
                    float(contributed_capital),
                    float(realized_pnl),
                    float(fees_paid),
                    float(rebates_earned),
                    updated_at,
                ),
            )
            await db.commit()

    async def load_runtime_state(self) -> dict[str, Any] | None:
        rows = await self._fetch_all(
            "SELECT cash, contributed_capital, realized_pnl, fees_paid, rebates_earned, updated_at "
            "FROM arb_runtime_state WHERE id = 1"
        )
        return rows[0] if rows else None

    async def save_cooldowns(self, cooldowns: dict[str, datetime]) -> None:
        async with _connect(self._path) as db:
            await db.execute("DELETE FROM arb_cooldowns")
            for key, expires_at in cooldowns.items():
                await db.execute(
                    "INSERT INTO arb_cooldowns (cooldown_key, expires_at) VALUES (?, ?)",
                    (key, expires_at.isoformat()),
                )
            await db.commit()

    async def load_cooldowns(self) -> dict[str, datetime]:
        from datetime import datetime, timezone

        rows = await self._fetch_all(
            "SELECT cooldown_key, expires_at FROM arb_cooldowns"
        )
        now = datetime.now(timezone.utc)
        result: dict[str, datetime] = {}
        for row in rows:
            try:
                dt = datetime.fromisoformat(row["expires_at"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt > now:
                    result[row["cooldown_key"]] = dt
            except Exception:
                pass
        return result

    async def _fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        async with _connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def list_events(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self._fetch_all("SELECT * FROM arb_events ORDER BY updated_at DESC LIMIT ?", (limit,))

    async def list_opportunities(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self._fetch_all("SELECT * FROM arb_opportunities ORDER BY created_at DESC LIMIT ?", (limit,))

    async def list_orders(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self._fetch_all("SELECT * FROM arb_orders ORDER BY updated_at DESC LIMIT ?", (limit,))
        result: list[dict[str, Any]] = []
        for row in rows:
            merged = dict(row)
            raw = row.get("payload_json")
            if raw:
                try:
                    payload = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    payload = None
                if isinstance(payload, dict):
                    merged.update(payload)
            result.append(merged)
        return result

    async def list_positions(self) -> list[dict[str, Any]]:
        return await self._fetch_all("SELECT * FROM arb_positions ORDER BY event_id, market_id")

    async def list_baskets(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self._fetch_all("SELECT * FROM arb_baskets ORDER BY created_at DESC LIMIT ?", (limit,))
        result: list[dict[str, Any]] = []
        for row in rows:
            payload_raw = row.get("payload_json")
            if not payload_raw:
                result.append(row)
                continue
            try:
                payload = json.loads(payload_raw)
            except (TypeError, json.JSONDecodeError):
                result.append(row)
                continue
            merged = dict(row)
            merged.update(payload if isinstance(payload, dict) else {})
            result.append(merged)
        return result

    async def load_active_baskets(self) -> list[BasketRecord]:
        rows = await self._fetch_all(
            "SELECT payload_json FROM arb_baskets WHERE status IN ('OPEN', 'EXECUTING') ORDER BY created_at DESC"
        )
        baskets: list[BasketRecord] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            baskets.append(
                BasketRecord(
                    basket_id=payload["basket_id"],
                    opportunity_id=payload["opportunity_id"],
                    event_id=payload["event_id"],
                    strategy_type=payload["strategy_type"],
                    status=payload["status"],
                    capital_reserved=float(payload["capital_reserved"]),
                    target_net_edge_bps=float(payload["target_net_edge_bps"]),
                    realized_net_pnl=float(payload.get("realized_net_pnl", 0.0)),
                    fill_slippage_bps=float(payload.get("fill_slippage_bps", 0.0)),
                    notes=payload.get("notes", ""),
                    created_at=datetime.fromisoformat(payload["created_at"]),
                    closed_at=datetime.fromisoformat(payload["closed_at"]) if payload.get("closed_at") else None,
                )
            )
        return baskets
