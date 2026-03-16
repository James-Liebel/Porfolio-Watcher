"""Telegram alerts and command handler for the trading bot."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

import structlog
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

if TYPE_CHECKING:
    from ..config import Settings
    from ..risk.manager import RiskManager
    from ..storage.db import Database

logger = structlog.get_logger(__name__)


class TelegramAlerter:
    """
    Sends proactive alerts and handles inbound /commands.
    Built on python-telegram-bot v21 async API.
    """

    def __init__(self, config: "Settings") -> None:
        self._config = config
        self._chat_id = config.telegram_chat_id
        self._risk: Optional["RiskManager"] = None
        self._db: Optional["Database"] = None
        self._app: Optional[Application] = None

    def wire(self, risk: "RiskManager", db: "Database") -> None:
        self._risk = risk
        self._db = db

    # ── Outbound alerts ─────────────────────────────────────────────────

    async def send(self, text: str) -> None:
        if self._app is None:
            return
        try:
            await self._app.bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.error("telegram.send_error", error=str(exc))

    async def send_trade_placed(self, result) -> None:
        paper_tag = " [PAPER]" if result.paper_trade else ""
        asset = getattr(result, "asset", "BTC")
        repost = getattr(result, "repost_count", 0)
        repost_tag = f" | Repost #{repost}" if repost > 0 else ""
        msg = (
            f"🎯 <b>Order posted (maker){paper_tag}</b>\n"
            f"Asset: {asset} | Market: {result.question}\n"
            f"Side: <b>{result.side}</b> @ ${float(result.limit_price):.3f}\n"
            f"Size: ${float(result.bet_size):.2f} | Edge: {result.edge:.1%}\n"
            f"⏱ {result.seconds_at_entry}s remaining{repost_tag}"
        )
        await self.send(msg)

    async def send_trade_result(self, result, daily_pnl: float, fill_rate: float = 0.0) -> None:
        asset = getattr(result, "asset", "BTC")
        if result.outcome == "WIN":
            pnl_val = result.pnl or 0.0
            rebate = float(getattr(result, "maker_rebate_earned", 0))
            msg = (
                f"✅ <b>WIN +${pnl_val:.2f}</b>  (rebate: +${rebate:.4f})\n"
                f"Asset: {asset} | {result.question}\n"
                f"Fill rate today: {fill_rate:.0%}\n"
                f"PnL today: ${daily_pnl:+.2f}"
            )
        elif result.outcome == "LOSS":
            msg = (
                f"❌ <b>LOSS -${float(result.bet_size):.2f}</b>\n"
                f"Asset: {asset} | {result.question}\n"
                f"PnL today: ${daily_pnl:+.2f}"
            )
        else:
            msg = f"⚪ Order not filled: [{asset}] {result.question}"
        await self.send(msg)

    async def send_halt_alert(self, reason: str, daily_pnl: float) -> None:
        msg = (
            f"🚨 <b>TRADING HALTED</b>\n"
            f"Reason: {reason}\n"
            f"Daily PnL: ${daily_pnl:+.2f}\n"
            f"Send /resume to resume trading"
        )
        await self.send(msg)

    async def send_daily_summary(self, stats: dict) -> None:
        trades = stats.get("daily_trade_count", 0)
        wins = stats.get("daily_wins", 0)
        losses = stats.get("daily_losses", 0)
        not_filled = stats.get("daily_not_filled", 0)
        fills = trades - not_filled
        gross_pnl = stats.get("daily_pnl", 0.0)
        bankroll = stats.get("bankroll", 0.0)
        rebates = stats.get("total_rebates_earned", 0.0)
        net_pnl = gross_pnl + rebates
        fill_rate = (fills / trades) if trades > 0 else 0.0
        win_rate = (wins / fills) if fills > 0 else 0.0
        by_asset = stats.get("trades_by_asset", {})
        msg = (
            f"📊 <b>Daily Summary</b>\n"
            f"Trades: {trades} | Fills: {fills} ({fill_rate:.0%} fill rate)\n"
            f"Wins: {wins} | Losses: {losses} | Win rate: {win_rate:.1%}\n"
            f"Gross PnL: ${gross_pnl:+.2f}\n"
            f"Rebates earned: +${rebates:.4f}\n"
            f"Net PnL: ${net_pnl:+.2f}\n"
            f"By asset: BTC {by_asset.get('BTC', 0)} | ETH {by_asset.get('ETH', 0)} | "
            f"SOL {by_asset.get('SOL', 0)} | XRP {by_asset.get('XRP', 0)}\n"
            f"Bankroll: ${bankroll:.2f}"
        )
        await self.send(msg)

    # ── Inbound command handlers ─────────────────────────────────────────

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._risk is None:
            return
        stats = self._risk.get_stats()
        status = "🔴 HALTED" if stats["trading_halted"] else "🟢 ACTIVE"
        msg = (
            f"{status}\n"
            f"Bankroll: ${stats['bankroll']:.2f}\n"
            f"Daily PnL: ${stats['daily_pnl']:+.2f}\n"
            f"Trades today: {stats['daily_trade_count']} "
            f"(W:{stats['daily_wins']} L:{stats['daily_losses']})\n"
            f"Open positions: {stats['open_positions']}"
        )
        await update.message.reply_text(msg)

    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._risk is None:
            return
        await self._risk.resume_trading()
        await update.message.reply_text("✅ Trading resumed.")

    async def _cmd_halt(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._risk is None:
            return
        await self._risk.halt_trading("Manual halt via Telegram")
        await update.message.reply_text("🛑 Trading halted manually.")

    async def _cmd_trades(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._db is None:
            return
        trades = await self._db.get_all_trades(limit=5)
        if not trades:
            await update.message.reply_text("No trades recorded yet.")
            return
        lines = []
        for t in trades:
            outcome_icon = {"WIN": "✅", "LOSS": "❌", "PENDING": "⏳", "NOT_FILLED": "⚪"}.get(
                t.get("outcome", ""), "❓"
            )
            paper = " [P]" if t.get("paper_trade") else ""
            lines.append(
                f"{outcome_icon} {t.get('side')} ${t.get('bet_size', 0):.2f}{paper} — "
                f"{t.get('question', '')[:40]}"
            )
        await update.message.reply_text("\n".join(lines))

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Build and start the Telegram Application in polling mode."""
        try:
            app = (
                Application.builder()
                .token(self._config.telegram_bot_token)
                .build()
            )
            app.add_handler(CommandHandler("status", self._cmd_status))
            app.add_handler(CommandHandler("resume", self._cmd_resume))
            app.add_handler(CommandHandler("halt", self._cmd_halt))
            app.add_handler(CommandHandler("trades", self._cmd_trades))
            self._app = app

            await app.initialize()
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)
            logger.info("telegram.started")

            # Keep running until cancelled
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            if self._app:
                await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
        except Exception as exc:
            logger.error("telegram.run_error", error=str(exc))
