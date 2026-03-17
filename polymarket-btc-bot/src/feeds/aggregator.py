"""
Aggregates MultiAssetFeed (primary) and CoinbaseFeed (BTC cross-check).
Emits a PriceUpdate every 500 ms with per-asset prices.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import structlog

from .coinbase import CoinbaseFeed
from .multi_asset import MultiAssetFeed

logger = structlog.get_logger(__name__)

_DIVERGENCE_THRESHOLD = Decimal("0.003")  # 0.3%
_POLL_INTERVAL = 0.5  # seconds
_STALE_LOG_INTERVAL = 30.0  # seconds between stale-feed log lines


@dataclass
class PriceUpdate:
    timestamp: datetime
    # Per-asset prices from the primary Binance combined feed
    btc_price: Optional[Decimal]
    eth_price: Optional[Decimal]
    sol_price: Optional[Decimal]
    xrp_price: Optional[Decimal]
    # Coinbase BTC price (used only for cross-check / divergence warning)
    coinbase_btc_price: Optional[Decimal]
    # Legacy field kept so existing code using .median_price still works
    median_price: Optional[Decimal]
    feed_count: int


class PriceAggregator:
    """
    Every 500 ms pulls prices from the MultiAssetFeed (Binance combined stream)
    and CoinbaseFeed (BTC only), then publishes a PriceUpdate.
    Callers read `latest` for the most recent snapshot, or call get_price(asset).
    """

    def __init__(self, multi_asset: MultiAssetFeed, coinbase: CoinbaseFeed) -> None:
        self._multi = multi_asset
        self._coinbase = coinbase
        self.latest: Optional[PriceUpdate] = None
        self._last_stale_log: float = 0.0

    def get_price(self, asset: str) -> Optional[Decimal]:
        """Convenience accessor for the trading loop."""
        if self.latest is None:
            return None
        return {
            "BTC": self.latest.btc_price,
            "ETH": self.latest.eth_price,
            "SOL": self.latest.sol_price,
            "XRP": self.latest.xrp_price,
        }.get(asset.upper())

    async def run(self) -> None:
        while True:
            try:
                await self._tick()
            except Exception as exc:
                logger.error("aggregator.tick_error", error=str(exc))
            await asyncio.sleep(_POLL_INTERVAL)

    async def _tick(self) -> None:
        btc_p, eth_p, sol_p, xrp_p, cb_p = await asyncio.gather(
            self._multi.get_price("BTC"),
            self._multi.get_price("ETH"),
            self._multi.get_price("SOL"),
            self._multi.get_price("XRP"),
            self._coinbase.get_price(),
        )

        # BTC divergence check between Binance and Coinbase feeds
        if btc_p is not None and cb_p is not None:
            divergence = abs(btc_p - cb_p) / ((btc_p + cb_p) / 2)
            if divergence > _DIVERGENCE_THRESHOLD:
                logger.warning(
                    "aggregator.btc_feed_divergence",
                    binance=str(btc_p),
                    coinbase=str(cb_p),
                    divergence_pct=f"{float(divergence) * 100:.3f}%",
                )

        # Primary feed availability
        primary_prices = [p for p in (btc_p, eth_p, sol_p, xrp_p) if p is not None]
        feed_count = len(primary_prices)

        if not self._multi.all_connected:
            now = time.monotonic()
            if now - self._last_stale_log >= _STALE_LOG_INTERVAL:
                logger.warning("aggregator.multi_asset_feed_stale")
                self._last_stale_log = now

        self.latest = PriceUpdate(
            timestamp=datetime.now(timezone.utc),
            btc_price=btc_p,
            eth_price=eth_p,
            sol_price=sol_p,
            xrp_price=xrp_p,
            coinbase_btc_price=cb_p,
            # median_price kept for backward compatibility — uses BTC
            median_price=btc_p,
            feed_count=feed_count,
        )
