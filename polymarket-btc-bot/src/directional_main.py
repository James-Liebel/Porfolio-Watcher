"""
directional_main.py — Paper-mode runner for the legacy 5-minute directional crypto strategy.

This re-wires the legacy directional components (price feeds → window tracker → edge signal →
maker-order trader → risk/DB) to evaluate whether the strategy's claimed momentum edge is real,
WITHOUT risking capital. It hard-forces PAPER_TRADE and writes to an isolated SQLite file so it
never touches the structural-arb bot's live database.

Run:  python -m src.directional_main
"""
from __future__ import annotations

import asyncio
import logging
import os

# Hard-force paper mode for this experiment BEFORE settings are constructed.
# Env vars take precedence over the .env file in pydantic-settings.
os.environ["PAPER_TRADE"] = "true"

import signal as signal_module
import sys
from datetime import datetime, timezone
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

# Isolated paper DB so this never collides with the arb bot's live_arb.db.
_PAPER_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "paper_directional.db")


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
    # Belt-and-suspenders: never allow real orders from this runner.
    try:
        config.paper_trade = True
    except Exception:
        pass
    if not config.paper_trade:
        print("[ABORT] Could not force paper mode; refusing to run directional bot live.", file=sys.stderr)
        sys.exit(2)

    _configure_logging(config.log_level)
    log = structlog.get_logger("directional")
    log.info("directional_paper.starting", paper_trade=config.paper_trade)

    db = Database(_PAPER_DB_PATH)
    await db.init()

    risk = RiskManager(config)
    await risk.load_from_db(db)

    telegram = TelegramAlerter(config)
    telegram.wire(risk, db)
    risk.add_halt_callback(telegram.send_halt_alert)

    multi_asset_feed = MultiAssetFeed()
    coinbase = CoinbaseFeed()
    aggregator = PriceAggregator(multi_asset_feed, coinbase)

    scanner = MarketScanner(config)
    trader = Trader(config)
    control_api = ControlAPI(config, risk, db)

    window_states: Dict[str, WindowState] = {}
    # Lightweight session counters for the periodic results summary.
    session = {"signals": 0, "trades_attempted": 0}

    async def trading_loop() -> None:
        while True:
            try:
                if aggregator.latest is None:
                    await asyncio.sleep(0.5)
                    continue

                for market_id, market in scanner.active_markets.items():
                    if market_id not in window_states:
                        window_states[market_id] = WindowState.from_market(market)

                expired = [
                    mid for mid, ws in window_states.items()
                    if not ws.is_active and mid not in scanner.active_markets
                ]
                for mid in expired:
                    del window_states[mid]

                for market_id, window in list(window_states.items()):
                    try:
                        asset = window.asset
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

                        sig = calculator.compute(window, current_price, config, asset)
                        if sig is None or not sig.tradeable:
                            continue

                        window.status = WindowStatus.SIGNAL_FOUND
                        session["signals"] += 1
                        log.info(
                            "signal.found",
                            market_id=market_id,
                            asset=asset,
                            side=sig.trade_side,
                            edge=f"{sig.edge:.3f}",
                            delta=f"{sig.delta:.4f}",
                            true_prob=f"{sig.true_prob:.3f}",
                            market_prob=f"{sig.market_implied_prob:.3f}",
                            secs=int(window.seconds_remaining),
                        )

                        bet_size = compute_bet_size(sig, risk.current_bankroll, config, window)
                        if bet_size <= 0:
                            window.status = WindowStatus.SKIPPED
                            continue

                        session["trades_attempted"] += 1
                        result = await trader.execute(window, sig, bet_size)
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
                                _resolve_and_record(result, window, risk, db, telegram, log, config)
                            )
                    except Exception as exc:
                        log.error("trading_loop.window_error", market_id=market_id, error=str(exc))
            except Exception as exc:
                log.error("trading_loop.error", error=str(exc))

            await asyncio.sleep(0.5)

    async def stats_loop() -> None:
        """Periodic session summary so paper edge/win-rate/PnL is observable while running."""
        while True:
            await asyncio.sleep(60)
            try:
                stats = risk.get_stats()
                db_stats = await db.get_today_stats() or {}
                wins = stats.get("daily_wins", 0)
                losses = stats.get("daily_losses", 0)
                settled = wins + losses
                win_rate = (wins / settled) if settled else None
                log.info(
                    "directional_paper.summary",
                    tracked_windows=len(window_states),
                    signals_found=session["signals"],
                    trades_attempted=session["trades_attempted"],
                    daily_trades=stats.get("daily_trade_count", 0),
                    wins=wins,
                    losses=losses,
                    not_filled=stats.get("daily_not_filled", 0),
                    win_rate=(round(win_rate, 4) if win_rate is not None else None),
                    daily_pnl=round(float(stats.get("daily_pnl", 0.0)), 4),
                    bankroll=round(float(stats.get("bankroll", 0.0)), 4),
                    fill_rate=db_stats.get("fill_rate", 0.0),
                )
            except Exception as exc:
                log.error("stats_loop.error", error=str(exc))

    shutting_down = {"value": False}

    async def shutdown(signal_name: str) -> None:
        if shutting_down["value"]:
            return
        shutting_down["value"] = True
        log.info("shutdown.received", signal=signal_name)
        await risk.halt_trading(f"Shutdown requested: {signal_name}")
        current = asyncio.current_task()
        tasks = [t for t in asyncio.all_tasks() if t is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    loop = asyncio.get_running_loop()
    for sig_name in (signal_module.SIGINT, signal_module.SIGTERM):
        try:
            loop.add_signal_handler(sig_name, lambda s=sig_name: asyncio.create_task(shutdown(s.name)))
        except NotImplementedError:
            pass

    log.info(
        "directional_paper.started",
        paper_trade=config.paper_trade,
        strategy_profile=config.strategy_profile,
        auto_asset_selection=config.auto_asset_selection,
        enabled_assets=list(config.enabled_assets()),
        edge_threshold=config.edge_threshold,
        entry_window_seconds=config.entry_window_seconds,
        db_path=os.path.abspath(_PAPER_DB_PATH),
    )

    running_tasks = [
        asyncio.create_task(_safe_run(multi_asset_feed.run(), "multi_asset_feed")),
        asyncio.create_task(_safe_run(coinbase.run(), "coinbase_feed")),
        asyncio.create_task(_safe_run(aggregator.run(), "aggregator")),
        asyncio.create_task(_safe_run(scanner.run(), "scanner")),
        asyncio.create_task(_safe_run(trading_loop(), "trading_loop")),
        asyncio.create_task(_safe_run(stats_loop(), "stats_loop")),
        asyncio.create_task(_safe_run(control_api.run(), "control_api")),
        asyncio.create_task(_safe_run(telegram.run(), "telegram")),
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


async def _resolve_and_record(result, window, risk, db, telegram, log, config: Settings) -> None:
    """Wait for the window to close, then fetch + record the WIN/LOSS outcome and PnL."""
    try:
        wait_secs = max(window.seconds_remaining + 65, 65)
        await asyncio.sleep(wait_secs)

        resolver = Trader(config)
        result = await resolver.resolve_outcome(result)

        await risk.record_outcome(result)
        if result.trade_id:
            await db.update_trade_outcome(result.trade_id, result.outcome, result.pnl or 0.0)

        stats = risk.get_stats()
        db_stats = await db.get_today_stats() or {}
        await telegram.send_trade_result(result, stats["daily_pnl"], db_stats.get("fill_rate", 0.0))

        window.status = (
            WindowStatus.SETTLED_WIN if result.outcome == "WIN" else WindowStatus.SETTLED_LOSS
        )
        log.info(
            "trade.settled",
            market_id=result.market_id,
            asset=result.asset,
            outcome=result.outcome,
            pnl=round(float(result.pnl or 0.0), 4),
        )
    except Exception as exc:
        log.error("resolve.error", market_id=getattr(result, "market_id", "?"), error=str(exc))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nDirectional paper bot stopped by user.")
        sys.exit(0)
