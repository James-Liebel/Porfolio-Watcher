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


async def handle_advice(request: web.Request) -> web.Response:
    global _cache, _cache_ts
    settings = _settings(request)
    now = time.monotonic()
    bust = request.rel_url.query.get("refresh") == "1"
    if (
        not bust
        and _cache is not None
        and (now - _cache_ts) < settings.advice_cache_seconds
    ):
        out = dict(_cache)
        out["cached"] = True
        return web.json_response(out)

    timeout = min(45.0, settings.advisor_http_timeout)
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
            payload = {
                "ok": True,
                "markdown": markdown,
                "provider": settings.llm_provider,
                "cached": False,
                "context_ok": True,
            }
        except Exception as exc:
            payload = {
                "ok": False,
                "markdown": "",
                "error": str(exc),
                "provider": settings.llm_provider,
                "cached": False,
                "context_ok": True,
                "partial_context": {"agent_a": a_ctx, "agent_b": b_ctx},
            }

    if payload.get("ok"):
        _cache = {k: v for k, v in payload.items()}
        _cache_ts = now
    return web.json_response(payload)


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
