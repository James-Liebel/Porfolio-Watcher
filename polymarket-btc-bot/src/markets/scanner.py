"""Polls Gamma API every 60 s for active 5-minute BTC/ETH/SOL/XRP markets."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional

import aiohttp
import structlog

logger = structlog.get_logger(__name__)

_GAMMA_URL = "https://gamma-api.polymarket.com/markets"
_POLL_INTERVAL = 60  # seconds
_MIN_SECONDS_FROM_NOW = 30
_MAX_SECONDS_FROM_NOW = 600  # 10 minutes

# Multi-asset keyword → canonical asset symbol
_ASSET_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("bitcoin", "btc"), "BTC"),
    (("ethereum", "eth"), "ETH"),
    (("solana", "sol"), "SOL"),
    (("xrp", "ripple"), "XRP"),
]


def _detect_asset(title: str) -> Optional[str]:
    """Return canonical asset symbol or None if not a recognised asset."""
    lower = title.lower()
    for keywords, symbol in _ASSET_KEYWORDS:
        if any(kw in lower for kw in keywords):
            return symbol
    return None


@dataclass
class ActiveMarket:
    market_id: str
    condition_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    start_time: datetime
    end_time: datetime
    current_yes_price: Decimal
    current_no_price: Decimal
    asset: str  # "BTC", "ETH", "SOL", or "XRP"


class MarketScanner:
    """
    Continuously polls Gamma API for active short-window prediction markets
    across BTC, ETH, SOL, and XRP.  Assets disabled in config are skipped.
    Exposes `active_markets` dict keyed by market_id.
    """

    def __init__(self, config=None) -> None:
        self.active_markets: Dict[str, ActiveMarket] = {}
        self._session: Optional[aiohttp.ClientSession] = None
        self._config = config  # Settings — optional; used for per-asset on/off

    def _asset_enabled(self, asset: str) -> bool:
        if self._config is None:
            return True
        return {
            "BTC": self._config.trade_btc,
            "ETH": self._config.trade_eth,
            "SOL": self._config.trade_sol,
            "XRP": self._config.trade_xrp,
        }.get(asset, False)

    async def run(self) -> None:
        async with aiohttp.ClientSession() as session:
            self._session = session
            while True:
                try:
                    await self._scan()
                except Exception as exc:
                    logger.error("scanner.scan_error", error=str(exc))
                await asyncio.sleep(_POLL_INTERVAL)

    async def _scan(self) -> None:
        params = {
            "tag": "crypto",
            "active": "true",
            "limit": 50,
        }
        assert self._session is not None
        async with self._session.get(
            _GAMMA_URL,
            params=params,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                logger.warning("scanner.bad_response", status=resp.status)
                return
            data = await resp.json()

        markets = data if isinstance(data, list) else data.get("markets", [])
        found: Dict[str, ActiveMarket] = {}
        now = datetime.now(timezone.utc)

        for m in markets:
            try:
                question: str = m.get("question", "") or m.get("title", "") or ""
                asset = _detect_asset(question)
                if asset is None:
                    continue
                if not self._asset_enabled(asset):
                    continue

                end_str: Optional[str] = m.get("endDateIso") or m.get("end_date_iso")
                if not end_str:
                    continue
                end_time = _parse_dt(end_str)

                seconds_left = (end_time - now).total_seconds()
                if not (_MIN_SECONDS_FROM_NOW <= seconds_left <= _MAX_SECONDS_FROM_NOW):
                    continue

                start_str: Optional[str] = m.get("startDateIso") or m.get("start_date_iso")
                start_time = _parse_dt(start_str) if start_str else now

                tokens: List[dict] = m.get("tokens", []) or m.get("clobTokenIds", [])
                yes_token_id = ""
                no_token_id = ""
                yes_price = Decimal("0.5")
                no_price = Decimal("0.5")

                for tok in tokens:
                    outcome = (tok.get("outcome", "") or "").upper()
                    if outcome == "YES":
                        yes_token_id = tok.get("token_id", tok.get("tokenId", ""))
                        yes_price = Decimal(str(tok.get("price", 0.5)))
                    elif outcome == "NO":
                        no_token_id = tok.get("token_id", tok.get("tokenId", ""))
                        no_price = Decimal(str(tok.get("price", 0.5)))

                if not yes_token_id or not no_token_id:
                    continue

                market_id: str = str(m.get("id", m.get("market_id", "")))
                condition_id: str = str(m.get("conditionId", m.get("condition_id", "")))

                am = ActiveMarket(
                    market_id=market_id,
                    condition_id=condition_id,
                    question=question,
                    yes_token_id=yes_token_id,
                    no_token_id=no_token_id,
                    start_time=start_time,
                    end_time=end_time,
                    current_yes_price=yes_price,
                    current_no_price=no_price,
                    asset=asset,
                )
                found[market_id] = am

            except Exception as exc:
                logger.warning(
                    "scanner.market_parse_error",
                    error=str(exc),
                    market=m.get("id"),
                )

        self.active_markets = found
        by_asset = {}
        for m in found.values():
            by_asset[m.asset] = by_asset.get(m.asset, 0) + 1
        logger.info("scanner.scan_complete", found=len(found), by_asset=by_asset)


def _parse_dt(s: str) -> datetime:
    from datetime import timezone as tz
    from dateutil import parser as dtparser
    dt = dtparser.parse(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz.utc)
    return dt
