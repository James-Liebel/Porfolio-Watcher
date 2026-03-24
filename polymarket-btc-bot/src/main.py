"""
main.py - Structural-arbitrage runtime for Polymarket.

Boots the paper-first negative-risk / basket-arbitrage engine and its control API.
Legacy directional modules remain in the repository, but they are no longer the active runtime.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

import structlog

from .arb import ArbControlAPI, ArbEngine, ArbRepository
from .config import Settings, get_settings
from .storage.db import Database


def _configure_logging(level: str) -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.stdlib.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )


async def main() -> None:
    config: Settings = get_settings()
    _configure_logging(config.log_level)
    log = structlog.get_logger("main")

    legacy_db = Database()
    repository = ArbRepository()
    engine = ArbEngine(config=config, legacy_db=legacy_db, repository=repository)
    control_api = ArbControlAPI(config=config, engine=engine, legacy_db=legacy_db, repository=repository)

    await engine.initialize()
    log.info(
        "arb_bot.started",
        paper_trade=config.paper_trade,
        gamma_base_url=config.gamma_base_url,
        clob_host=config.clob_host,
        arb_poll_seconds=config.arb_poll_seconds,
        max_tracked_events=config.max_tracked_events,
    )

    shutting_down = {"value": False}

    async def shutdown(signal_name: str) -> None:
        if shutting_down["value"]:
            return
        shutting_down["value"] = True
        log.info("shutdown.received", signal=signal_name)
        await engine.risk.halt(f"Shutdown requested: {signal_name}")
        await engine.shutdown()
        current = asyncio.current_task()
        tasks = [task for task in asyncio.all_tasks() if task is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(shutdown(s.name)))
        except NotImplementedError:
            pass

    running_tasks = [
        asyncio.create_task(_safe_run(engine.run(), "arb_engine")),
        asyncio.create_task(_safe_run(control_api.run(), "arb_control")),
    ]

    try:
        await asyncio.gather(*running_tasks)
    except asyncio.CancelledError:
        await shutdown("CANCELLED")


async def _safe_run(coro, name: str) -> None:
    log = structlog.get_logger(name)
    try:
        await coro
    except asyncio.CancelledError:
        log.info(f"{name}.cancelled")
    except Exception as exc:
        log.error(f"{name}.fatal_error", error=str(exc), exc_info=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user.")
        sys.exit(0)
