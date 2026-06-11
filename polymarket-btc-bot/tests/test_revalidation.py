"""Pre-execution fresh-book re-validation: live execution must re-price an opportunity against a
freshly fetched book and abort if the edge/depth decayed, instead of placing legs on a stale snapshot."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.arb.engine import ArbEngine
from src.arb.exchange import PaperExchange
from src.arb.models import ArbEvent, OutcomeMarket, PriceLevel, TokenBook
from src.arb.pricing import OpportunityScanner
from src.arb.repository import ArbRepository
from src.config import Settings
from src.storage.db import Database


def _ts() -> datetime:
    return datetime.now(timezone.utc)


def _settings(**overrides) -> Settings:
    defaults = dict(
        paper_trade=True,
        initial_bankroll=100.0,
        control_api_port=18790,
        log_level="WARNING",
        min_complete_set_edge_bps=10.0,
        min_neg_risk_edge_bps=10.0,
        allow_taker_execution=True,
        paper_taker_fee_bps=0.0,
        paper_spread_penalty_bps=0.0,
        max_basket_notional=3.75,
        max_opportunities_per_cycle=1,
        max_arb_leg_spread_bps=0.0,
        arb_min_expected_profit_usd=0.0,
        arb_max_plausible_edge_bps=0.0,
        arb_min_leg_touch_notional_usd=0.0,
        arb_live_execution=True,
        arb_revalidate_with_fresh_books=True,
    )
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def _event() -> ArbEvent:
    return ArbEvent(
        event_id="event-1",
        title="Who wins?",
        neg_risk=True,
        markets=[
            OutcomeMarket("event-1", "m1", "A?", "A", "y1", "n1", tick_size=0.01),
            OutcomeMarket("event-1", "m2", "B?", "B", "y2", "n2", tick_size=0.01),
        ],
    )


def _books(ask: float) -> dict[str, TokenBook]:
    b = {
        "y1": TokenBook("y1", _ts(), ask - 0.01, ask, bids=[PriceLevel(ask - 0.01, 100)], asks=[PriceLevel(ask, 100)]),
        "y2": TokenBook("y2", _ts(), ask - 0.01, ask, bids=[PriceLevel(ask - 0.01, 100)], asks=[PriceLevel(ask, 100)]),
    }
    for book in b.values():
        book.source = "clob"
    return b


class _FakeMarketData:
    def __init__(self, books):
        self._books = books

    async def refresh(self, events, on_progress=None):
        del events
        if on_progress:
            on_progress(100.0)
        return self._books


async def _engine(settings, fresh_books, tmp_path) -> ArbEngine:
    db_path = str(tmp_path / "reval.db")
    legacy_db = Database(path=db_path)
    repository = ArbRepository(path=db_path)
    await legacy_db.init()
    await repository.init()
    event = _event()
    exchange = PaperExchange(settings)
    exchange.update_universe([event])
    engine = ArbEngine(
        config=settings,
        legacy_db=legacy_db,
        repository=repository,
        market_data=_FakeMarketData(fresh_books),
        exchange=exchange,
    )
    engine._events[event.event_id] = event
    return engine


@pytest.mark.anyio
async def test_revalidation_aborts_when_fresh_edge_gone(tmp_path):
    settings = _settings()
    event = _event()
    # Opportunity priced on cheap books (0.30 + 0.30 = 0.60 < 1.0 -> edge).
    opp = OpportunityScanner(settings).scan([event], _books(0.30), max_basket_notional=3.75)
    assert opp and opp[0].strategy_type == "complete_set"

    # Fresh books are expensive (0.55 + 0.55 = 1.10 > 1.0 -> no edge): execution must abort.
    engine = await _engine(settings, _books(0.55), tmp_path)
    result = await engine._execute_opportunity(opp[0], basket_cap=3.75)
    assert result is None


@pytest.mark.anyio
async def test_revalidation_executes_when_fresh_edge_persists(tmp_path):
    settings = _settings()
    event = _event()
    opp = OpportunityScanner(settings).scan([event], _books(0.30), max_basket_notional=3.75)
    assert opp

    # Fresh books still show the edge -> the (re-priced) basket executes.
    engine = await _engine(settings, _books(0.30), tmp_path)
    result = await engine._execute_opportunity(opp[0], basket_cap=3.75)
    assert result is not None
    assert result.status == "OPEN"
