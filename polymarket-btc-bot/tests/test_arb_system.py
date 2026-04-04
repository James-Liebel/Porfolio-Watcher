from __future__ import annotations

import json
import os
import tempfile

import pytest
import aiohttp.web as web
from aiohttp.test_utils import TestClient, TestServer

from src.arb.control import ArbControlAPI, cors_middleware
from src.arb.engine import ArbEngine
from src.arb.exchange import PaperExchange
from src.arb.models import ArbEvent, OrderIntent, OutcomeMarket, PriceLevel, TokenBook
from src.arb.pricing import OpportunityScanner
from src.arb.replay import load_cycle_records, replay_cycle_records
from src.arb.repository import ArbRepository
from src.config import Settings
from src.storage.db import Database


def _settings(**overrides) -> Settings:
    defaults = dict(
        paper_trade=True,
        initial_bankroll=100.0,
        control_api_port=18765,
        log_level="WARNING",
        max_basket_notional=50.0,
        min_event_liquidity=0.0,
        min_outcomes_per_event=2,
        min_complete_set_edge_bps=10.0,
        min_neg_risk_edge_bps=10.0,
        allow_taker_execution=True,
        paper_taker_fee_bps=0.0,
        paper_maker_rebate_bps=0.0,
        opportunity_cooldown_seconds=0,
        max_total_open_baskets=10,
        max_opportunities_per_cycle=5,
        max_event_exposure_pct=1.0,
        daily_loss_cap=0.50,
        arb_poll_seconds=1,
        paper_equity_snapshot_log=False,
        arb_halt_execution_if_synthetic_books_ge=0,
        max_arb_leg_spread_bps=0.0,
        arb_min_expected_profit_usd=0.0,
        arb_consecutive_execution_failures_halt=0,
        paper_spread_penalty_bps=0.0,
    )
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def _complete_set_event() -> tuple[ArbEvent, dict[str, TokenBook]]:
    event = ArbEvent(
        event_id="event-1",
        title="Who wins?",
        markets=[
            OutcomeMarket("event-1", "m1", "A?", "A", "y1", "n1", tick_size=0.01),
            OutcomeMarket("event-1", "m2", "B?", "B", "y2", "n2", tick_size=0.01),
            OutcomeMarket("event-1", "m3", "C?", "C", "y3", "n3", tick_size=0.01),
        ],
    )
    books = {
        "y1": TokenBook("y1", event_time(), 0.29, 0.30, bids=[PriceLevel(0.29, 100)], asks=[PriceLevel(0.30, 100)]),
        "y2": TokenBook("y2", event_time(), 0.24, 0.25, bids=[PriceLevel(0.24, 100)], asks=[PriceLevel(0.25, 100)]),
        "y3": TokenBook("y3", event_time(), 0.19, 0.20, bids=[PriceLevel(0.19, 100)], asks=[PriceLevel(0.20, 100)]),
        "n1": TokenBook("n1", event_time(), 0.69, 0.70, bids=[PriceLevel(0.69, 100)], asks=[PriceLevel(0.70, 100)]),
        "n2": TokenBook("n2", event_time(), 0.74, 0.75, bids=[PriceLevel(0.74, 100)], asks=[PriceLevel(0.75, 100)]),
        "n3": TokenBook("n3", event_time(), 0.79, 0.80, bids=[PriceLevel(0.79, 100)], asks=[PriceLevel(0.80, 100)]),
    }
    return event, books


def _neg_risk_event() -> tuple[ArbEvent, dict[str, TokenBook]]:
    event = ArbEvent(
        event_id="event-neg",
        title="Election",
        neg_risk=True,
        markets=[
            OutcomeMarket("event-neg", "m1", "A?", "A", "y1", "n1", tick_size=0.01),
            OutcomeMarket("event-neg", "m2", "B?", "B", "y2", "n2", tick_size=0.01),
            OutcomeMarket("event-neg", "m3", "C?", "C", "y3", "n3", tick_size=0.01),
        ],
    )
    books = {
        "y1": TokenBook("y1", event_time(), 0.54, 0.55, bids=[PriceLevel(0.54, 100)], asks=[PriceLevel(0.55, 100)]),
        "n1": TokenBook("n1", event_time(), 0.29, 0.30, bids=[PriceLevel(0.29, 100)], asks=[PriceLevel(0.30, 100)]),
        "y2": TokenBook("y2", event_time(), 0.24, 0.25, bids=[PriceLevel(0.24, 100)], asks=[PriceLevel(0.25, 100)]),
        "n2": TokenBook("n2", event_time(), 0.74, 0.75, bids=[PriceLevel(0.74, 100)], asks=[PriceLevel(0.75, 100)]),
        "y3": TokenBook("y3", event_time(), 0.20, 0.21, bids=[PriceLevel(0.20, 100)], asks=[PriceLevel(0.21, 100)]),
        "n3": TokenBook("n3", event_time(), 0.78, 0.79, bids=[PriceLevel(0.78, 100)], asks=[PriceLevel(0.79, 100)]),
    }
    return event, books


def event_time():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _set_book_source(books: dict[str, TokenBook], source: str) -> dict[str, TokenBook]:
    for book in books.values():
        book.source = source
    return books


class StaticUniverse:
    def __init__(self, events=None, refresh_sequence=None, resolution_map=None):
        self._events = events or []
        self._refresh_sequence = refresh_sequence
        self._resolution_map = resolution_map or {}
        self._refresh_calls = 0

    async def refresh(self):
        if self._refresh_sequence is not None:
            index = min(self._refresh_calls, len(self._refresh_sequence) - 1)
            self._refresh_calls += 1
            return self._refresh_sequence[index]
        return self._events

    async def close(self):
        return None

    async def lookup_resolution(self, event_id, fallback_event=None):
        return self._resolution_map.get(event_id, (fallback_event, None, "unresolved"))


class StaticMarketData:
    def __init__(self, books):
        self._books = books

    async def refresh(self, events, on_progress=None):
        del events
        if on_progress:
            on_progress(100.0)
        return self._books


@pytest.mark.anyio
async def test_paper_equity_snapshot_log_writes_jsonl(tmp_path):
    event, books = _complete_set_event()
    log_path = tmp_path / "eq.jsonl"
    settings = _settings(
        max_opportunities_per_cycle=1,
        max_basket_notional=3.75,
        paper_equity_snapshot_log=True,
        paper_equity_log_path=str(log_path),
    )
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
        path = handle.name
    try:
        legacy_db = Database(path=path)
        repository = ArbRepository(path=path)
        engine = ArbEngine(
            config=settings,
            legacy_db=legacy_db,
            repository=repository,
            universe=StaticUniverse([event]),
            market_data=StaticMarketData(books),
        )
        await engine.run_cycle()
        text = log_path.read_text(encoding="utf-8")
        line = text.strip().splitlines()[0]
        payload = json.loads(line)
        assert "summary" in payload and "last_cycle" in payload
        assert payload["summary"]["equity"] is not None
        assert payload["last_cycle"]["opportunities"] == 0
    finally:
        os.unlink(path)


@pytest.mark.anyio
async def test_run_cycle_includes_book_source_counts():
    event, books = _complete_set_event()
    settings = _settings(max_opportunities_per_cycle=1, max_basket_notional=3.75)
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
        path = handle.name
    try:
        legacy_db = Database(path=path)
        repository = ArbRepository(path=path)
        engine = ArbEngine(
            config=settings,
            legacy_db=legacy_db,
            repository=repository,
            universe=StaticUniverse([event]),
            market_data=StaticMarketData(books),
        )
        summary = await engine.run_cycle()
        assert summary["books_clob"] == 0
        assert summary["books_synthetic"] == len(books)
        assert summary["books_other"] == 0
        assert summary["opportunities"] == 0
        assert summary["executed"] == 0
    finally:
        os.unlink(path)


def test_complete_set_scanner_finds_arbitrage():
    event, books = _complete_set_event()
    scanner = OpportunityScanner(_settings())
    opportunities = scanner.scan([event], books)

    complete_set = [opp for opp in opportunities if opp.strategy_type == "complete_set"]
    assert len(complete_set) == 1
    assert complete_set[0].net_edge_bps > 0
    assert len(complete_set[0].legs) == 3


def test_complete_set_scanner_skips_extreme_leg_spread():
    event, books = _complete_set_event()
    _set_book_source(books, "clob")
    y1 = books["y1"]
    y1.best_bid = 0.01
    y1.best_ask = 0.99
    y1.bids = [PriceLevel(0.01, 100)]
    y1.asks = [PriceLevel(0.99, 100)]
    scanner = OpportunityScanner(_settings(max_arb_leg_spread_bps=400.0))
    opportunities = scanner.scan([event], books)
    complete_set = [opp for opp in opportunities if opp.strategy_type == "complete_set"]
    assert complete_set == []


def test_neg_risk_scanner_finds_conversion_trade():
    event, books = _neg_risk_event()
    scanner = OpportunityScanner(_settings())
    opportunities = scanner.scan([event], books)

    neg_risk = [opp for opp in opportunities if opp.strategy_type == "neg_risk_conversion"]
    assert len(neg_risk) >= 1
    assert neg_risk[0].convert_from_market_id == "m1"
    assert neg_risk[0].expected_profit > 0


def test_paper_exchange_converts_and_realizes_profit():
    event, books = _neg_risk_event()
    exchange = PaperExchange(_settings())
    exchange.set_starting_cash(100.0)
    exchange.update_universe([event])
    exchange.sync_books(books)

    order, fills = exchange.place_order(
        OrderIntent(
            basket_id="b1",
            opportunity_id="o1",
            token_id="n1",
            market_id="m1",
            event_id=event.event_id,
            contract_side="NO",
            side="BUY",
            price=0.30,
            size=5.0,
            order_type="fok",
            maker_or_taker="taker",
        )
    )
    assert order.status == "filled"
    assert sum(fill.size for fill in fills) == pytest.approx(5.0)

    outputs = exchange.convert_neg_risk(event, "m1", 5.0)
    assert len(outputs) == 2
    assert exchange.get_positions()[0].contract_side == "YES"

    sell_1, _ = exchange.place_order(
        OrderIntent(
            basket_id="b1",
            opportunity_id="o1",
            token_id="y2",
            market_id="m2",
            event_id=event.event_id,
            contract_side="YES",
            side="SELL",
            price=0.24,
            size=5.0,
            order_type="fok",
            maker_or_taker="taker",
        )
    )
    sell_2, _ = exchange.place_order(
        OrderIntent(
            basket_id="b1",
            opportunity_id="o1",
            token_id="y3",
            market_id="m3",
            event_id=event.event_id,
            contract_side="YES",
            side="SELL",
            price=0.20,
            size=5.0,
            order_type="fok",
            maker_or_taker="taker",
        )
    )

    assert sell_1.status == "filled"
    assert sell_2.status == "filled"
    assert exchange.realized_pnl == pytest.approx(0.7, abs=1e-6)
    assert exchange.get_positions() == []


def test_fok_rejection_does_not_consume_book():
    event, books = _neg_risk_event()
    books["n1"] = TokenBook(
        "n1",
        event_time(),
        0.29,
        0.30,
        bids=[PriceLevel(0.29, 2.0)],
        asks=[PriceLevel(0.30, 2.0)],
    )
    exchange = PaperExchange(_settings())
    exchange.set_starting_cash(100.0)
    exchange.update_universe([event])
    exchange.sync_books(books)

    order, fills = exchange.place_order(
        OrderIntent(
            basket_id="b1",
            opportunity_id="o1",
            token_id="n1",
            market_id="m1",
            event_id=event.event_id,
            contract_side="NO",
            side="BUY",
            price=0.30,
            size=5.0,
            order_type="fok",
            maker_or_taker="taker",
        )
    )

    assert order.status == "rejected"
    assert fills == []
    assert exchange.get_positions() == []
    remaining_book = exchange.book_for_token("n1")
    assert remaining_book is not None
    assert remaining_book.asks[0].size == pytest.approx(2.0)


def test_neg_risk_scanner_skips_augmented_and_other_outcomes():
    event, books = _neg_risk_event()
    event.neg_risk_augmented = True
    event.markets[2].outcome_name = "Other"
    scanner = OpportunityScanner(_settings())

    opportunities = scanner.scan([event], books)

    neg_risk = [opp for opp in opportunities if opp.strategy_type == "neg_risk_conversion"]
    assert neg_risk == []


@pytest.mark.anyio
async def test_engine_executes_complete_set_and_settles():
    event, books = _complete_set_event()
    _set_book_source(books, "clob")
    settings = _settings(max_opportunities_per_cycle=1, max_basket_notional=3.75)

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
        path = handle.name
    try:
        legacy_db = Database(path=path)
        repository = ArbRepository(path=path)
        engine = ArbEngine(
            config=settings,
            legacy_db=legacy_db,
            repository=repository,
            universe=StaticUniverse([event]),
            market_data=StaticMarketData(books),
        )

        summary = await engine.run_cycle()
        assert summary["executed"] == 1
        assert len(engine.exchange.get_positions()) == 3

        settlement = await engine.settle_event(event.event_id, "m2")
        assert settlement["pnl_realized"] == pytest.approx(1.25, abs=1e-6)
        assert engine.exchange.get_positions() == []
    finally:
        os.unlink(path)


@pytest.mark.anyio
async def test_engine_auto_settles_when_event_leaves_active_universe():
    event, books = _complete_set_event()
    _set_book_source(books, "clob")
    settings = _settings(max_opportunities_per_cycle=1, max_basket_notional=3.75)
    resolved_event = ArbEvent(
        event_id=event.event_id,
        title=event.title,
        status="resolved",
        markets=event.markets,
    )

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
        path = handle.name
    try:
        legacy_db = Database(path=path)
        repository = ArbRepository(path=path)
        engine = ArbEngine(
            config=settings,
            legacy_db=legacy_db,
            repository=repository,
            universe=StaticUniverse(
                refresh_sequence=[[event], []],
                resolution_map={event.event_id: (resolved_event, "m2", "test-resolution")},
            ),
            market_data=StaticMarketData(books),
        )

        first = await engine.run_cycle()
        assert first["executed"] == 1
        assert len(engine.exchange.get_positions()) == 3

        second = await engine.run_cycle()
        assert second["auto_settled"] == 1
        assert engine.exchange.get_positions() == []
        baskets = engine.baskets_snapshot()
        assert baskets[0]["status"] == "SETTLED"
    finally:
        os.unlink(path)


@pytest.mark.anyio
async def test_engine_rejects_invalid_settlement_market():
    event, books = _complete_set_event()
    _set_book_source(books, "clob")
    settings = _settings(max_opportunities_per_cycle=1, max_basket_notional=3.75)

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
        path = handle.name
    try:
        legacy_db = Database(path=path)
        repository = ArbRepository(path=path)
        engine = ArbEngine(
            config=settings,
            legacy_db=legacy_db,
            repository=repository,
            universe=StaticUniverse([event]),
            market_data=StaticMarketData(books),
        )

        await engine.run_cycle()

        with pytest.raises(ValueError):
            await engine.settle_event(event.event_id, "bad-market")
    finally:
        os.unlink(path)


@pytest.mark.anyio
async def test_engine_restores_runtime_state_after_restart():
    event, books = _complete_set_event()
    _set_book_source(books, "clob")
    settings = _settings(max_opportunities_per_cycle=1, max_basket_notional=3.75)

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
        path = handle.name
    try:
        legacy_db = Database(path=path)
        repository = ArbRepository(path=path)
        engine = ArbEngine(
            config=settings,
            legacy_db=legacy_db,
            repository=repository,
            universe=StaticUniverse([event]),
            market_data=StaticMarketData(books),
        )
        await engine.run_cycle()

        restarted = ArbEngine(
            config=settings,
            legacy_db=Database(path=path),
            repository=ArbRepository(path=path),
            universe=StaticUniverse([event]),
            market_data=StaticMarketData(books),
        )
        await restarted.initialize()

        assert restarted.exchange.cash == pytest.approx(96.25, abs=1e-6)
        assert len(restarted.exchange.get_positions()) == 3
    finally:
        os.unlink(path)


@pytest.mark.anyio
async def test_control_api_requires_token_when_configured():
    settings = _settings(control_api_token="secret-token")

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
        path = handle.name
    try:
        legacy_db = Database(path=path)
        repository = ArbRepository(path=path)
        engine = ArbEngine(
            config=settings,
            legacy_db=legacy_db,
            repository=repository,
            universe=StaticUniverse([]),
            market_data=StaticMarketData({}),
        )
        await engine.initialize()
        control = ArbControlAPI(settings, engine, legacy_db, repository)

        app = web.Application(middlewares=[cors_middleware, control.auth_middleware])
        app.router.add_get("/summary", control._summary)
        app.router.add_get("/health", control._health)

        async with TestClient(TestServer(app)) as client:
            unauthorized = await client.get("/summary")
            assert unauthorized.status == 401

            authorized = await client.get("/summary", headers={"X-Control-Token": "secret-token"})
            assert authorized.status == 200

            health = await client.get("/health")
            assert health.status == 200
    finally:
        os.unlink(path)


@pytest.mark.anyio
async def test_full_engine_replay_reproduces_recorded_cycles():
    event, books = _complete_set_event()
    _set_book_source(books, "clob")
    settings = _settings(max_opportunities_per_cycle=1, max_basket_notional=3.75)
    resolved_event = ArbEvent(
        event_id=event.event_id,
        title=event.title,
        status="resolved",
        markets=event.markets,
    )

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
        path = handle.name
    try:
        legacy_db = Database(path=path)
        repository = ArbRepository(path=path)
        engine = ArbEngine(
            config=settings,
            legacy_db=legacy_db,
            repository=repository,
            universe=StaticUniverse(
                refresh_sequence=[[event], []],
                resolution_map={event.event_id: (resolved_event, "m2", "test-resolution")},
            ),
            market_data=StaticMarketData(books),
        )
        await engine.initialize()

        records = []
        for cycle_index in (1, 2):
            await engine.run_cycle()
            snapshot = engine.cycle_snapshot()
            snapshot["record_type"] = "cycle"
            snapshot["schema_version"] = 3
            snapshot["cycle_index"] = cycle_index
            records.append(snapshot)

        replay_result = await replay_cycle_records(records, settings)

        assert replay_result["mismatch_count"] == 0
        assert len(replay_result["cycles"]) == 2
        assert all(cycle["matched"] for cycle in replay_result["cycles"])
    finally:
        os.unlink(path)


@pytest.mark.anyio
async def test_committed_replay_fixtures_replay_cleanly():
    """Regression guard: JSONL under tests/fixtures/replay/ must replay with zero mismatches."""
    root = os.path.join(os.path.dirname(__file__), "fixtures", "replay")
    if not os.path.isdir(root):
        pytest.skip("no tests/fixtures/replay directory")

    jsonl_files = sorted(
        f for f in os.listdir(root) if f.endswith(".jsonl")
    )
    if not jsonl_files:
        pytest.skip("no *.jsonl replay fixtures")

    for name in jsonl_files:
        jsonl_path = os.path.join(root, name)
        stem = os.path.splitext(name)[0]
        meta_path = os.path.join(root, f"{stem}.meta.json")
        assert os.path.isfile(meta_path), f"missing meta for {name}: {meta_path}"
        meta = json.loads(open(meta_path, encoding="utf-8").read())
        inner = meta.get("settings")
        assert isinstance(inner, dict), f"{meta_path} must contain settings dict"
        config = Settings(_env_file=None, **inner)
        records = load_cycle_records(jsonl_path)
        replay_result = await replay_cycle_records(records, config)
        assert replay_result["mismatch_count"] == 0, (
            f"{name}: replay mismatches — {replay_result['cycles']}"
        )


@pytest.mark.anyio
async def test_arb_engine_add_funds_includes_new_bankroll_alias():
    settings = _settings(initial_bankroll=100.0)
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
        path = handle.name
    try:
        legacy_db = Database(path=path)
        repository = ArbRepository(path=path)
        engine = ArbEngine(
            config=settings,
            legacy_db=legacy_db,
            repository=repository,
            universe=StaticUniverse([]),
            market_data=StaticMarketData({}),
        )
        await engine.initialize()
        result = await engine.add_funds(25.0, "top-up")
        assert result["ok"] is True
        assert result["new_bankroll"] == pytest.approx(result["new_equity"])
        assert result["new_equity"] > 100.0
    finally:
        os.unlink(path)


@pytest.mark.anyio
async def test_arb_control_legacy_compat_routes():
    settings = _settings()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
        path = handle.name
    try:
        legacy_db = Database(path=path)
        repository = ArbRepository(path=path)
        engine = ArbEngine(
            config=settings,
            legacy_db=legacy_db,
            repository=repository,
            universe=StaticUniverse([]),
            market_data=StaticMarketData({}),
        )
        await engine.initialize()
        control = ArbControlAPI(settings, engine, legacy_db, repository)
        app = web.Application(middlewares=[cors_middleware, control.auth_middleware])
        app.router.add_get("/stats", control._stats_compat)
        app.router.add_get("/stats/assets", control._stats_assets_compat)
        app.router.add_get("/trades", control._trades_compat)
        app.router.add_post("/halt/asset", control._halt_asset_compat)
        app.router.add_post("/resume/asset", control._resume_asset_compat)

        async with TestClient(TestServer(app)) as client:
            stats = await (await client.get("/stats")).json()
            assert stats["runtime"] == "structural_arb"
            assert stats["bankroll"] == stats["paper_bankroll"]

            assets = await (await client.get("/stats/assets")).json()
            assert set(assets.keys()) >= {"BTC", "ETH", "SOL", "XRP"}
            assert assets["BTC"]["trades"] == 0

            trades = await (await client.get("/trades")).json()
            assert trades == []

            halt_resp = await client.post("/halt/asset", json={"asset": "SOL"})
            assert halt_resp.status == 200
            assert engine.risk.halted

            resume_resp = await client.post("/resume/asset", json={"asset": "SOL"})
            assert resume_resp.status == 200
            assert not engine.risk.halted

            bad = await client.post("/halt/asset", json={"asset": "NOPE"})
            assert bad.status == 400
    finally:
        os.unlink(path)


def test_scanner_capital_required_matches_exchange_cash_for_complete_set():
    """Scanner sizing uses the same book walk + taker fees as PaperExchange."""
    event = ArbEvent(
        event_id="e-depth",
        title="Depth test",
        markets=[
            OutcomeMarket(
                "e-depth", "m1", "?", "A", "y1", "n1", tick_size=0.01, fees_enabled=True
            ),
            OutcomeMarket(
                "e-depth", "m2", "?", "B", "y2", "n2", tick_size=0.01, fees_enabled=True
            ),
            OutcomeMarket(
                "e-depth", "m3", "?", "C", "y3", "n3", tick_size=0.01, fees_enabled=True
            ),
        ],
    )
    ts = event_time()
    books = {
        "y1": TokenBook(
            "y1",
            ts,
            0.28,
            0.30,
            bids=[PriceLevel(0.28, 100)],
            asks=[PriceLevel(0.30, 2), PriceLevel(0.31, 100)],
            fees_enabled=True,
        ),
        "y2": TokenBook(
            "y2", ts, 0.24, 0.25, bids=[PriceLevel(0.24, 100)], asks=[PriceLevel(0.25, 100)], fees_enabled=True
        ),
        "y3": TokenBook(
            "y3", ts, 0.19, 0.20, bids=[PriceLevel(0.19, 100)], asks=[PriceLevel(0.20, 100)], fees_enabled=True
        ),
        "n1": TokenBook("n1", ts, 0.69, 0.70, bids=[PriceLevel(0.69, 100)], asks=[PriceLevel(0.70, 100)]),
        "n2": TokenBook("n2", ts, 0.74, 0.75, bids=[PriceLevel(0.74, 100)], asks=[PriceLevel(0.75, 100)]),
        "n3": TokenBook("n3", ts, 0.79, 0.80, bids=[PriceLevel(0.79, 100)], asks=[PriceLevel(0.80, 100)]),
    }
    cfg = Settings(
        _env_file=None,
        paper_trade=True,
        initial_bankroll=1_000_000.0,
        max_basket_notional=10_000.0,
        min_event_liquidity=0.0,
        min_outcomes_per_event=2,
        min_complete_set_edge_bps=1.0,
        min_neg_risk_edge_bps=10.0,
        allow_taker_execution=True,
        paper_taker_fee_bps=100.0,
        paper_maker_rebate_bps=0.0,
        opportunity_cooldown_seconds=0,
        max_total_open_baskets=10,
        max_opportunities_per_cycle=5,
        max_event_exposure_pct=1.0,
        daily_loss_cap=0.99,
        arb_poll_seconds=1,
        max_tracked_events=100,
    )
    complete = [o for o in OpportunityScanner(cfg).scan([event], books) if o.strategy_type == "complete_set"]
    assert len(complete) == 1
    opp = complete[0]

    exchange = PaperExchange(cfg)
    exchange.set_starting_cash(1_000_000.0)
    exchange.update_universe([event])
    exchange.sync_books(books)
    cash0 = exchange.cash

    for leg in opp.legs:
        intent = OrderIntent(
            basket_id="b1",
            opportunity_id=opp.opportunity_id,
            token_id=leg.token_id,
            market_id=leg.market_id,
            event_id=opp.event_id,
            contract_side=leg.position_side,
            side=leg.action,
            price=leg.price,
            size=leg.size,
            order_type="fok",
            maker_or_taker="taker",
            fees_enabled=leg.fees_enabled,
            metadata={},
        )
        order, fills = exchange.place_order(intent)
        assert order.status == "filled", order.reason
        assert order.filled_size == pytest.approx(leg.size)

    assert opp.capital_required == pytest.approx(cash0 - exchange.cash, rel=0, abs=1e-4)


def test_cycle_diagnostics_reports_structural_counts_and_raw_edges():
    event, books = _complete_set_event()
    books = _set_book_source(books, "clob")
    nr_event, nr_books = _neg_risk_event()
    nr_books = _set_book_source(nr_books, "clob")
    merged = {**books, **nr_books}
    scanner = OpportunityScanner(_settings(min_complete_set_edge_bps=10_000.0, min_neg_risk_edge_bps=10_000.0))
    diag = scanner.cycle_diagnostics([event, nr_event], merged)
    assert diag["events_in_universe"] == 2
    assert diag["neg_risk_tagged_events"] == 1
    assert diag["complete_set_priceable_events"] >= 1
    assert diag["max_raw_complete_set_edge_bps"] is not None
    assert scanner.scan([event, nr_event], merged) == []
