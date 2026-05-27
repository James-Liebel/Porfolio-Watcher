from __future__ import annotations

import asyncio
import json
import os
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from src.arb.engine import ArbEngine
from src.arb.exchange import PaperExchange
from src.arb.live_exchange import LiveClobExchange, LiveExecutionError
from src.arb.models import ArbEvent, ArbOpportunity, OpportunityLeg, OrderIntent, OutcomeMarket
from src.arb.repository import ArbRepository
from src.arb.risk import ArbRiskManager
from src.arb.streaming import ClobBookStream, TokenMeta
from src.config import Settings
from src.storage.db import Database


# ── helpers ──────────────────────────────────────────────────────────────


def _settings(**overrides) -> Settings:
    defaults = dict(
        paper_trade=True,
        initial_bankroll=100.0,
        log_level="WARNING",
        max_basket_notional=50.0,
        min_event_liquidity=0.0,
        min_outcomes_per_event=2,
        min_complete_set_edge_bps=10.0,
        min_neg_risk_edge_bps=10.0,
        allow_taker_execution=True,
        paper_taker_fee_bps=0.0,
        paper_maker_rebate_bps=0.0,
        arb_slippage_buffer_bps=0.0,
        opportunity_cooldown_seconds=0,
        max_total_open_baskets=10,
        max_opportunities_per_cycle=5,
        max_event_exposure_pct=1.0,
        daily_loss_cap=0.50,
        arb_poll_seconds=1,
        arb_hot_scan_debounce_ms=0,
    )
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def _live_settings(armed: bool, **overrides) -> Settings:
    base = dict(
        paper_trade=False,
        enable_live_execution=True,
        live_dry_run=not armed,
        polymarket_api_key="k",
        polymarket_secret="s",
        polymarket_passphrase="p",
        polymarket_wallet_address="0xabc",
        wallet_private_key="0xkey",
        initial_bankroll=100.0,
        live_max_order_usdc=25.0,
        paper_taker_fee_bps=0.0,
    )
    base.update(overrides)
    return _settings(**base)


def _complete_set_event() -> ArbEvent:
    return ArbEvent(
        event_id="event-1",
        title="Who wins?",
        neg_risk=True,
        markets=[
            OutcomeMarket("event-1", "m1", "A?", "A", "y1", "n1", tick_size=0.01),
            OutcomeMarket("event-1", "m2", "B?", "B", "y2", "n2", tick_size=0.01),
            OutcomeMarket("event-1", "m3", "C?", "C", "y3", "n3", tick_size=0.01),
        ],
    )


def _book_msg(token: str, bid: float, ask: float, size: float = 100.0) -> str:
    return json.dumps(
        {
            "event_type": "book",
            "asset_id": token,
            "bids": [{"price": str(bid), "size": str(size)}],
            "asks": [{"price": str(ask), "size": str(size)}],
            "timestamp": "1700000000000",
        }
    )


class _StaticUniverse:
    def __init__(self, events):
        self._events = events

    async def refresh(self):
        return self._events

    async def close(self):
        return None

    async def lookup_resolution(self, event_id, fallback_event=None):
        return (fallback_event, None, "unresolved")


class _StaticMarketData:
    def __init__(self, books=None):
        self._books = books or {}

    async def refresh(self, events):
        return self._books


class _FakeClient:
    """Stand-in for py-clob-client; records calls and returns a canned response."""

    def __init__(self, response):
        self._response = response
        self.created = []
        self.posted = []

    def create_order(self, args):
        self.created.append(args)
        return {"signed": True}

    def post_order(self, signed, order_type):
        self.posted.append((signed, order_type))
        return self._response


# ── streaming: pure message handling ──────────────────────────────────────


def _stream_with_token(token="TOK", fees=True, tick=0.01):
    changed_log = []
    stream = ClobBookStream(_settings(), on_books_changed=lambda c: changed_log.append(set(c)))
    stream.set_subscriptions([(token, TokenMeta("m1", "e1", fees, tick))])
    return stream, changed_log


def test_book_snapshot_sorts_and_sets_top_of_book():
    stream, _ = _stream_with_token()
    changed = stream.apply_raw(
        json.dumps(
            {
                "event_type": "book",
                "asset_id": "TOK",
                "bids": [{"price": "0.40", "size": "10"}, {"price": "0.45", "size": "5"}],
                "asks": [{"price": "0.55", "size": "8"}, {"price": "0.50", "size": "3"}],
                "timestamp": "1700000000000",
            }
        )
    )
    assert changed == {"TOK"}
    book = stream.materialize("TOK")
    assert book.best_bid == 0.45 and book.best_ask == 0.50
    assert book.bids[0].price == 0.45  # best bid highest
    assert book.asks[0].price == 0.50  # best ask lowest
    assert book.source == "clob_ws"
    assert book.fees_enabled is True


def test_price_change_adds_updates_and_removes_levels():
    stream, _ = _stream_with_token()
    stream.apply_raw(_book_msg("TOK", 0.45, 0.50))
    stream.apply_raw(
        json.dumps(
            {
                "event_type": "price_change",
                "asset_id": "TOK",
                "changes": [
                    {"price": "0.49", "side": "SELL", "size": "4"},  # new better ask
                    {"price": "0.50", "side": "SELL", "size": "0"},  # remove old ask
                    {"price": "0.46", "side": "BUY", "size": "2"},   # new better bid
                ],
            }
        )
    )
    book = stream.materialize("TOK")
    assert book.best_ask == 0.49
    assert book.best_bid == 0.46
    assert all(abs(level.price - 0.50) > 1e-9 for level in book.asks)


def test_price_change_with_per_entry_asset_ids_updates_multiple_tokens():
    """Real Polymarket format: one price_change message, per-entry asset_id, many tokens."""
    changed_log = []
    stream = ClobBookStream(_settings(), on_books_changed=lambda c: changed_log.append(set(c)))
    stream.set_subscriptions(
        [("A", TokenMeta("m1", "e1", False, 0.01)), ("B", TokenMeta("m2", "e1", False, 0.01))]
    )
    stream.apply_raw(_book_msg("A", 0.40, 0.41))
    stream.apply_raw(_book_msg("B", 0.55, 0.56))

    # No top-level asset_id; each change names its own token (as Polymarket sends).
    changed = stream.apply_raw(
        json.dumps(
            {
                "event_type": "price_change",
                "market": "0xcondition",
                "price_changes": [
                    {"asset_id": "A", "price": "0.40", "side": "SELL", "size": "10"},  # better ask for A
                    {"asset_id": "B", "price": "0.50", "side": "BUY", "size": "7"},    # new bid level for B
                ],
                "timestamp": "1700000000000",
            }
        )
    )
    assert changed == {"A", "B"}
    assert stream.materialize("A").best_ask == 0.40  # A's ask improved 0.41 -> 0.40
    assert any(level.price == 0.50 for level in stream.materialize("B").bids)  # B got the bid level
    assert stream.metrics()["price_change_messages"] == 1


def test_unknown_token_is_ignored():
    stream, _ = _stream_with_token()
    assert stream.apply_raw(_book_msg("NOT_SUBSCRIBED", 0.4, 0.5)) == set()
    assert stream.materialize("NOT_SUBSCRIBED") is None


def test_batched_array_message_applies_each():
    stream, _ = _stream_with_token()
    stream.set_subscriptions(
        [("A", TokenMeta("m1", "e1", False, 0.01)), ("B", TokenMeta("m2", "e1", False, 0.01))]
    )
    changed = stream.apply_raw("[" + _book_msg("A", 0.3, 0.31) + "," + _book_msg("B", 0.6, 0.61) + "]")
    assert changed == {"A", "B"}


def test_staleness_excludes_old_books():
    stream, _ = _stream_with_token()
    stream.apply_raw(_book_msg("TOK", 0.45, 0.50))
    assert len(stream.fresh_books(["TOK"], max_age=10.0)) == 1
    assert len(stream.fresh_books(["TOK"], max_age=0.0)) == 0  # age > 0 -> stale


def test_tick_size_change_updates_meta():
    stream, _ = _stream_with_token(tick=0.01)
    stream.apply_raw(_book_msg("TOK", 0.45, 0.50))
    stream.apply_raw(json.dumps({"event_type": "tick_size_change", "asset_id": "TOK", "new_tick_size": "0.001"}))
    assert stream.materialize("TOK").tick_size == 0.001


def test_set_subscriptions_drops_removed_tokens_and_signals_resubscribe():
    stream, _ = _stream_with_token("A")
    stream.apply_raw(_book_msg("A", 0.4, 0.5))
    stream._resubscribe.clear()
    changed = stream.set_subscriptions([("B", TokenMeta("m2", "e1", False, 0.01))])
    assert changed is True
    assert stream._resubscribe.is_set()
    assert stream.materialize("A") is None  # dropped
    assert stream.subscribed_count == 1


# ── streaming: network loop with an injected connector ─────────────────────


@pytest.mark.anyio
async def test_stream_run_subscribes_and_invokes_callback():
    got: list[set[str]] = []
    stream = ClobBookStream(_settings(), on_books_changed=lambda c: got.append(set(c)))
    stream.set_subscriptions([("TOK", TokenMeta("m1", "e1", True, 0.01))])

    class FakeWS:
        def __init__(self, messages):
            self.sent: list[str] = []
            self._q: asyncio.Queue = asyncio.Queue()
            for m in messages:
                self._q.put_nowait(m)

        async def send(self, data):
            self.sent.append(data)

        async def recv(self):
            if self._q.empty():
                await asyncio.sleep(3600)
            return await self._q.get()

    fake = FakeWS([_book_msg("TOK", 0.45, 0.50)])

    @asynccontextmanager
    async def connector(url):
        yield fake

    stream._connector = connector
    task = asyncio.create_task(stream.run())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if got:
            break
    await stream.close()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert got == [{"TOK"}]
    payload = json.loads(fake.sent[0])
    assert payload["type"] == "market" and payload["assets_ids"] == ["TOK"]
    assert stream.materialize("TOK").best_ask == 0.50


@pytest.mark.anyio
async def test_stream_resubscribes_on_subscription_change():
    """Changing subscriptions reconnects the socket with the new asset set."""
    stream = ClobBookStream(_settings())
    stream.set_subscriptions([("A", TokenMeta("m1", "e1", False, 0.01))])

    connections = []

    class FakeWS:
        def __init__(self):
            self.sent = []

        async def send(self, data):
            self.sent.append(data)

        async def recv(self):
            await asyncio.sleep(3600)  # never delivers; loop ticks on timeout

    @asynccontextmanager
    async def connector(url):
        ws = FakeWS()
        connections.append(ws)
        yield ws

    stream._connector = connector
    task = asyncio.create_task(stream.run())

    for _ in range(200):
        await asyncio.sleep(0.01)
        if connections and connections[0].sent:
            break
    # Change the subscription set -> triggers a reconnect with the new assets.
    stream.set_subscriptions([("B", TokenMeta("m2", "e1", False, 0.01))])
    for _ in range(300):
        await asyncio.sleep(0.01)
        if len(connections) >= 2 and connections[1].sent:
            break

    await stream.close()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert len(connections) >= 2
    assert json.loads(connections[0].sent[0])["assets_ids"] == ["A"]
    assert json.loads(connections[1].sent[0])["assets_ids"] == ["B"]


# ── engine hot path ────────────────────────────────────────────────────────


def _seed_arb_stream(engine, event):
    engine._stream = ClobBookStream(engine._config, on_books_changed=engine._on_books_changed)
    engine._sync_stream_subscriptions([event])
    # YES asks 0.30+0.25+0.20 = 0.75 -> clear complete-set arbitrage.
    for token, (bid, ask) in {"y1": (0.29, 0.30), "y2": (0.24, 0.25), "y3": (0.19, 0.20)}.items():
        engine._stream.apply_raw(_book_msg(token, bid, ask))


@pytest.mark.anyio
async def test_hot_evaluate_executes_on_streamed_arbitrage():
    event = _complete_set_event()
    settings = _settings(max_opportunities_per_cycle=1, max_basket_notional=3.75)
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
        path = handle.name
    try:
        engine = ArbEngine(
            config=settings,
            legacy_db=Database(path=path),
            repository=ArbRepository(path=path),
            universe=_StaticUniverse([event]),
            market_data=_StaticMarketData(),
        )
        await engine.run_cycle()  # load universe; no books -> nothing executes
        assert engine.exchange.get_positions() == []

        _seed_arb_stream(engine, event)
        executed = await engine._hot_evaluate({"event-1"})

        assert executed == 1
        assert len(engine.exchange.get_positions()) == 3
        assert engine._hot_executions == 1
        assert engine._last_hot_latency_ms is not None
    finally:
        os.unlink(path)


@pytest.mark.anyio
async def test_hot_evaluate_skips_stale_books():
    event = _complete_set_event()
    settings = _settings(arb_book_staleness_seconds=0.0)  # everything is instantly "stale"
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
        path = handle.name
    try:
        engine = ArbEngine(
            config=settings,
            legacy_db=Database(path=path),
            repository=ArbRepository(path=path),
            universe=_StaticUniverse([event]),
            market_data=_StaticMarketData(),
        )
        await engine.run_cycle()
        _seed_arb_stream(engine, event)
        executed = await engine._hot_evaluate({"event-1"})
        assert executed == 0  # stale books excluded -> no scan input
        assert engine.exchange.get_positions() == []
    finally:
        os.unlink(path)


def test_on_books_changed_marks_dirty_events():
    event = _complete_set_event()
    settings = _settings()
    engine = ArbEngine(
        config=settings,
        legacy_db=Database(path=os.path.join(tempfile.gettempdir(), "unused_hot.db")),
        repository=ArbRepository(path=os.path.join(tempfile.gettempdir(), "unused_hot.db")),
        universe=_StaticUniverse([event]),
        market_data=_StaticMarketData(),
    )
    engine._stream = ClobBookStream(settings, on_books_changed=engine._on_books_changed)
    engine._sync_stream_subscriptions([event])

    engine._on_books_changed({"y2"})
    assert engine._dirty_events == {"event-1"}
    assert engine._dirty_signal.is_set()

    # A token we don't track maps to nothing and doesn't wake the loop.
    engine._dirty_events.clear()
    engine._dirty_signal.clear()
    engine._on_books_changed({"unknown-token"})
    assert engine._dirty_events == set()
    assert not engine._dirty_signal.is_set()


@pytest.mark.anyio
async def test_hot_loop_reacts_to_dirty_signal():
    event = _complete_set_event()
    settings = _settings(max_opportunities_per_cycle=1, max_basket_notional=3.75, arb_hot_scan_debounce_ms=0)
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
        path = handle.name
    try:
        engine = ArbEngine(
            config=settings,
            legacy_db=Database(path=path),
            repository=ArbRepository(path=path),
            universe=_StaticUniverse([event]),
            market_data=_StaticMarketData(),
        )
        await engine.run_cycle()
        _seed_arb_stream(engine, event)

        loop_task = asyncio.create_task(engine._hot_loop())
        engine._on_books_changed({"y1", "y2", "y3"})  # wake the loop
        for _ in range(400):
            await asyncio.sleep(0.005)
            if engine._hot_executions:
                break
        engine._stop.set()
        engine._dirty_signal.set()
        await asyncio.wait_for(loop_task, timeout=2.0)

        assert engine._hot_executions == 1
        assert len(engine.exchange.get_positions()) == 3
    finally:
        os.unlink(path)


class _StreamAwareMarketData:
    """Market-data stub that records the attached stream; books come from it."""

    def __init__(self):
        self.stream = None

    def attach_stream(self, stream):
        self.stream = stream

    async def refresh(self, events):
        # No REST contribution; the WebSocket stream is the sole book source here.
        return {}


@pytest.mark.anyio
async def test_run_streaming_executes_from_live_socket_end_to_end():
    """Full run() streaming path: socket fills books -> hot loop trades, no poll."""
    event = _complete_set_event()
    settings = _settings(
        arb_streaming_enabled=True,
        arb_hot_scan_debounce_ms=0,
        arb_universe_refresh_seconds=3600,  # keep the slow loop out of the way
        max_opportunities_per_cycle=1,
        max_basket_notional=3.75,
    )

    class FakeWS:
        def __init__(self, messages):
            self.sent = []
            self._q: asyncio.Queue = asyncio.Queue()
            for m in messages:
                self._q.put_nowait(m)

        async def send(self, data):
            self.sent.append(data)

        async def recv(self):
            if self._q.empty():
                await asyncio.sleep(3600)
            return await self._q.get()

    fake = FakeWS([_book_msg("y1", 0.29, 0.30), _book_msg("y2", 0.24, 0.25), _book_msg("y3", 0.19, 0.20)])

    @asynccontextmanager
    async def connector(url):
        yield fake

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
        path = handle.name
    try:
        engine = ArbEngine(
            config=settings,
            legacy_db=Database(path=path),
            repository=ArbRepository(path=path),
            universe=_StaticUniverse([event]),
            market_data=_StreamAwareMarketData(),
        )
        engine._stream_connector = connector
        run_task = asyncio.create_task(engine.run())

        for _ in range(800):  # generous budget so it's not flaky under suite load
            await asyncio.sleep(0.01)
            if engine._hot_executions:
                break

        await engine.shutdown()
        try:
            await asyncio.wait_for(run_task, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            run_task.cancel()

        assert engine._hot_executions == 1
        assert len(engine.exchange.get_positions()) == 3
        # The market-data stub got the stream wired in by the streaming runner.
        assert engine._market_data.stream is engine._stream
    finally:
        os.unlink(path)


# ── exchange selection + live gating ───────────────────────────────────────


def test_engine_defaults_to_paper_exchange():
    engine = ArbEngine(
        config=_settings(),
        legacy_db=Database(path=os.path.join(tempfile.gettempdir(), "sel1.db")),
        repository=ArbRepository(path=os.path.join(tempfile.gettempdir(), "sel1.db")),
        universe=_StaticUniverse([]),
        market_data=_StaticMarketData(),
    )
    assert isinstance(engine.exchange, PaperExchange)
    assert not isinstance(engine.exchange, LiveClobExchange)
    assert engine.exchange.execution_mode == "paper"


def test_engine_selects_live_exchange_when_configured():
    engine = ArbEngine(
        config=_live_settings(armed=False),
        legacy_db=Database(path=os.path.join(tempfile.gettempdir(), "sel2.db")),
        repository=ArbRepository(path=os.path.join(tempfile.gettempdir(), "sel2.db")),
        universe=_StaticUniverse([]),
        market_data=_StaticMarketData(),
    )
    assert isinstance(engine.exchange, LiveClobExchange)
    assert engine.exchange.execution_mode == "live_dry_run"


def test_engine_falls_back_to_paper_without_credentials():
    settings = _settings(paper_trade=False, enable_live_execution=True, live_dry_run=False)
    engine = ArbEngine(
        config=settings,
        legacy_db=Database(path=os.path.join(tempfile.gettempdir(), "sel3.db")),
        repository=ArbRepository(path=os.path.join(tempfile.gettempdir(), "sel3.db")),
        universe=_StaticUniverse([]),
        market_data=_StaticMarketData(),
    )
    assert isinstance(engine.exchange, PaperExchange)
    assert not isinstance(engine.exchange, LiveClobExchange)


def test_config_live_gates():
    paper = _settings()
    assert paper.live_execution_configured() is False
    assert paper.live_execution_armed() is False

    dry = _live_settings(armed=False)
    assert dry.has_live_credentials() is True
    assert dry.live_execution_configured() is True
    assert dry.live_execution_armed() is False  # dry-run withholds the POST

    armed = _live_settings(armed=True)
    assert armed.live_execution_configured() is True
    assert armed.live_execution_armed() is True


def _book(token, bid, ask):
    from src.arb.models import PriceLevel, TokenBook

    return TokenBook(token, datetime.now(timezone.utc), bid, ask, bids=[PriceLevel(bid, 100)], asks=[PriceLevel(ask, 100)])


def _buy_intent(token="y1", price=0.30, size=5.0):
    return OrderIntent(
        basket_id="b1",
        opportunity_id="o1",
        token_id=token,
        market_id="m1",
        event_id="event-1",
        contract_side="YES",
        side="BUY",
        price=price,
        size=size,
        order_type="fok",
        maker_or_taker="taker",
    )


def test_live_dry_run_simulates_and_tags_order():
    cfg = _live_settings(armed=False)
    ex = LiveClobExchange(cfg)
    ex.set_starting_cash(100.0)
    ex.update_universe([_complete_set_event()])
    ex.sync_books({"y1": _book("y1", 0.29, 0.30)})

    order, fills = ex.place_order(_buy_intent())
    assert order.status == "filled"
    assert order.metadata.get("live_dry_run") is True
    assert sum(f.size for f in fills) == pytest.approx(5.0)


def test_live_order_rejected_over_usdc_ceiling():
    cfg = _live_settings(armed=False, live_max_order_usdc=1.0)
    ex = LiveClobExchange(cfg)
    ex.set_starting_cash(100.0)
    ex.update_universe([_complete_set_event()])
    ex.sync_books({"y1": _book("y1", 0.29, 0.30)})

    order, fills = ex.place_order(_buy_intent(size=5.0))  # 1.5 USDC > 1.0 ceiling
    assert order.status == "rejected"
    assert "live_max_order_usdc" in order.reason
    assert fills == []
    assert ex.get_positions() == []


def test_live_armed_refuses_neg_risk_conversion():
    cfg = _live_settings(armed=True)
    ex = LiveClobExchange(cfg)
    with pytest.raises(LiveExecutionError):
        ex.convert_neg_risk(_complete_set_event(), "m1", 5.0)


def test_risk_rejects_conversion_when_live_armed_but_allows_in_dry_run():
    opp = ArbOpportunity(
        strategy_type="neg_risk_conversion",
        event_id="event-1",
        event_title="t",
        gross_edge_bps=100.0,
        net_edge_bps=80.0,
        capital_required=5.0,
        expected_profit=0.5,
        legs=[OpportunityLeg("m1", "n1", "A", "NO", "BUY", 0.30, 5.0)],
        rationale="",
        requires_conversion=True,
    )

    armed = _live_settings(armed=True)
    risk_armed = ArbRiskManager(armed)
    ok, reason = risk_armed.approve(opp, LiveClobExchange(armed), open_baskets=0, open_baskets_by_strategy={})
    assert ok is False and "conversion" in reason

    dry = _live_settings(armed=False)
    risk_dry = ArbRiskManager(dry)
    ok2, _ = risk_dry.approve(opp, LiveClobExchange(dry), open_baskets=0, open_baskets_by_strategy={})
    assert ok2 is True


def test_live_armed_posts_and_reconciles_a_fill():
    cfg = _live_settings(armed=True)
    client = _FakeClient({"success": True, "status": "matched", "orderID": "0xfeed"})
    ex = LiveClobExchange(cfg, client=client)
    ex.set_starting_cash(100.0)
    ex.update_universe([_complete_set_event()])

    cash0 = ex.cash
    order, fills = ex.place_order(_buy_intent(price=0.30, size=5.0))

    assert len(client.created) == 1 and len(client.posted) == 1
    assert order.status == "filled"
    assert order.order_id == "0xfeed"
    positions = ex.get_positions()
    assert len(positions) == 1 and positions[0].size == pytest.approx(5.0)
    assert ex.cash == pytest.approx(cash0 - 0.30 * 5.0)  # zero fee in test config


def test_live_armed_treats_unmatched_response_as_no_fill():
    cfg = _live_settings(armed=True)
    client = _FakeClient({"success": True, "status": "live"})  # resting, not matched
    ex = LiveClobExchange(cfg, client=client)
    ex.set_starting_cash(100.0)
    ex.update_universe([_complete_set_event()])

    order, fills = ex.place_order(_buy_intent())
    assert order.status == "rejected"
    assert fills == []
    assert ex.get_positions() == []  # never books phantom inventory
