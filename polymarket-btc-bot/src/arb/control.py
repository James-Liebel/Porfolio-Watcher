"""aiohttp control API for the structural-arbitrage runtime.

Canonical routes are GET/POST paths documented in README. Legacy directional
routes (/stats, /stats/assets, /trades, per-asset halt) are thin aliases so
older dashboards and curl snippets keep working; they map to arb semantics
(global halt, equity as bankroll, order rows as trades).
"""
from __future__ import annotations

import asyncio
import json

import aiohttp.web as web
import structlog

from ..storage.db import Database
from .engine import ArbEngine
from .repository import ArbRepository

logger = structlog.get_logger(__name__)


@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
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
        text=json.dumps(body, default=str),
        content_type="application/json",
        status=status,
    )


def _bounded_limit(value: str | None, default: int, maximum: int) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, maximum))


class ArbControlAPI:
    def __init__(self, config, engine: ArbEngine, legacy_db: Database, repository: ArbRepository) -> None:
        self._config = config
        self._engine = engine
        self._legacy_db = legacy_db
        self._repository = repository

    @web.middleware
    async def auth_middleware(self, request: web.Request, handler):
        if request.method == "OPTIONS" or request.path == "/health":
            return await handler(request)

        token = (self._config.control_api_token or "").strip()
        if not token:
            return await handler(request)

        provided = request.headers.get("X-Control-Token", "").strip()
        if not provided:
            auth = request.headers.get("Authorization", "").strip()
            if auth.lower().startswith("bearer "):
                provided = auth[7:].strip()

        if provided != token:
            return _json({"ok": False, "error": "unauthorized"}, status=401)
        return await handler(request)

    async def _health(self, request: web.Request) -> web.Response:
        return _json({"status": "ok", **self._engine.summary()})

    async def _summary(self, request: web.Request) -> web.Response:
        return _json(self._engine.summary())

    async def _events(self, request: web.Request) -> web.Response:
        return _json(self._engine.events_snapshot())

    async def _opportunities(self, request: web.Request) -> web.Response:
        limit = _bounded_limit(request.rel_url.query.get("limit"), default=50, maximum=500)
        rows = await self._repository.list_opportunities(limit=limit)
        return _json(rows)

    async def _orders(self, request: web.Request) -> web.Response:
        limit = _bounded_limit(request.rel_url.query.get("limit"), default=100, maximum=500)
        rows = await self._repository.list_orders(limit=limit)
        return _json(rows)

    async def _positions(self, request: web.Request) -> web.Response:
        return _json([position.as_dict() for position in self._engine.exchange.get_positions()])

    async def _baskets(self, request: web.Request) -> web.Response:
        limit = _bounded_limit(request.rel_url.query.get("limit"), default=100, maximum=500)
        rows = await self._repository.list_baskets(limit=limit)
        return _json(rows)

    async def _halt(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            reason = str(payload.get("reason", "API halt"))
        except Exception:
            reason = "API halt"
        await self._engine.risk.halt(reason)
        return _json({"ok": True, "reason": reason})

    async def _resume(self, request: web.Request) -> web.Response:
        await self._engine.risk.resume()
        return _json({"ok": True})

    async def _run_cycle(self, request: web.Request) -> web.Response:
        return _json(await self._engine.run_cycle())

    async def _settle(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            event_id = str(payload["event_id"])
            resolution_market_id = str(payload["resolution_market_id"])
        except Exception:
            return _json({"ok": False, "error": "event_id and resolution_market_id are required"}, status=400)

        try:
            result = await self._engine.settle_event(event_id, resolution_market_id)
        except ValueError as exc:
            return _json({"ok": False, "error": str(exc)}, status=400)
        return _json({"ok": True, **result})

    async def _add_funds(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            amount = float(payload.get("amount", 0))
            note = str(payload.get("note", ""))
        except Exception:
            return _json({"ok": False, "error": "invalid request body"}, status=400)

        try:
            result = await self._engine.add_funds(amount, note)
        except ValueError as exc:
            return _json({"ok": False, "error": str(exc)}, status=400)
        return _json(result)

    async def _funds_history(self, request: web.Request) -> web.Response:
        limit = _bounded_limit(request.rel_url.query.get("limit"), default=50, maximum=500)
        return _json(await self._legacy_db.get_deposits(limit=limit))

    async def _stats_compat(self, request: web.Request) -> web.Response:
        """Legacy /stats shape for OpenClaw and old clients (maps arb fields)."""
        s = self._engine.summary()
        return _json(
            {
                "runtime": "structural_arb",
                "trading_halted": s["trading_halted"],
                "halt_reason": s["halt_reason"],
                "bankroll": s["equity"],
                "paper_bankroll": s["equity"],
                "session_start_bankroll": s["contributed_capital"],
                "daily_pnl": s["realized_pnl"],
                "daily_trade_count": int(s.get("executed_count", 0)),
                "daily_wins": 0,
                "daily_losses": 0,
                "daily_not_filled": int(s.get("rejected_count", 0)),
                "open_positions": int(s["open_positions"]),
                "positions_by_asset": {},
                "halted_assets": {},
                "total_exposure": 0.0,
                "session_date": None,
                "open_baskets": int(s.get("open_baskets", 0)),
                "tracked_events": int(s.get("tracked_events", 0)),
            }
        )

    async def _stats_assets_compat(self, request: web.Request) -> web.Response:
        """Legacy /stats/assets: no per-crypto breakdown in arb mode; stable empty grid."""
        body: dict[str, dict[str, object]] = {}
        for asset in ("BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "AVAX", "LINK"):
            body[asset] = {
                "trades": 0,
                "wins": 0,
                "pnl": 0.0,
                "open": 0,
                "halted": False,
            }
        return _json(body)

    async def _trades_compat(self, request: web.Request) -> web.Response:
        """Legacy /trades: recent arb order rows in a directional-shaped list."""
        limit = _bounded_limit(request.rel_url.query.get("limit"), default=20, maximum=200)
        rows = await self._repository.list_orders(limit=limit)
        paper = bool(self._config.paper_trade)
        out: list[dict] = []
        for r in rows:
            status = str(r.get("status", "")).lower()
            outcome = "PENDING"
            if status == "filled":
                outcome = "WIN"
            elif status in ("cancelled", "rejected", "failed"):
                outcome = "NOT_FILLED"
            out.append(
                {
                    "timestamp": r.get("updated_at") or r.get("created_at"),
                    "asset": "ARB",
                    "question": str(r.get("market_id", "")),
                    "market_id": r.get("market_id"),
                    "side": str(r.get("side", "")).upper(),
                    "bet_size": float(r.get("size") or 0),
                    "limit_price": float(r.get("price") or 0),
                    "edge": None,
                    "outcome": outcome,
                    "pnl": None,
                    "paper_trade": paper,
                }
            )
        return _json(out)

    async def _halt_asset_compat(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            asset = str(payload.get("asset", "")).upper()
        except Exception:
            asset = ""
        supported = ("BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "AVAX", "LINK")
        if asset not in supported:
            return _json({"ok": False, "error": "invalid asset"}, status=400)
        reason = f"legacy per-asset halt for {asset} (arb halts globally)"
        await self._engine.risk.halt(reason)
        return _json({"ok": True, "asset": asset, "halted": True, "scope": "global_arb"})

    async def _resume_asset_compat(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            asset = str(payload.get("asset", "")).upper()
        except Exception:
            asset = ""
        supported = ("BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "AVAX", "LINK")
        if asset not in supported:
            return _json({"ok": False, "error": "invalid asset"}, status=400)
        await self._engine.risk.resume()
        return _json({"ok": True, "asset": asset, "halted": False, "scope": "global_arb"})

    async def run(self) -> None:
        app = web.Application(middlewares=[cors_middleware, self.auth_middleware])
        app.router.add_get("/health", self._health)
        app.router.add_get("/summary", self._summary)
        app.router.add_get("/events", self._events)
        app.router.add_get("/opportunities", self._opportunities)
        app.router.add_get("/orders", self._orders)
        app.router.add_get("/positions", self._positions)
        app.router.add_get("/baskets", self._baskets)
        app.router.add_post("/halt", self._halt)
        app.router.add_post("/resume", self._resume)
        app.router.add_post("/cycle", self._run_cycle)
        app.router.add_post("/settle", self._settle)
        app.router.add_post("/funds/add", self._add_funds)
        app.router.add_get("/funds/history", self._funds_history)
        app.router.add_get("/stats", self._stats_compat)
        app.router.add_get("/stats/assets", self._stats_assets_compat)
        app.router.add_get("/trades", self._trades_compat)
        app.router.add_post("/halt/asset", self._halt_asset_compat)
        app.router.add_post("/resume/asset", self._resume_asset_compat)

        for route in list(app.router.routes()):
            if hasattr(route, "resource"):
                try:
                    app.router.add_options(route.resource.canonical, lambda r: web.Response(status=204))
                except Exception:
                    pass

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", self._config.control_api_port)
        await site.start()
        logger.info("arb_control.started", port=self._config.control_api_port)
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await runner.cleanup()
