from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import structlog

from ..config import Settings
from .models import ArbEvent, OutcomeMarket, PriceLevel, TokenBook

logger = structlog.get_logger(__name__)

try:
    from py_clob_client.client import ClobClient
except Exception:  # pragma: no cover
    ClobClient = None  # type: ignore[assignment]


def _is_missing_clob_orderbook(exc: BaseException) -> bool:
    """Gamma can reference tokens with no live CLOB book (resolved/closed markets, lag)."""
    msg = str(exc).lower()
    if "no orderbook exists" in msg:
        return True
    return "404" in msg and "orderbook" in msg


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in ("", None):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


class ClobMarketDataService:
    def __init__(self, config: Settings, client: Any | None = None) -> None:
        self._config = config
        self._client = client
        self._fetch_sem = asyncio.Semaphore(max(1, int(config.clob_book_fetch_concurrency)))
        if self._client is None and ClobClient is not None:
            try:
                self._client = ClobClient(
                    host=self._config.clob_host,
                    chain_id=137,
                    signature_type=2,
                )
            except Exception as exc:  # pragma: no cover
                logger.warning("market_data.clob_unavailable", error=str(exc))
                self._client = None

    async def refresh(
        self, events: list[ArbEvent], on_progress: Any | None = None
    ) -> dict[str, TokenBook]:
        markets: list[tuple[OutcomeMarket, str, str]] = []
        for event in events:
            for market in event.markets:
                markets.append((market, market.yes_token_id, "YES"))
                markets.append((market, market.no_token_id, "NO"))

        books: dict[str, TokenBook] = {}
        if self._client is None:
            for market, token_id, contract_side in markets:
                books[token_id] = self._synthetic_book(market, token_id, contract_side)
            return books

        async def _gated_fetch(
            market: OutcomeMarket, token_id: str, contract_side: str
        ) -> TokenBook | Exception:
            async with self._fetch_sem:
                try:
                    return await self._fetch_book(market, token_id, contract_side)
                except Exception as exc:
                    return exc

        tasks = [
            _gated_fetch(market, token_id, contract_side)
            for market, token_id, contract_side in markets
        ]
        completed = 0
        total = len(tasks)
        if total > 0:
            logger.info("market_data.fetch_start", port=self._config.control_api_port, total=total)

        for coro in asyncio.as_completed(tasks):
            book = await coro
            completed += 1
            pct = round(completed / total * 100, 1)
            if on_progress:
                on_progress(pct)

            if completed % 50 == 0 or completed == total:
                logger.info(
                    "market_data.progress",
                    port=self._config.control_api_port,
                    done=completed,
                    total=total,
                    pct=pct,
                )

            if isinstance(book, Exception):
                # Don't log every single error if we're doing hundreds, just a warning
                # but we still count it as 'done' for the progress bar.
                continue
            books[book.token_id] = book

        for market, token_id, contract_side in markets:
            if token_id not in books:
                books[token_id] = self._synthetic_book(market, token_id, contract_side)
        return books

    async def _fetch_book(self, market: OutcomeMarket, token_id: str, contract_side: str) -> TokenBook:
        attempts = 1 + max(0, int(self._config.clob_book_retry_attempts))
        last_exc: Exception | None = None
        raw_book = None
        for attempt in range(attempts):
            try:
                raw_book = await asyncio.to_thread(self._client.get_order_book, token_id)
                break
            except Exception as exc:
                last_exc = exc
                if attempt + 1 >= attempts:
                    if _is_missing_clob_orderbook(exc):
                        logger.debug(
                            "market_data.book_missing",
                            token_id=token_id,
                            attempts=attempts,
                            error=str(exc),
                        )
                    else:
                        logger.warning(
                            "market_data.book_error",
                            token_id=token_id,
                            attempts=attempts,
                            error=str(exc),
                        )
                    raise
                delay = float(self._config.clob_book_retry_delay_seconds) * (attempt + 1)
                await asyncio.sleep(delay)
        if raw_book is None:
            assert last_exc is not None
            raise last_exc
        bids = self._parse_levels(getattr(raw_book, "bids", None))
        asks = self._parse_levels(getattr(raw_book, "asks", None))
        if not bids and not asks:
            return self._synthetic_book(market, token_id, contract_side)

        # py_clob_client may return bids ascending or descending depending on version.
        # Normalise: bids descending (best = highest price first), asks ascending (best = lowest price first).
        bids.sort(key=lambda l: l.price, reverse=True)
        asks.sort(key=lambda l: l.price)

        best_bid = bids[0].price if bids else 0.0
        best_ask = asks[0].price if asks else 0.0
        if best_bid <= 0 or best_ask <= 0:
            synthetic = self._synthetic_book(market, token_id, contract_side)
            best_bid = best_bid or synthetic.best_bid
            best_ask = best_ask or synthetic.best_ask
            bids = bids or synthetic.bids
            asks = asks or synthetic.asks

        return TokenBook(
            token_id=token_id,
            timestamp=datetime.now(timezone.utc),
            best_bid=best_bid,
            best_ask=best_ask,
            bids=bids,
            asks=asks,
            fees_enabled=market.fees_enabled,
            tick_size=market.tick_size,
            source="clob",
        )

    def _parse_levels(self, levels: Any) -> list[PriceLevel]:
        parsed: list[PriceLevel] = []
        for level in levels or []:
            price = _coerce_float(getattr(level, "price", None) or (level.get("price") if isinstance(level, dict) else None))
            size = _coerce_float(getattr(level, "size", None) or (level.get("size") if isinstance(level, dict) else None))
            if price > 0 and size > 0:
                parsed.append(PriceLevel(price=price, size=size))
        return parsed

    def _synthetic_book(self, market: OutcomeMarket, token_id: str, contract_side: str) -> TokenBook:
        center = market.current_yes_price if contract_side == "YES" else market.current_no_price
        center = min(max(center or 0.5, 0.01), 0.99)
        spread = max(market.tick_size, 0.01)
        depth = max(market.liquidity / max(center, 0.05) / 50.0, 25.0)
        bid = max(center - spread / 2, 0.001)
        ask = min(center + spread / 2, 0.999)
        return TokenBook(
            token_id=token_id,
            timestamp=datetime.now(timezone.utc),
            best_bid=bid,
            best_ask=ask,
            bids=[PriceLevel(price=bid, size=depth)],
            asks=[PriceLevel(price=ask, size=depth)],
            fees_enabled=market.fees_enabled,
            tick_size=market.tick_size,
            source="synthetic",
        )
