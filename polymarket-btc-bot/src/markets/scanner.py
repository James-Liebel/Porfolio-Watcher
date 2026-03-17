"""Polls Gamma API every 60 s for active 5-minute BTC/ETH/SOL/XRP markets.

Discovery uses two methods:
1. Tag-based: /markets?tag=crypto (and 5M, 5m) — often misses short-dated 5m windows.
2. Timestamp-based: /events/slug/{asset}-updown-5m-{interval_start_ts} — deterministic
   discovery of the current 5-minute window per asset (see handiko/Polymarket-Market-Finder).
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional

import aiohttp
import structlog

logger = structlog.get_logger(__name__)

_GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
_GAMMA_EVENTS_SLUG_URL = "https://gamma-api.polymarket.com/events/slug"
_POLL_INTERVAL = 60  # seconds
_MIN_SECONDS_FROM_NOW = 10   # allow 10s–15min window
_MAX_SECONDS_FROM_NOW = 900  # 15 minutes
_INTERVAL_5M_SECONDS = 5 * 60  # 300

# Slug prefix for timestamp-based 5m discovery (asset symbol -> slug prefix)
_ASSET_SLUG_PREFIX: Dict[str, str] = {
    "BTC": "btc",
    "ETH": "eth",
    "SOL": "sol",
    "XRP": "xrp",
}

# Multiple tags to try for tag-based discovery
_GAMMA_TAGS = ("crypto", "5M", "5m")

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
        assert self._session is not None
        now = datetime.now(timezone.utc)
        found: Dict[str, ActiveMarket] = {}

        # 1) Timestamp-based discovery: current 5m window per asset (deterministic)
        start_ts = (int(now.timestamp()) // _INTERVAL_5M_SECONDS) * _INTERVAL_5M_SECONDS
        for asset, prefix in _ASSET_SLUG_PREFIX.items():
            if not self._asset_enabled(asset):
                continue
            slug = f"{prefix}-updown-5m-{start_ts}"
            try:
                async with self._session.get(
                    f"{_GAMMA_EVENTS_SLUG_URL}/{slug}",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        continue
                    event = await resp.json()
            except Exception:
                continue
            event_markets = event.get("markets") or event.get("marketsData") or []
            if not event_markets:
                continue
            m = event_markets[0] if isinstance(event_markets[0], dict) else None
            if not m:
                continue
            # End time for 5m window is always start_ts + 300 (API may return date-only)
            end_time = datetime.fromtimestamp(
                start_ts + _INTERVAL_5M_SECONDS, tz=timezone.utc
            )
            seconds_left = (end_time - now).total_seconds()
            if not (_MIN_SECONDS_FROM_NOW <= seconds_left <= _MAX_SECONDS_FROM_NOW):
                continue
            # clobTokenIds: JSON string "[id1, id2]" — index 0 = Up/Yes, 1 = Down/No
            raw_tokens = m.get("clobTokenIds")
            token_ids: List[str] = []
            if isinstance(raw_tokens, str):
                try:
                    token_ids = json.loads(raw_tokens)
                except json.JSONDecodeError:
                    pass
            elif isinstance(raw_tokens, list):
                token_ids = [str(t) for t in raw_tokens]
            if len(token_ids) < 2:
                continue
            yes_token_id = token_ids[0]
            no_token_id = token_ids[1]
            outcome_prices = m.get("outcomePrices") or event.get("outcomePrices")
            if isinstance(outcome_prices, str):
                try:
                    outcome_prices = json.loads(outcome_prices)
                except json.JSONDecodeError:
                    outcome_prices = None
            yes_price = Decimal("0.5")
            no_price = Decimal("0.5")
            if isinstance(outcome_prices, (list, tuple)) and len(outcome_prices) >= 2:
                try:
                    yes_price = Decimal(str(outcome_prices[0]))
                    no_price = Decimal(str(outcome_prices[1]))
                except Exception:
                    pass
            question = (
                m.get("question")
                or m.get("title")
                or event.get("title")
                or f"{asset} Up or Down - 5 min"
            )
            market_id = str(m.get("id", m.get("market_id", "")))
            condition_id = str(m.get("conditionId", m.get("condition_id", "")))
            start_time = now  # optional; we don't have exact start from event
            if market_id:
                found[market_id] = ActiveMarket(
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

        # 2) Tag-based discovery: /markets?tag=crypto (etc.) — merge in any we don't have
        all_markets: Dict[str, dict] = {}
        for tag in _GAMMA_TAGS:
            params = {"tag": tag, "active": "true", "limit": 50}
            try:
                async with self._session.get(
                    _GAMMA_MARKETS_URL,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
            except Exception:
                continue
            raw = data if isinstance(data, list) else data.get("markets", [])
            for m in raw:
                mid = str(m.get("id", m.get("market_id", "")))
                if mid and mid not in all_markets:
                    all_markets[mid] = m
        markets = list(all_markets.values())

        for m in markets:
            market_id = str(m.get("id", m.get("market_id", "")))
            if not market_id or market_id in found:
                continue
            try:
                question = m.get("question", "") or m.get("title", "") or ""
                asset = _detect_asset(question)
                if asset is None or not self._asset_enabled(asset):
                    continue
                end_str = (
                    m.get("endDateIso") or m.get("end_date_iso") or m.get("endDate")
                )
                if not end_str:
                    continue
                end_time = _parse_dt(str(end_str))
                seconds_left = (end_time - now).total_seconds()
                if not (_MIN_SECONDS_FROM_NOW <= seconds_left <= _MAX_SECONDS_FROM_NOW):
                    continue
                start_str = m.get("startDateIso") or m.get("start_date_iso")
                start_time = _parse_dt(start_str) if start_str else now

                tokens_raw = m.get("tokens") or m.get("clobTokenIds")
                yes_token_id = ""
                no_token_id = ""
                yes_price = Decimal("0.5")
                no_price = Decimal("0.5")
                if isinstance(tokens_raw, list):
                    for tok in tokens_raw:
                        if not isinstance(tok, dict):
                            continue
                        outcome = (tok.get("outcome", "") or "").upper()
                        if outcome == "YES":
                            yes_token_id = tok.get("token_id", tok.get("tokenId", ""))
                            yes_price = Decimal(str(tok.get("price", 0.5)))
                        elif outcome == "NO":
                            no_token_id = tok.get("token_id", tok.get("tokenId", ""))
                            no_price = Decimal(str(tok.get("price", 0.5)))
                elif isinstance(tokens_raw, str):
                    try:
                        ids = json.loads(tokens_raw)
                        if len(ids) >= 2:
                            yes_token_id, no_token_id = str(ids[0]), str(ids[1])
                    except json.JSONDecodeError:
                        pass
                if not yes_token_id or not no_token_id:
                    continue
                condition_id = str(m.get("conditionId", m.get("condition_id", "")))
                found[market_id] = ActiveMarket(
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
        logger.info(
            "scanner.scan_complete",
            found=len(found),
            by_asset=by_asset,
            raw_markets=len(markets),
        )


def _parse_dt(s: str) -> datetime:
    from datetime import timezone as tz
    from dateutil import parser as dtparser
    dt = dtparser.parse(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz.utc)
    return dt
