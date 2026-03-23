"""Tiny aiohttp REST API on localhost for the frontend dashboard and external control."""
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


# ── CORS middleware ───────────────────────────────────────────────────────────
# Allows the standalone frontend/index.html (opened via file://) to call the API.

@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        # Respond to CORS preflight
        return web.Response(
            status=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
            },
        )
    response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


def _json(body: object, status: int = 200) -> web.Response:
    return web.Response(
        text=json.dumps(body),
        content_type="application/json",
        status=status,
    )


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
        self._supported_assets = ("BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "AVAX", "LINK")

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
        return _json(body)

    async def _stats(self, request: web.Request) -> web.Response:
        return _json(self._risk.get_stats())

    async def _stats_assets(self, request: web.Request) -> web.Response:
        """Per-asset breakdown: trades, wins, PnL today + open positions."""
        risk_asset_stats = self._risk.get_asset_stats()
        db_asset_stats = await self._db.get_asset_trade_stats()

        body: dict = {}
        for asset in self._supported_assets:
            db_row = db_asset_stats.get(asset, {})
            risk_row = risk_asset_stats.get(asset, {})
            body[asset] = {
                "trades": db_row.get("trades", 0),
                "wins": db_row.get("wins", 0),
                "pnl": round(db_row.get("pnl", 0.0), 4),
                "open": risk_row.get("open", 0),
                "halted": risk_row.get("halted", False),
            }
        return _json(body)

    async def _halt(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            reason = payload.get("reason", "API halt")
        except Exception:
            reason = "API halt"
        await self._risk.halt_trading(reason)
        return _json({"ok": True, "reason": reason})

    async def _resume(self, request: web.Request) -> web.Response:
        await self._risk.resume_trading()
        return _json({"ok": True})

    async def _halt_asset(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            asset = payload.get("asset", "").upper()
        except Exception:
            asset = ""
        if asset not in self._supported_assets:
            return _json({"ok": False, "error": "invalid asset"}, status=400)
        await self._risk.halt_asset(asset)
        return _json({"ok": True, "asset": asset, "halted": True})

    async def _resume_asset(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            asset = payload.get("asset", "").upper()
        except Exception:
            asset = ""
        if asset not in self._supported_assets:
            return _json({"ok": False, "error": "invalid asset"}, status=400)
        await self._risk.resume_asset(asset)
        return _json({"ok": True, "asset": asset, "halted": False})

    async def _trades(self, request: web.Request) -> web.Response:
        limit = int(request.rel_url.query.get("limit", 20))
        trades = await self._db.get_all_trades(limit=limit)
        return _json(trades)

    # ── Funds endpoints ───────────────────────────────────────────────────

    async def _add_funds(self, request: web.Request) -> web.Response:
        """POST /funds/add — Add money to the bankroll.

        Body: {"amount": 100.0, "note": "initial deposit"}
        """
        try:
            payload = await request.json()
            amount = float(payload.get("amount", 0))
            note = str(payload.get("note", ""))
        except Exception:
            return _json({"ok": False, "error": "invalid request body"}, status=400)

        if amount <= 0:
            return _json({"ok": False, "error": "amount must be positive"}, status=400)

        try:
            await self._risk.add_funds(amount, note, self._db)
        except ValueError as exc:
            return _json({"ok": False, "error": str(exc)}, status=400)

        stats = self._risk.get_stats()
        return _json({
            "ok": True,
            "amount": amount,
            "note": note,
            "new_bankroll": stats["bankroll"],
        })

    async def _funds_history(self, request: web.Request) -> web.Response:
        """GET /funds/history — List all deposit records."""
        limit = int(request.rel_url.query.get("limit", 50))
        deposits = await self._db.get_deposits(limit=limit)
        return _json(deposits)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        app = web.Application(middlewares=[cors_middleware])
        app.router.add_get("/health", self._health)
        app.router.add_get("/stats", self._stats)
        app.router.add_get("/stats/assets", self._stats_assets)
        app.router.add_post("/halt", self._halt)
        app.router.add_post("/resume", self._resume)
        app.router.add_post("/halt/asset", self._halt_asset)
        app.router.add_post("/resume/asset", self._resume_asset)
        app.router.add_get("/trades", self._trades)
        app.router.add_post("/funds/add", self._add_funds)
        app.router.add_get("/funds/history", self._funds_history)

        # Handle OPTIONS preflight for all routes
        for route in list(app.router.routes()):
            if hasattr(route, "resource"):
                try:
                    app.router.add_options(
                        route.resource.canonical, lambda r: web.Response(status=204)
                    )
                except Exception:
                    pass

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
