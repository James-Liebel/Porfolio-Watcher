"""
Binance combined-stream feed for the supported crypto asset set.
Uses a single WebSocket connection with the multi-stream endpoint.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from decimal import Decimal
from typing import Deque, Dict, NamedTuple, Optional

import aiohttp
import structlog
import websockets

logger = structlog.get_logger(__name__)

_SUPPORTED_ASSETS = ("BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "AVAX", "LINK")
_STREAMS = "/".join(f"{a.lower()}usdt@trade" for a in _SUPPORTED_ASSETS)
_WS_URL = f"wss://stream.binance.com/stream?streams={_STREAMS}"

_VWAP_WINDOW_SECONDS = 3
_STALE_THRESHOLD_SECONDS = 5
_BACKOFF_BASE = 1
_BACKOFF_MAX = 60
_REST_URL = "https://api.binance.com/api/v3/ticker/price?symbol={symbol}USDT"


class _Trade(NamedTuple):
    price: Decimal
    qty: Decimal
    ts: float


class MultiAssetFeed:
    """
    Single Binance combined-stream WebSocket for the supported asset set.
    Maintains a 3-second rolling VWAP per asset.
    Auto-reconnects with exponential backoff.
    """

    def __init__(self) -> None:
        self._trades: Dict[str, Deque[_Trade]] = {
            asset: deque() for asset in _SUPPORTED_ASSETS
        }
        self._vwap: Dict[str, Optional[Decimal]] = {
            asset: None for asset in _SUPPORTED_ASSETS
        }
        self._last_msg_ts: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def all_connected(self) -> bool:
        return (time.monotonic() - self._last_msg_ts) < _STALE_THRESHOLD_SECONDS

    async def get_price(self, asset: str) -> Optional[Decimal]:
        async with self._lock:
            return self._vwap.get(asset.upper())

    def _update_vwap(self, asset: str, price: Decimal, qty: Decimal) -> None:
        now = time.monotonic()
        buf = self._trades[asset]
        buf.append(_Trade(price, qty, now))
        cutoff = now - _VWAP_WINDOW_SECONDS
        while buf and buf[0].ts < cutoff:
            buf.popleft()

        total_vol = sum(t.qty for t in buf)
        if total_vol > 0:
            self._vwap[asset] = sum(t.price * t.qty for t in buf) / total_vol
        self._last_msg_ts = now

    async def run(self) -> None:
        """Run forever, reconnecting on failure with exponential backoff."""
        backoff = _BACKOFF_BASE
        while True:
            try:
                await self._connect()
                backoff = _BACKOFF_BASE
            except Exception as exc:
                logger.warning(
                    "multi_asset_feed.reconnecting",
                    error=str(exc),
                    backoff_seconds=backoff,
                )
                await self._rest_snapshot()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)

    async def _connect(self) -> None:
        logger.info("multi_asset_feed.connecting", url=_WS_URL)
        async with websockets.connect(_WS_URL, ping_interval=20, ping_timeout=30) as ws:
            logger.info("multi_asset_feed.connected", assets=list(_SUPPORTED_ASSETS))
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                    stream: str = msg.get("stream", "")
                    data = msg.get("data", {})
                    # stream name: "btcusdt@trade" → asset "BTC"
                    asset = stream.split("usdt")[0].upper()
                    if asset not in _SUPPORTED_ASSETS:
                        continue
                    price = Decimal(data["p"])
                    qty = Decimal(data["q"])
                    async with self._lock:
                        self._update_vwap(asset, price, qty)
                except (KeyError, ValueError) as exc:
                    logger.warning("multi_asset_feed.parse_error", error=str(exc))

    async def _rest_snapshot(self) -> None:
        """
        Safety fallback when WebSocket handshake fails in restricted networks.
        Keeps prices warm so paper-trading can still run.
        """
        try:
            async with aiohttp.ClientSession() as session:
                for asset in _SUPPORTED_ASSETS:
                    async with session.get(
                        _REST_URL.format(symbol=asset),
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()
                        price = Decimal(str(data["price"]))
                        async with self._lock:
                            self._update_vwap(asset, price, Decimal("1"))
            logger.info("multi_asset_feed.rest_snapshot_ok")
        except Exception as exc:
            logger.warning("multi_asset_feed.rest_snapshot_error", error=str(exc))
