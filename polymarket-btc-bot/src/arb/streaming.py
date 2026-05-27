"""Real-time order-book streaming over the Polymarket CLOB market WebSocket.

The REST poll path (``ClobMarketDataService``) re-fetches every book on a fixed
interval, so the scanner only ever sees the market as it looked seconds ago. By
then a streaming competitor has already taken the trade. ``ClobBookStream`` keeps
a continuously-updated in-memory book cache fed by the market channel's ``book``
(full snapshot) and ``price_change`` (incremental) messages, and fires a callback
the instant a tracked token's book changes so the engine can re-scan and execute
within milliseconds.

The class is deliberately split so the message handling is pure and unit-testable
without a socket: ``apply_raw`` mutates the cache and returns the set of changed
tokens; ``run`` is the thin network loop (connect → subscribe → read → reconnect).
A ``connector`` can be injected for tests to drive ``run`` without real I/O.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, AsyncContextManager, Awaitable, Callable, Iterable

import structlog

from ..config import Settings
from .models import PriceLevel, TokenBook

logger = structlog.get_logger(__name__)

try:  # pragma: no cover - import guard
    import websockets
except Exception:  # pragma: no cover
    websockets = None  # type: ignore[assignment]

# Connector yields an async context manager wrapping a live socket object that
# exposes async ``recv()`` and ``send()``. Injected in tests; defaults to
# ``websockets.connect`` in production.
Connector = Callable[[str], AsyncContextManager[Any]]
BooksChangedCb = Callable[[set[str]], None]


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value in ("", None):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _price_key(value: Any) -> float | None:
    """Stable float key for a price level (prices live on a tick grid)."""
    p = _f(value, default=-1.0)
    if p < 0:
        return None
    return round(p, 6)


class TokenMeta:
    """Per-token static info needed to materialize a TokenBook from raw levels."""

    __slots__ = ("market_id", "event_id", "fees_enabled", "tick_size")

    def __init__(self, market_id: str, event_id: str, fees_enabled: bool, tick_size: float) -> None:
        self.market_id = market_id
        self.event_id = event_id
        self.fees_enabled = fees_enabled
        self.tick_size = tick_size


class ClobBookStream:
    def __init__(
        self,
        config: Settings,
        on_books_changed: BooksChangedCb | None = None,
        connector: Connector | None = None,
    ) -> None:
        self._config = config
        self._on_books_changed = on_books_changed
        self._connector = connector
        self._url = config.clob_ws_url

        # Desired subscriptions and the static meta needed to build books.
        self._desired: set[str] = set()
        self._meta: dict[str, TokenMeta] = {}

        # Live book state kept as price->size maps for cheap delta application.
        self._bids: dict[str, dict[float, float]] = {}
        self._asks: dict[str, dict[float, float]] = {}
        self._last_update_mono: dict[str, float] = {}
        self._book_ts: dict[str, datetime] = {}

        # Coordination.
        self._stop = asyncio.Event()
        self._resubscribe = asyncio.Event()
        self._connected = False

        # Observability.
        self._messages = 0
        self._book_msgs = 0
        self._price_change_msgs = 0
        self._reconnects = 0
        self._last_message_mono: float | None = None
        self._connected_since_mono: float | None = None
        self._apply_latency_ms: deque[float] = deque(maxlen=256)

    # ── subscriptions ────────────────────────────────────────────────────

    def set_subscriptions(self, tokens: Iterable[tuple[str, TokenMeta]]) -> bool:
        """Replace the desired subscription set. Returns True if it changed.

        Triggers a reconnect (via ``_resubscribe``) so the next socket carries the
        new asset list. Books for tokens that fell out of the set are dropped.
        """
        cap = max(1, int(self._config.arb_max_book_subscriptions))
        new_meta: dict[str, TokenMeta] = {}
        for token_id, meta in tokens:
            if not token_id:
                continue
            new_meta[token_id] = meta
            if len(new_meta) >= cap:
                break
        new_desired = set(new_meta)
        if new_desired == self._desired:
            # Meta may still have shifted (tick size, fee flag); keep it fresh.
            self._meta = new_meta
            return False

        # Drop cached state for tokens we no longer track.
        for token_id in self._desired - new_desired:
            self._bids.pop(token_id, None)
            self._asks.pop(token_id, None)
            self._last_update_mono.pop(token_id, None)
            self._book_ts.pop(token_id, None)

        self._desired = new_desired
        self._meta = new_meta
        self._resubscribe.set()
        return True

    @property
    def subscribed_count(self) -> int:
        return len(self._desired)

    # ── message handling (pure; no I/O) ──────────────────────────────────

    def apply_raw(self, raw: Any) -> set[str]:
        """Parse one raw WS payload and apply it to the cache.

        Polymarket may batch several events into a JSON array, so this accepts a
        str/bytes/dict/list. Returns the set of token_ids whose book changed.
        """
        start = time.perf_counter()
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "ignore")
        if isinstance(raw, str):
            raw = raw.strip()
            if not raw:
                return set()
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("book_stream.bad_json")
                return set()

        messages = raw if isinstance(raw, list) else [raw]
        changed: set[str] = set()
        now_mono = time.perf_counter()
        self._last_message_mono = now_mono
        for message in messages:
            if not isinstance(message, dict):
                continue
            self._messages += 1
            changed |= self._apply_message(message, now_mono)

        if changed:
            self._apply_latency_ms.append((time.perf_counter() - start) * 1000.0)
        return changed

    def _apply_message(self, message: dict[str, Any], now_mono: float) -> set[str]:
        """Apply one market-channel message, returning the changed token ids.

        A single ``price_change`` message can update several tokens at once (each
        change carries its own ``asset_id``), so this returns a set, not one id.
        """
        event_type = str(message.get("event_type") or message.get("type") or "").strip().lower()

        if event_type == "book":
            token_id = self._extract_asset_id(message)
            if not token_id or token_id not in self._meta:
                return set()
            self._book_msgs += 1
            self._apply_book_snapshot(token_id, message)
            tick = _f(message.get("tick_size"))
            if tick > 0:
                self._meta[token_id].tick_size = tick
            self._mark_updated(token_id, now_mono, message.get("timestamp"))
            return {token_id}

        if event_type in ("price_change", "price_changes"):
            return self._apply_price_changes(message, now_mono)

        if event_type == "tick_size_change":
            token_id = self._extract_asset_id(message)
            new_tick = _f(message.get("new_tick_size") or message.get("tick_size"))
            if token_id in self._meta and new_tick > 0:
                self._meta[token_id].tick_size = new_tick
            return set()

        # last_trade_price and other informational events don't move the book.
        return set()

    def _apply_price_changes(self, message: dict[str, Any], now_mono: float) -> set[str]:
        # Polymarket's price_change carries a `price_changes` list, each entry with
        # its own asset_id/price/side/size. Older/simple payloads use `changes`
        # with a single top-level asset_id and no per-entry id — support both.
        entries = message.get("price_changes")
        if entries is None:
            entries = message.get("changes")
        if entries is None:
            if message.get("price") is not None and message.get("side") is not None:
                entries = [message]
            else:
                return set()
        if not isinstance(entries, list):
            return set()

        default_token = self._extract_asset_id(message)
        changed: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            token_id = self._extract_asset_id(entry) or default_token
            if not token_id or token_id not in self._meta:
                continue
            key = _price_key(self._level_price(entry))
            if key is None:
                continue
            size = _f(self._level_size(entry))
            side = str(entry.get("side") or entry.get("side_type") or "").strip().upper()
            book = self._bids.setdefault(token_id, {}) if side in ("BUY", "BID", "BIDS") \
                else self._asks.setdefault(token_id, {})
            if size <= 0:
                book.pop(key, None)
            else:
                book[key] = size
            self._mark_updated(token_id, now_mono, message.get("timestamp"))
            changed.add(token_id)
        if changed:
            self._price_change_msgs += 1
        return changed

    def _mark_updated(self, token_id: str, now_mono: float, timestamp: Any) -> None:
        self._last_update_mono[token_id] = now_mono
        self._book_ts[token_id] = self._parse_ts(timestamp)

    @staticmethod
    def _extract_asset_id(payload: dict[str, Any]) -> str:
        return str(payload.get("asset_id") or payload.get("assetId") or payload.get("asset") or "")

    def _apply_book_snapshot(self, token_id: str, message: dict[str, Any]) -> None:
        bids: dict[float, float] = {}
        asks: dict[float, float] = {}
        for level in message.get("bids") or message.get("buys") or []:
            key = _price_key(self._level_price(level))
            size = _f(self._level_size(level))
            if key is not None and size > 0:
                bids[key] = size
        for level in message.get("asks") or message.get("sells") or []:
            key = _price_key(self._level_price(level))
            size = _f(self._level_size(level))
            if key is not None and size > 0:
                asks[key] = size
        self._bids[token_id] = bids
        self._asks[token_id] = asks

    @staticmethod
    def _level_price(level: Any) -> Any:
        if isinstance(level, dict):
            return level.get("price")
        return getattr(level, "price", None)

    @staticmethod
    def _level_size(level: Any) -> Any:
        if isinstance(level, dict):
            return level.get("size") if level.get("size") is not None else level.get("amount")
        return getattr(level, "size", None)

    @staticmethod
    def _parse_ts(value: Any) -> datetime:
        # Market channel timestamps are unix ms strings; fall back to receive time.
        try:
            if value not in (None, ""):
                ms = float(value)
                if ms > 1e12:  # milliseconds
                    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
                if ms > 1e9:  # seconds
                    return datetime.fromtimestamp(ms, tz=timezone.utc)
        except (TypeError, ValueError):
            pass
        return datetime.now(timezone.utc)

    # ── reading the cache ────────────────────────────────────────────────

    def materialize(self, token_id: str) -> TokenBook | None:
        meta = self._meta.get(token_id)
        if meta is None:
            return None
        raw_bids = self._bids.get(token_id) or {}
        raw_asks = self._asks.get(token_id) or {}
        if not raw_bids and not raw_asks:
            return None
        bids = [PriceLevel(price=p, size=s) for p, s in sorted(raw_bids.items(), reverse=True)]
        asks = [PriceLevel(price=p, size=s) for p, s in sorted(raw_asks.items())]
        return TokenBook(
            token_id=token_id,
            timestamp=self._book_ts.get(token_id) or datetime.now(timezone.utc),
            best_bid=bids[0].price if bids else 0.0,
            best_ask=asks[0].price if asks else 0.0,
            bids=bids,
            asks=asks,
            fees_enabled=meta.fees_enabled,
            tick_size=meta.tick_size,
            source="clob_ws",
        )

    def book_age_seconds(self, token_id: str) -> float | None:
        last = self._last_update_mono.get(token_id)
        if last is None:
            return None
        return max(time.perf_counter() - last, 0.0)

    def fresh_books(self, token_ids: Iterable[str], max_age: float | None = None) -> dict[str, TokenBook]:
        """Materialize books for the requested tokens that are fresh enough."""
        if max_age is None:
            max_age = float(self._config.arb_book_staleness_seconds)
        out: dict[str, TokenBook] = {}
        for token_id in token_ids:
            age = self.book_age_seconds(token_id)
            if age is None or age > max_age:
                continue
            book = self.materialize(token_id)
            if book is not None and (book.best_bid > 0 or book.best_ask > 0):
                out[token_id] = book
        return out

    def metrics(self) -> dict[str, Any]:
        now = time.perf_counter()
        last_age = (now - self._last_message_mono) if self._last_message_mono is not None else None
        uptime = (now - self._connected_since_mono) if self._connected_since_mono is not None else None
        avg_apply = (
            sum(self._apply_latency_ms) / len(self._apply_latency_ms)
            if self._apply_latency_ms
            else 0.0
        )
        fresh = sum(
            1
            for token_id in self._desired
            if (age := self.book_age_seconds(token_id)) is not None
            and age <= float(self._config.arb_book_staleness_seconds)
        )
        return {
            "connected": self._connected,
            "subscribed": len(self._desired),
            "cached_books": len(set(self._bids) | set(self._asks)),
            "fresh_books": fresh,
            "messages": self._messages,
            "book_messages": self._book_msgs,
            "price_change_messages": self._price_change_msgs,
            "reconnects": self._reconnects,
            "last_message_age_seconds": round(last_age, 3) if last_age is not None else None,
            "connection_uptime_seconds": round(uptime, 1) if uptime is not None else None,
            "avg_apply_latency_ms": round(avg_apply, 4),
        }

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── network loop ─────────────────────────────────────────────────────

    async def run(self) -> None:
        """Connect → subscribe → read, reconnecting with capped backoff.

        Returns cleanly (without raising) when the WebSocket library is missing so
        the engine can degrade to the REST poll path instead of crashing.
        """
        if self._connector is None and websockets is None:  # pragma: no cover
            logger.warning("book_stream.websockets_unavailable")
            return

        backoff = 0.5
        backoff_max = max(1.0, float(self._config.arb_ws_reconnect_max_seconds))
        while not self._stop.is_set():
            if not self._desired:
                # Nothing to watch yet (universe not loaded); idle briefly.
                if await self._sleep_or_stop(0.25):
                    break
                continue
            clean_return = False
            try:
                await self._connect_once()
                clean_return = True  # exited to apply new subscriptions, not an error
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._reconnects += 1
                logger.warning("book_stream.connection_error", error=str(exc))
            finally:
                self._connected = False
                self._connected_since_mono = None
            if self._stop.is_set():
                break
            if clean_return:
                # A subscription change closed the socket on purpose; reconnect
                # immediately so universe refreshes don't add a backoff gap.
                backoff = 0.5
                continue
            if await self._sleep_or_stop(backoff):
                break
            backoff = min(backoff * 2, backoff_max)

    async def _connect_once(self) -> None:
        connector = self._connector or self._default_connector
        async with connector(self._url) as ws:
            self._connected = True
            self._connected_since_mono = time.perf_counter()
            self._resubscribe.clear()
            await self._send_subscribe(ws)
            logger.info("book_stream.connected", subscribed=len(self._desired))
            while not self._stop.is_set() and not self._resubscribe.is_set():
                try:
                    # Short tick so a subscription change (new universe) is picked
                    # up within ~1s. Active streams iterate far faster than this.
                    message = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue  # idle keepalive tick; re-check stop/resubscribe
                changed = self.apply_raw(message)
                if changed and self._on_books_changed is not None:
                    try:
                        self._on_books_changed(changed)
                    except Exception as exc:  # never let a callback kill the read loop
                        logger.warning("book_stream.callback_error", error=str(exc))

    async def _send_subscribe(self, ws: Any) -> None:
        payload = {"type": "market", "assets_ids": sorted(self._desired)}
        await ws.send(json.dumps(payload))

    def _default_connector(self, url: str) -> AsyncContextManager[Any]:  # pragma: no cover
        # ping_interval keeps the socket alive; the server also drops idle clients.
        return websockets.connect(url, ping_interval=10, ping_timeout=20, close_timeout=5, max_queue=None)

    async def _sleep_or_stop(self, seconds: float) -> bool:
        """Sleep up to ``seconds`` or until stop is set. Returns True if stopping."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def close(self) -> None:
        self._stop.set()
        self._resubscribe.set()
