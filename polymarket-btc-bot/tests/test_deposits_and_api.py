"""
Unit tests for:
  1. Database deposits table (insert_deposit, get_total_deposits, get_deposits)
  2. RiskManager.add_funds()
  3. ControlAPI CORS headers + /funds/add endpoint
"""
from __future__ import annotations

import asyncio
import os
import tempfile

import pytest


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_settings(**overrides):
    """Return a minimal Settings instance that doesn't need a real .env."""
    from src.config import Settings

    defaults = dict(
        paper_trade=True,
        initial_bankroll=300.0,
        edge_threshold=0.07,
        entry_window_seconds=30,
        min_seconds_remaining=3,
        max_bet_fraction=0.06,
        kelly_fraction=0.20,
        target_edge_for_max_size=0.12,
        min_bet_usd=1.0,
        daily_loss_cap=0.10,
        min_market_liquidity=750.0,
        max_concurrent_positions=3,
        max_reposts_per_window=4,
        repost_stale_ticks=2,
        cancel_at_seconds_remaining=6,
        max_maker_aggression_ticks=3,
        maker_rebate_bps_assumption=0.0,
        max_positions_per_asset=1,
        max_total_exposure_pct=0.40,
        control_api_port=18765,
        log_level="WARNING",
    )
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


@pytest.fixture
def tmp_db():
    """Create a real Database backed by a temp file, initialised and ready."""
    from src.storage.db import Database

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name

    db = Database(path=path)
    asyncio.run(db.init())
    yield db

    os.unlink(path)


# ── Database tests ────────────────────────────────────────────────────────────

class TestDatabaseDeposits:
    @pytest.mark.anyio
    async def test_initial_total_deposits_is_zero(self, tmp_db):
        total = await tmp_db.get_total_deposits()
        assert total == 0.0

    @pytest.mark.anyio
    async def test_insert_and_sum_deposits(self, tmp_db):
        await tmp_db.insert_deposit(100.0, "first")
        await tmp_db.insert_deposit(50.50, "second")
        total = await tmp_db.get_total_deposits()
        assert abs(total - 150.50) < 0.001

    @pytest.mark.anyio
    async def test_get_deposits_returns_records(self, tmp_db):
        await tmp_db.insert_deposit(200.0, "big deposit")
        rows = await tmp_db.get_deposits()
        assert len(rows) == 1
        assert rows[0]["amount"] == 200.0
        assert rows[0]["note"] == "big deposit"
        assert "timestamp" in rows[0]

    @pytest.mark.anyio
    async def test_get_deposits_newest_first(self, tmp_db):
        await tmp_db.insert_deposit(10.0, "first")
        await tmp_db.insert_deposit(20.0, "second")
        rows = await tmp_db.get_deposits()
        # Newest first: second deposit should come first
        assert rows[0]["amount"] == 20.0


# ── RiskManager tests ─────────────────────────────────────────────────────────

class TestRiskManagerAddFunds:
    @pytest.mark.anyio
    async def test_add_funds_increases_bankroll(self, tmp_db):
        from src.risk.manager import RiskManager
        config = _make_settings(initial_bankroll=300.0)
        risk = RiskManager(config)
        await risk.load_from_db(tmp_db)

        starting = float(risk.current_bankroll)
        await risk.add_funds(100.0, "test deposit", tmp_db)

        assert float(risk.current_bankroll) == pytest.approx(starting + 100.0)

    @pytest.mark.anyio
    async def test_add_funds_persists_to_db(self, tmp_db):
        from src.risk.manager import RiskManager
        config = _make_settings(initial_bankroll=300.0)
        risk = RiskManager(config)
        await risk.load_from_db(tmp_db)

        await risk.add_funds(75.0, "persistence test", tmp_db)

        total = await tmp_db.get_total_deposits()
        assert total == pytest.approx(75.0)

    @pytest.mark.anyio
    async def test_add_funds_rejects_zero(self, tmp_db):
        from src.risk.manager import RiskManager
        config = _make_settings(initial_bankroll=300.0)
        risk = RiskManager(config)
        await risk.load_from_db(tmp_db)

        with pytest.raises(ValueError):
            await risk.add_funds(0.0, "zero", tmp_db)

    @pytest.mark.anyio
    async def test_add_funds_rejects_negative(self, tmp_db):
        from src.risk.manager import RiskManager
        config = _make_settings(initial_bankroll=300.0)
        risk = RiskManager(config)
        await risk.load_from_db(tmp_db)

        with pytest.raises(ValueError):
            await risk.add_funds(-50.0, "negative", tmp_db)

    @pytest.mark.anyio
    async def test_load_from_db_uses_deposits_when_no_session(self, tmp_db):
        """If there's no daily_summary yet, bankroll should reflect deposits."""
        from src.risk.manager import RiskManager
        # First, record a deposit directly in DB
        await tmp_db.insert_deposit(500.0, "initial")

        config = _make_settings(initial_bankroll=300.0)  # config says 300
        risk = RiskManager(config)
        await risk.load_from_db(tmp_db)

        # Should override config.initial_bankroll with the recorded 500
        assert float(risk.current_bankroll) == pytest.approx(500.0)


# ── ControlAPI tests ──────────────────────────────────────────────────────────

class TestControlAPICORS:
    @pytest.mark.anyio
    async def test_cors_header_present_on_health(self, tmp_db):
        """Ensure Access-Control-Allow-Origin is set on all responses."""
        from aiohttp.test_utils import TestClient, TestServer
        import aiohttp.web as web
        from src.control.api import ControlAPI, cors_middleware
        from src.risk.manager import RiskManager

        config = _make_settings()
        risk = RiskManager(config)

        ctrl = ControlAPI(config, risk, tmp_db)
        app = web.Application(middlewares=[cors_middleware])
        app.router.add_get("/health", ctrl._health)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/health")
            assert resp.headers.get("Access-Control-Allow-Origin") == "*"

    @pytest.mark.anyio
    async def test_add_funds_endpoint(self, tmp_db):
        """POST /funds/add should update bankroll and return new_bankroll."""
        from aiohttp.test_utils import TestClient, TestServer
        import aiohttp.web as web
        from src.control.api import ControlAPI, cors_middleware
        from src.risk.manager import RiskManager

        config = _make_settings(initial_bankroll=200.0)
        risk = RiskManager(config)
        await risk.load_from_db(tmp_db)

        ctrl = ControlAPI(config, risk, tmp_db)
        app = web.Application(middlewares=[cors_middleware])
        app.router.add_post("/funds/add", ctrl._add_funds)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/funds/add", json={"amount": 50.0, "note": "e2e test"})
            data = await resp.json()

        assert data["ok"] is True
        assert data["new_bankroll"] == pytest.approx(250.0)

    @pytest.mark.anyio
    async def test_add_funds_rejects_zero(self, tmp_db):
        from aiohttp.test_utils import TestClient, TestServer
        import aiohttp.web as web
        from src.control.api import ControlAPI, cors_middleware
        from src.risk.manager import RiskManager

        config = _make_settings()
        risk = RiskManager(config)

        ctrl = ControlAPI(config, risk, tmp_db)
        app = web.Application(middlewares=[cors_middleware])
        app.router.add_post("/funds/add", ctrl._add_funds)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/funds/add", json={"amount": 0})
            assert resp.status == 400
