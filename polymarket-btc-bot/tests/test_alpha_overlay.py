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


def test_single_binary_requires_one_market_with_tokens():
    assert overlay_mod._single_binary_market(_evt(0)) is None
    assert overlay_mod._single_binary_market(_evt(2)) is None
    one = _evt(1)
    m = overlay_mod._single_binary_market(one)
    assert m is not None
    assert m.yes_token_id == "y1"


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
