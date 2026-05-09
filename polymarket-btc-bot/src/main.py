"""
main.py - Structural-arbitrage runtime for Polymarket.

Boots the paper-first negative-risk / basket-arbitrage engine and its control API.
Legacy directional modules remain in the repository, but they are no longer the active runtime.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

import structlog

from .arb import ArbControlAPI, ArbEngine, ArbRepository
from .config import get_settings
from .storage.db import Database, get_default_database_path


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"Get-Process -Id {pid} -ErrorAction SilentlyContinue"],
            capture_output=True,
            text=True,
        )
        return bool((out.stdout or "").strip())
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _acquire_runtime_lock(lock_path: Path) -> bool:
    """Single-instance guard for `python -m src` per SQLite file (stale-PID aware)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = f"{os.getpid()}\n"
    for _ in range(2):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            return True
        except FileExistsError:
            try:
                raw = lock_path.read_text(encoding="utf-8").splitlines()
                holder = int(raw[0].strip()) if raw else -1
            except Exception:
                holder = -1
            if holder > 0 and _pid_running(holder):
                return False
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                return False
    return False


def _release_runtime_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


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
    db_path = (config.arb_sqlite_path or "").strip() or get_default_database_path()
    lock_path = Path(db_path).resolve().with_name(Path(db_path).name + ".runtime.lock")
    if not _acquire_runtime_lock(lock_path):
        print(
            f"[BLOCKED] Another bot runtime is already using this database (lock: {lock_path}). "
            "Stop the other process or remove a stale lock if you are sure it is not running.",
            file=sys.stderr,
        )
        sys.exit(2)
    try:
        _configure_logging(config.log_level)
        log = structlog.get_logger("main")

        legacy_db = Database(db_path)
        repository = ArbRepository(db_path)
        engine = ArbEngine(config=config, legacy_db=legacy_db, repository=repository)
        control_api = ArbControlAPI(config=config, engine=engine, legacy_db=legacy_db, repository=repository)

        await engine.initialize()
        log.info(
            "arb_bot.started",
            paper_trade=config.paper_trade,
            arb_live_execution=config.arb_live_execution,
            arb_strategy_mode=config.arb_strategy_mode,
            paper_taker_fee_bps=config.paper_taker_fee_bps,
            paper_spread_penalty_bps=config.paper_spread_penalty_bps,
            gamma_base_url=config.gamma_base_url,
            clob_host=config.clob_host,
            arb_poll_seconds=config.arb_poll_seconds,
            max_tracked_events=config.max_tracked_events,
            sqlite_path=db_path,
            control_port=config.control_api_port,
            initial_bankroll=config.initial_bankroll,
        )
        mode = (config.arb_strategy_mode or "").strip().lower()
        if (
            not config.paper_trade
            and config.arb_live_execution
            and mode in {"neg_risk", "both"}
            and not config.neg_risk_live_onchain_available()
        ):
            log.warning(
                "arb_bot.neg_risk_live_skipped_for_safe",
                arb_strategy_mode=mode,
                clob_signature_type=int(config.clob_signature_type or 0),
                hint="Neg-risk needs on-chain convert after NO buy; Safe requires relayer. "
                "Use ARB_STRATEGY_MODE=complete_set (CLOB-only legs) or set ARB_ALLOW_NEG_RISK_LIVE_WITH_SAFE after relayer integration.",
            )

        shutting_down = {"value": False}

        async def shutdown(signal_name: str) -> None:
            if shutting_down["value"]:
                return
            shutting_down["value"] = True
            log.info("shutdown.received", signal=signal_name)
            engine.risk.halt(f"Shutdown requested: {signal_name}")
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

        engine_task = asyncio.create_task(_safe_run(engine.run(), "arb_engine"))
        control_task = asyncio.create_task(_safe_run(control_api.run(), "arb_control"))

        try:
            done, pending = await asyncio.wait(
                {engine_task, control_task},
                return_when=asyncio.FIRST_EXCEPTION,
            )
            for finished in done:
                exc = finished.exception()
                if exc is not None:
                    for p in pending:
                        p.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    raise exc
        except asyncio.CancelledError:
            await shutdown("CANCELLED")
    finally:
        _release_runtime_lock(lock_path)


async def _safe_run(coro, name: str) -> None:
    log = structlog.get_logger(name)
    try:
        await coro
    except asyncio.CancelledError:
        log.info(f"{name}.cancelled")
        raise
    except Exception as exc:
        log.error(f"{name}.fatal_error", error=str(exc), exc_info=True)
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user.")
        sys.exit(0)
