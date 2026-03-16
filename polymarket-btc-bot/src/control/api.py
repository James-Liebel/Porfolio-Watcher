"""Tiny aiohttp REST API on localhost for OpenClaw and external control."""
from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import aiohttp.web as web
import structlog

if TYPE_CHECKING:
    from ..config import Settings
    from ..risk.manager import RiskManager
    from ..storage.db import Database

logger = structlog.get_logger(__name__)


class ControlAPI:
    def __init__(
        self,
        config: "Settings",
        risk: "RiskManager",
        db: "Database",
    ) -> None:
        self._config = config
        self._risk = risk
        self._db = db

    # ── Handlers ─────────────────────────────────────────────────────────

    async def _health(self, request: web.Request) -> web.Response:
        stats = self._risk.get_stats()
        body = {
            "status": "ok",
            "trading": not stats["trading_halted"],
            "bankroll": stats["bankroll"],
            "daily_pnl": stats["daily_pnl"],
            "paper_trade": self._config.paper_trade,
        }
        return web.Response(
            text=json.dumps(body), content_type="application/json"
        )

    async def _stats(self, request: web.Request) -> web.Response:
        body = self._risk.get_stats()
        return web.Response(
            text=json.dumps(body), content_type="application/json"
        )

    async def _stats_assets(self, request: web.Request) -> web.Response:
        """Per-asset breakdown: trades, wins, PnL today + open positions."""
        risk_asset_stats = self._risk.get_asset_stats()
        db_asset_stats = await self._db.get_asset_trade_stats()

        body: dict = {}
        for asset in ("BTC", "ETH", "SOL", "XRP"):
            db_row = db_asset_stats.get(asset, {})
            risk_row = risk_asset_stats.get(asset, {})
            body[asset] = {
                "trades": db_row.get("trades", 0),
                "wins": db_row.get("wins", 0),
                "pnl": round(db_row.get("pnl", 0.0), 4),
                "open": risk_row.get("open", 0),
                "halted": risk_row.get("halted", False),
            }
        return web.Response(
            text=json.dumps(body), content_type="application/json"
        )

    async def _halt(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            reason = payload.get("reason", "API halt")
        except Exception:
            reason = "API halt"
        await self._risk.halt_trading(reason)
        return web.Response(
            text=json.dumps({"ok": True, "reason": reason}),
            content_type="application/json",
        )

    async def _resume(self, request: web.Request) -> web.Response:
        await self._risk.resume_trading()
        return web.Response(
            text=json.dumps({"ok": True}), content_type="application/json"
        )

    async def _halt_asset(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            asset = payload.get("asset", "").upper()
        except Exception:
            asset = ""
        if asset not in ("BTC", "ETH", "SOL", "XRP"):
            return web.Response(
                text=json.dumps({"ok": False, "error": "invalid asset"}),
                content_type="application/json",
                status=400,
            )
        await self._risk.halt_asset(asset)
        return web.Response(
            text=json.dumps({"ok": True, "asset": asset, "halted": True}),
            content_type="application/json",
        )

    async def _resume_asset(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            asset = payload.get("asset", "").upper()
        except Exception:
            asset = ""
        if asset not in ("BTC", "ETH", "SOL", "XRP"):
            return web.Response(
                text=json.dumps({"ok": False, "error": "invalid asset"}),
                content_type="application/json",
                status=400,
            )
        await self._risk.resume_asset(asset)
        return web.Response(
            text=json.dumps({"ok": True, "asset": asset, "halted": False}),
            content_type="application/json",
        )

    async def _trades(self, request: web.Request) -> web.Response:
        limit = int(request.rel_url.query.get("limit", 20))
        trades = await self._db.get_all_trades(limit=limit)
        return web.Response(
            text=json.dumps(trades), content_type="application/json"
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        app = web.Application()
        app.router.add_get("/health", self._health)
        app.router.add_get("/stats", self._stats)
        app.router.add_get("/stats/assets", self._stats_assets)
        app.router.add_post("/halt", self._halt)
        app.router.add_post("/resume", self._resume)
        app.router.add_post("/halt/asset", self._halt_asset)
        app.router.add_post("/resume/asset", self._resume_asset)
        app.router.add_get("/trades", self._trades)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", self._config.control_api_port)
        await site.start()
        logger.info(
            "control_api.started",
            port=self._config.control_api_port,
        )
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await runner.cleanup()
