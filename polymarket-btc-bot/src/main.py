"""
main.py — Polymarket multi-asset (BTC/ETH/SOL/XRP) window trading bot.

Wires all components and runs the asyncio event loop.
Every async task handles its own exceptions to prevent cascading failures.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Dict

import structlog

from .alerts.telegram import TelegramAlerter
from .config import Settings, get_settings
from .control.api import ControlAPI
from .execution.sizer import compute_bet_size
from .execution.trader import Trader
from .feeds.aggregator import PriceAggregator
from .feeds.coinbase import CoinbaseFeed
from .feeds.multi_asset import MultiAssetFeed
from .markets.scanner import MarketScanner
from .markets.window import WindowState, WindowStatus
from .risk.manager import RiskManager
from .signal import calculator
from .storage.db import Database

# ── Logging setup ────────────────────────────────────────────────────────────


def _configure_logging(level: str) -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.stdlib.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(level.upper())
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )


# ── Main orchestrator ────────────────────────────────────────────────────────


async def main() -> None:
    config: Settings = get_settings()
    _configure_logging(config.log_level)
    log = structlog.get_logger("main")
    log.info("bot.starting", paper_trade=config.paper_trade)

    # 1. Database
    db = Database()
    await db.init()

    # 2. Risk manager
    risk = RiskManager(config)
    await risk.load_from_db(db)

    # 3. Telegram
    telegram = TelegramAlerter(config)
    telegram.wire(risk, db)
    risk.add_halt_callback(telegram.send_halt_alert)

    # 4. Price feeds — Binance multi-asset (primary) + Coinbase BTC (cross-check)
    multi_asset_feed = MultiAssetFeed()
    coinbase = CoinbaseFeed()
    aggregator = PriceAggregator(multi_asset_feed, coinbase)

    # 5. Market scanner — all 4 assets
    scanner = MarketScanner(config)

    # 6. Execution
    trader = Trader(config)

    # 7. Control API
    control_api = ControlAPI(config, risk, db)

    # Active window states (keyed by market_id)
    window_states: Dict[str, WindowState] = {}

    # ── Trading loop (500 ms tick) ────────────────────────────────────────

    async def trading_loop() -> None:
        while True:
            try:
                price_update = aggregator.latest
                if price_update is None:
                    await asyncio.sleep(0.5)
                    continue

                # Sync window states with scanner
                for market_id, market in scanner.active_markets.items():
                    if market_id not in window_states:
                        window_states[market_id] = WindowState.from_market(market)

                # Remove expired windows no longer in scanner
                expired = [
                    mid for mid, ws in window_states.items()
                    if not ws.is_active and mid not in scanner.active_markets
                ]
                for mid in expired:
                    del window_states[mid]

                for market_id, window in list(window_states.items()):
                    try:
                        asset = window.asset

                        # Per-asset price from the aggregator
                        current_price = aggregator.get_price(asset)
                        if current_price is None:
                            continue

                        live_market = scanner.active_markets.get(market_id)
                        window.update(
                            current_price,
                            yes_price=live_market.current_yes_price if live_market else None,
                            no_price=live_market.current_no_price if live_market else None,
                            liquidity=live_market.liquidity if live_market else None,
                            volume=live_market.volume if live_market else None,
                            minimum_tick_size=live_market.minimum_tick_size if live_market else None,
                            fees_enabled=live_market.fees_enabled if live_market else None,
                        )

                        if window.status in (
                            WindowStatus.ORDER_PLACED,
                            WindowStatus.FILLED,
                            WindowStatus.NOT_FILLED,
                            WindowStatus.SETTLED_WIN,
                            WindowStatus.SETTLED_LOSS,
                            WindowStatus.SKIPPED,
                        ):
                            continue

                        if not await risk.can_trade(window, asset):
                            continue

                        signal = calculator.compute(window, current_price, config, asset)
                        if signal is None or not signal.tradeable:
                            continue

                        window.status = WindowStatus.SIGNAL_FOUND
                        log.info(
                            "signal.found",
                            market_id=market_id,
                            asset=asset,
                            side=signal.trade_side,
                            edge=f"{signal.edge:.3f}",
                            delta=f"{signal.delta:.4f}",
                            secs=int(window.seconds_remaining),
                        )

                        bet_size = compute_bet_size(
                            signal,
                            risk.current_bankroll,
                            config,
                            window,
                        )
                        if bet_size <= 0:
                            window.status = WindowStatus.SKIPPED
                            continue
                        result = await trader.execute(window, signal, bet_size)

                        if result is None:
                            window.status = WindowStatus.SKIPPED
                            continue

                        window.status = (
                            WindowStatus.FILLED if result.filled else WindowStatus.NOT_FILLED
                        )

                        await risk.record_trade(result, asset)
                        row_id = await db.insert_trade(result)
                        result.trade_id = row_id

                        if result.filled:
                            await telegram.send_trade_placed(result)
                            window.status = WindowStatus.ORDER_PLACED
                            asyncio.create_task(
                                _resolve_and_record(
                                    result, window, risk, db, telegram, log, config
                                )
                            )

                    except Exception as exc:
                        log.error(
                            "trading_loop.window_error",
                            market_id=market_id,
                            error=str(exc),
                        )

            except Exception as exc:
                log.error("trading_loop.error", error=str(exc))

            await asyncio.sleep(0.5)

    # ── Daily summary scheduler ───────────────────────────────────────────

    async def daily_scheduler() -> None:
        while True:
            try:
                now = datetime.now(timezone.utc)
                tomorrow_midnight = datetime(
                    now.year, now.month, now.day, tzinfo=timezone.utc
                ) + timedelta(days=1)
                wait = (tomorrow_midnight - now).total_seconds()
                await asyncio.sleep(wait)

                stats = risk.get_stats()
                today_str = str(date.today())

                # Augment with today's DB stats for the full summary
                db_stats = await db.get_today_stats() or {}
                stats["total_rebates_earned"] = db_stats.get("total_rebates_earned", 0.0)
                stats["trades_by_asset"] = db_stats.get("trades_by_asset", {})
                stats["fill_rate"] = db_stats.get("fill_rate", 0.0)

                await db.upsert_daily_summary(
                    today_str,
                    {
                        "trades": stats["daily_trade_count"],
                        "wins": stats["daily_wins"],
                        "losses": stats["daily_losses"],
                        "not_filled": stats["daily_not_filled"],
                        "gross_pnl": stats["daily_pnl"],
                        "starting_bankroll": stats["session_start_bankroll"],
                        "ending_bankroll": stats["bankroll"],
                    },
                )
                await telegram.send_daily_summary(stats)
                log.info("daily_summary.sent", date=today_str)
            except Exception as exc:
                log.error("daily_scheduler.error", error=str(exc))

    # ── Graceful shutdown wiring ──────────────────────────────────────────

    shutting_down = {"value": False}

    async def shutdown(signal_name: str) -> None:
        if shutting_down["value"]:
            return
        shutting_down["value"] = True
        log.info("shutdown.received", signal=signal_name)

        # Step 1: Stop accepting new signals
        await risk.halt_trading(f"Shutdown requested: {signal_name}")

        # Step 2: Cancel open orders when order IDs are available
        for position in list(risk._state.open_positions):  # controlled access during shutdown
            order_id = getattr(position, "order_id", None)
            if not order_id:
                continue
            try:
                await trader.cancel_order(order_id)
                log.info("shutdown.order_cancelled", order_id=order_id)
            except Exception as exc:
                log.error("shutdown.cancel_failed", order_id=order_id, error=str(exc))

        # Step 3: Write final state to database
        stats = risk.get_stats()
        await db.upsert_daily_summary(
            date_str=datetime.utcnow().strftime("%Y-%m-%d"),
            stats={
                "trades": stats["daily_trade_count"],
                "wins": stats["daily_wins"],
                "losses": stats["daily_losses"],
                "not_filled": stats["daily_not_filled"],
                "gross_pnl": stats["daily_pnl"],
                "starting_bankroll": stats["session_start_bankroll"],
                "ending_bankroll": stats["bankroll"],
            },
        )

        # Step 4: Send Telegram notification
        await telegram.send("🔴 Bot shut down cleanly. All orders cancelled.")

        # Step 5: Cancel all asyncio tasks except current
        current = asyncio.current_task()
        tasks = [t for t in asyncio.all_tasks() if t is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(
                sig,
                lambda s=sig: asyncio.create_task(shutdown(s.name)),
            )
        except NotImplementedError:
            # Windows event loop fallback
            pass

    # ── Gather all concurrent tasks ───────────────────────────────────────

    log.info(
        "bot.started",
        paper_trade=config.paper_trade,
        strategy_profile=config.strategy_profile,
        auto_asset_selection=config.auto_asset_selection,
        enabled_assets=list(config.enabled_assets()),
        asset_flags=config.manual_asset_flags(),
    )

    running_tasks = [
        asyncio.create_task(_safe_run(multi_asset_feed.run(), "multi_asset_feed")),
        asyncio.create_task(_safe_run(coinbase.run(), "coinbase_feed")),
        asyncio.create_task(_safe_run(aggregator.run(), "aggregator")),
        asyncio.create_task(_safe_run(scanner.run(), "scanner")),
        asyncio.create_task(_safe_run(trading_loop(), "trading_loop")),
        asyncio.create_task(_safe_run(control_api.run(), "control_api")),
        asyncio.create_task(_safe_run(telegram.run(), "telegram")),
        asyncio.create_task(_safe_run(daily_scheduler(), "daily_scheduler")),
    ]

    try:
        await asyncio.gather(*running_tasks)
    except asyncio.CancelledError:
        await shutdown("CANCELLED")


async def _safe_run(coro, name: str) -> None:
    """Wrap a coroutine so exceptions are logged but never crash the gather."""
    log = structlog.get_logger(name)
    try:
        await coro
    except asyncio.CancelledError:
        log.info(f"{name}.cancelled")
    except Exception as exc:
        log.error(f"{name}.fatal_error", error=str(exc), exc_info=True)


async def _resolve_and_record(
    result, window, risk, db, telegram, log, config: Settings
) -> None:
    """Wait for window to close then fetch and record the final outcome."""
    try:
        wait_secs = max(window.seconds_remaining + 65, 65)
        await asyncio.sleep(wait_secs)

        _trader = Trader(config)
        result = await _trader.resolve_outcome(result)

        await risk.record_outcome(result)
        if result.trade_id:
            await db.update_trade_outcome(
                result.trade_id,
                result.outcome,
                result.pnl or 0.0,
            )

        stats = risk.get_stats()
        db_stats = await db.get_today_stats() or {}
        fill_rate = db_stats.get("fill_rate", 0.0)
        await telegram.send_trade_result(result, stats["daily_pnl"], fill_rate)

        window.status = (
            WindowStatus.SETTLED_WIN
            if result.outcome == "WIN"
            else WindowStatus.SETTLED_LOSS
        )
    except Exception as exc:
        log.error("resolve.error", market_id=result.market_id, error=str(exc))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user.")
        sys.exit(0)
