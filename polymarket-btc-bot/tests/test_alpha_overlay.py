from __future__ import annotations

from src.alpha import overlay as overlay_mod
from src.arb.models import ArbEvent, OutcomeMarket
from src.config import Settings


def _evt(n_markets: int) -> ArbEvent:
    markets = []
    for i in range(n_markets):
        markets.append(
            OutcomeMarket(
                event_id="e1",
                market_id=f"m{i}",
                question="Q",
                outcome_name="Yes" if i == 0 else "No",
                yes_token_id="y1" if i == 0 else "y2",
                no_token_id="n1" if i == 0 else "n2",
            )
        )
    return ArbEvent(
        event_id="e1",
        title="Test",
        markets=markets if n_markets else [],
    )


def test_overlay_market_from_event_single_binary():
    """_overlay_market_from_event returns None for empty events, picks best market otherwise."""
    from src.arb.models import PriceLevel, TokenBook
    from datetime import datetime, timezone

    def _book(bid: float, ask: float, token_id: str) -> TokenBook:
        now = datetime.now(timezone.utc)
        return TokenBook(
            token_id=token_id,
            timestamp=now,
            best_bid=bid,
            best_ask=ask,
            bids=[PriceLevel(price=bid, size=100.0)],
            asks=[PriceLevel(price=ask, size=100.0)],
            source="clob",
        )

    real_books = {
        "y1": _book(0.48, 0.52, "y1"),
        "y2": _book(0.38, 0.42, "y2"),
    }
    # Empty event → None
    assert overlay_mod._overlay_market_from_event(_evt(0), real_books, 0.14) is None
    # Single-binary event with valid book → returns market
    result = overlay_mod._overlay_market_from_event(_evt(1), real_books, 0.14)
    assert result is not None
    m, query, bk = result
    assert m.yes_token_id == "y1"
    # Multi-outcome event with valid books → returns best market by liquidity
    result2 = overlay_mod._overlay_market_from_event(_evt(2), real_books, 0.14)
    assert result2 is not None


def test_overlay_respects_disabled_config():
    """Smoke: disabled overlay returns immediately without touching exchange."""
    from src.arb.engine import ArbEngine
    from src.arb.repository import ArbRepository
    from src.storage.db import Database

    import tempfile
    import os

    cfg = Settings(
        _env_file=None,
        enable_directional_overlay=False,
        control_api_port=18766,
        log_level="WARNING",
    )
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as h:
        path = h.name
    try:
        db = Database(path=path)
        repo = ArbRepository(path=path)
        engine = ArbEngine(config=cfg, legacy_db=db, repository=repo)
        import asyncio

        async def run():
            await engine.initialize()
            await overlay_mod.run_directional_overlay(engine, [], {}, {}, 0, 0)

        asyncio.run(run())
    finally:
        os.unlink(path)
