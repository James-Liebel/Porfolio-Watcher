"""Binance BTC/USDT WebSocket trade feed with 3-second rolling VWAP."""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from decimal import Decimal
from typing import Deque, NamedTuple

import structlog
import websockets
from websockets.exceptions import ConnectionClosed

logger = structlog.get_logger(__name__)

_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@trade"
_VWAP_WINDOW_SECONDS = 3
_BACKOFF_BASE = 1
_BACKOFF_MAX = 60


class _Trade(NamedTuple):
    price: Decimal
    qty: Decimal
    ts: float  # monotonic seconds


class BinanceFeed:
    """
    Streams BTC/USDT trades from Binance and maintains a 3-second VWAP.
    Auto-reconnects with exponential backoff on any failure.
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
        """Run forever, reconnecting on failure."""
        backoff = _BACKOFF_BASE
        while True:
            try:
                await self._connect()
                backoff = _BACKOFF_BASE  # reset on clean run
            except Exception as exc:
                self._connected = False
                logger.warning(
                    "binance.reconnecting",
                    error=str(exc),
                    backoff_seconds=backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)

    async def _connect(self) -> None:
        logger.info("binance.connecting", url=_WS_URL)
        async with websockets.connect(_WS_URL, ping_interval=20, ping_timeout=30) as ws:
            self._connected = True
            logger.info("binance.connected")
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                    price = Decimal(msg["p"])
                    qty = Decimal(msg["q"])
                    async with self._lock:
                        self._update_vwap(price, qty)
                except (KeyError, ValueError) as exc:
                    logger.warning("binance.parse_error", error=str(exc))
