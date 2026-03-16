"""Coinbase Advanced Trade WebSocket feed for BTC-USD with 3-second rolling VWAP."""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from decimal import Decimal
from typing import Deque, NamedTuple

import structlog
import websockets

logger = structlog.get_logger(__name__)

_WS_URL = "wss://advanced-trade-ws.coinbase.com"
_SUBSCRIBE_MSG = {
    "type": "subscribe",
    "product_ids": ["BTC-USD"],
    "channel": "market_trades",
}
_VWAP_WINDOW_SECONDS = 3
_BACKOFF_BASE = 1
_BACKOFF_MAX = 60


class _Trade(NamedTuple):
    price: Decimal
    qty: Decimal
    ts: float


class CoinbaseFeed:
    """
    Streams BTC-USD trades from Coinbase Advanced Trade WebSocket.
    Maintains a 3-second VWAP. Auto-reconnects with exponential backoff.
    """

    def __init__(self) -> None:
        self._trades: Deque[_Trade] = deque()
        self._vwap: Decimal | None = None
        self._connected: bool = False
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._connected

    async def get_price(self) -> Decimal | None:
        async with self._lock:
            return self._vwap

    def _update_vwap(self, price: Decimal, qty: Decimal) -> None:
        now = time.monotonic()
        self._trades.append(_Trade(price, qty, now))
        cutoff = now - _VWAP_WINDOW_SECONDS
        while self._trades and self._trades[0].ts < cutoff:
            self._trades.popleft()

        total_vol = sum(t.qty for t in self._trades)
        if total_vol > 0:
            self._vwap = sum(t.price * t.qty for t in self._trades) / total_vol

    async def run(self) -> None:
        backoff = _BACKOFF_BASE
        while True:
            try:
                await self._connect()
                backoff = _BACKOFF_BASE
            except Exception as exc:
                self._connected = False
                logger.warning(
                    "coinbase.reconnecting",
                    error=str(exc),
                    backoff_seconds=backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)

    async def _connect(self) -> None:
        logger.info("coinbase.connecting", url=_WS_URL)
        async with websockets.connect(_WS_URL, ping_interval=20, ping_timeout=30) as ws:
            await ws.send(json.dumps(_SUBSCRIBE_MSG))
            self._connected = True
            logger.info("coinbase.connected")
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                    msg_type = msg.get("channel")
                    if msg_type != "market_trades":
                        continue
                    events = msg.get("events", [])
                    for event in events:
                        for trade in event.get("trades", []):
                            price = Decimal(trade["price"])
                            qty = Decimal(trade["size"])
                            async with self._lock:
                                self._update_vwap(price, qty)
                except (KeyError, ValueError) as exc:
                    logger.warning("coinbase.parse_error", error=str(exc))
