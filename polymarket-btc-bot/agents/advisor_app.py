"""
HTTP advisor: pulls /summary from both structural agents, calls Ollama or an OpenAI-compatible API.

Run from polymarket-btc-bot:  python -m agents.advisor_app
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp
from aiohttp import web

from .advisor_settings import AdvisorSettings
from .context_builder import SYSTEM_PROMPT, build_user_prompt, fetch_agent_context
from .llm_client import complete_chat

_cache: dict[str, Any] | None = None
_cache_ts: float = 0.0
_refresh_task: asyncio.Task | None = None  # background LLM refresh in flight


@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        return web.Response(
            status=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
            },
        )
    resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


def _settings(request: web.Request) -> AdvisorSettings:
    return request.app["settings"]


async def handle_health(request: web.Request) -> web.Response:
    settings = _settings(request)
    timeout = min(30.0, settings.advisor_http_timeout)
    async with aiohttp.ClientSession() as session:
        a = await fetch_agent_context(session, "127.0.0.1", settings.agent_a_port, "A", timeout)
        b = await fetch_agent_context(session, "127.0.0.1", settings.agent_b_port, "B", timeout)
    return web.json_response(
        {
            "ok": True,
            "llm_provider": settings.llm_provider,
            "ollama_model": settings.ollama_model if settings.llm_provider == "ollama" else None,
            "openai_model": settings.openai_model if settings.llm_provider == "openai_compatible" else None,
            "agent_a_reachable": "error" not in a,
            "agent_b_reachable": "error" not in b,
        }
    )


async def _do_refresh(settings: AdvisorSettings) -> None:
    """Background task: call LLM and update cache. Never raises."""
    global _cache, _cache_ts, _refresh_task
    timeout = float(settings.advisor_http_timeout)
    try:
        async with aiohttp.ClientSession() as session:
            a_ctx = await fetch_agent_context(
                session, "127.0.0.1", settings.agent_a_port, "Agent A", timeout
            )
            b_ctx = await fetch_agent_context(
                session, "127.0.0.1", settings.agent_b_port, "Agent B", timeout
            )
            user_prompt = build_user_prompt(a_ctx, b_ctx)
            try:
                markdown = await complete_chat(session, settings, SYSTEM_PROMPT, user_prompt)
                payload: dict[str, Any] = {
                    "ok": True,
                    "markdown": markdown,
                    "provider": settings.llm_provider,
                    "cached": False,
                    "context_ok": True,
                }
                _cache = {k: v for k, v in payload.items()}
                _cache_ts = time.monotonic()
            except Exception as exc:
                # Only overwrite cache on error if there is NO good cache yet.
                if _cache is None or not _cache.get("ok"):
                    _cache = {
                        "ok": False,
                        "markdown": "",
                        "error": str(exc),
                        "provider": settings.llm_provider,
                        "cached": False,
                        "context_ok": True,
                        "partial_context": {"agent_a": a_ctx, "agent_b": b_ctx},
                    }
                    _cache_ts = time.monotonic()
    except Exception:
        pass
    finally:
        _refresh_task = None


async def handle_advice(request: web.Request) -> web.Response:
    global _cache, _cache_ts, _refresh_task
    settings = _settings(request)
    now = time.monotonic()
    bust = request.rel_url.query.get("refresh") == "1"

    cache_fresh = (
        _cache is not None
        and (now - _cache_ts) < settings.advice_cache_seconds
    )

    # Stale-while-revalidate: if cache is still fresh AND this isn't a forced refresh, serve it immediately.
    if cache_fresh and not bust:
        out = dict(_cache)  # type: ignore[arg-type]
        out["cached"] = True
        return web.json_response(out)

    # Cache is stale (or bust). If a background refresh is already running, serve stale cache while waiting.
    if _refresh_task is None or _refresh_task.done():
        _refresh_task = asyncio.create_task(_do_refresh(settings))

    if bust:
        # Forced refresh: wait for the background task to finish, then return fresh data.
        await _refresh_task
        out = dict(_cache) if _cache is not None else {"ok": False, "error": "No data yet", "cached": False}
        out["cached"] = False
        return web.json_response(out)

    # Stale cache exists: serve it immediately with a "stale" flag while background refresh runs.
    if _cache is not None:
        out = dict(_cache)
        out["cached"] = True
        out["stale"] = True
        return web.json_response(out)

    # No cache at all yet: wait for the first refresh to complete.
    if _refresh_task is not None:
        await _refresh_task
    out = dict(_cache) if _cache is not None else {"ok": False, "error": "No LLM response yet", "cached": False}
    out["cached"] = False
    return web.json_response(out)


async def run_app() -> None:
    settings = AdvisorSettings()
    app = web.Application(middlewares=[cors_middleware])
    app["settings"] = settings
    app.router.add_get("/health", handle_health)
    app.router.add_get("/advice", handle_advice)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.advisor_host, settings.advisor_port)
    await site.start()
    print(
        f"Advisor listening on http://{settings.advisor_host}:{settings.advisor_port} "
        f"(LLM_PROVIDER={settings.llm_provider})"
    )
    print("  GET /health  GET /advice  (use ?refresh=1 to bypass cache)")
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()


def main() -> None:
    asyncio.run(run_app())


if __name__ == "__main__":
    main()
