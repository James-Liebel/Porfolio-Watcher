"""Risk manager: enforces all trading guardrails and tracks session PnL."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Callable, Dict, List, Optional

import structlog

from ..config import Settings
from ..execution.trader import TradeResult
from ..markets.window import WindowState, WindowStatus

logger = structlog.get_logger(__name__)

_SUPPORTED_ASSETS = ("BTC", "ETH", "SOL", "XRP")


@dataclass
class OpenPosition:
    market_id: str
    side: str
    bet_size: Decimal
    placed_at: datetime
    asset: str = "BTC"


@dataclass
class RiskState:
    session_date: str
    session_start_bankroll: Decimal
    current_bankroll: Decimal
    daily_pnl: Decimal = field(default=Decimal("0"))
    daily_trade_count: int = field(default=0)
    daily_wins: int = field(default=0)
    daily_losses: int = field(default=0)
    daily_not_filled: int = field(default=0)
    open_positions: List[OpenPosition] = field(default_factory=list)
    trading_halted: bool = field(default=False)
    halt_reason: Optional[str] = field(default=None)
    # Per-asset open position counts
    positions_by_asset: Dict[str, int] = field(
        default_factory=lambda: {a: 0 for a in _SUPPORTED_ASSETS}
    )
    # Per-asset halt flags (independent of global halt)
    halted_assets: Dict[str, bool] = field(
        default_factory=lambda: {a: False for a in _SUPPORTED_ASSETS}
    )


class RiskManager:
    """
    Central risk gate. All trading decisions pass through can_trade().
    State persists across restarts via the DB module.
    """

    def __init__(self, config: Settings) -> None:
        self._config = config
        self._lock = asyncio.Lock()
        today = str(date.today())
        initial = Decimal(str(config.initial_bankroll))
        self._state = RiskState(
            session_date=today,
            session_start_bankroll=initial,
            current_bankroll=initial,
        )
        self._halt_callbacks: List[Callable] = []

    def add_halt_callback(self, cb: Callable) -> None:
        self._halt_callbacks.append(cb)

    async def load_from_db(self, db) -> None:
        """Restore bankroll and daily stats from persistent storage.

        Priority order for bankroll:
        1. If today's daily_summary has an ending_bankroll, use that (mid-session resume).
        2. Otherwise use total deposits recorded (could be 0 if never added any).
        3. Fall back to config initial_bankroll.
        """
        try:
            # Sum all deposits ever made to get the true funded amount
            total_deposits = await db.get_total_deposits()

            stats = await db.get_today_stats()
            async with self._lock:
                if stats:
                    self._state.daily_pnl = Decimal(str(stats.get("gross_pnl", 0)))
                    self._state.daily_trade_count = stats.get("trades", 0)
                    self._state.daily_wins = stats.get("wins", 0)
                    self._state.daily_losses = stats.get("losses", 0)
                    self._state.daily_not_filled = stats.get("not_filled", 0)
                    sb = stats.get("starting_bankroll")
                    if sb:
                        self._state.session_start_bankroll = Decimal(str(sb))
                    eb = stats.get("ending_bankroll")
                    if eb:
                        self._state.current_bankroll = Decimal(str(eb))
                elif total_deposits > 0:
                    # No session yet today — initialise from total deposits
                    deposited = Decimal(str(total_deposits))
                    self._state.session_start_bankroll = deposited
                    self._state.current_bankroll = deposited
                # else: keep config.initial_bankroll (default from __init__)

            logger.info(
                "risk.state_loaded",
                total_deposits=total_deposits,
                bankroll=float(self._state.current_bankroll),
            )
        except Exception as exc:
            logger.error("risk.load_error", error=str(exc))

    async def can_trade(self, window: WindowState, asset: Optional[str] = None) -> bool:
        """Return True only when all 7 risk rules pass."""
        resolved_asset = (asset or getattr(window, "asset", "BTC") or "BTC").upper()

        async with self._lock:
            state = self._state
            config = self._config

            # Rule 5: globally halted
            if state.trading_halted:
                return False

            # Rule 5b: per-asset halt
            if state.halted_assets.get(resolved_asset, False):
                logger.debug("risk.asset_halted", asset=resolved_asset)
                return False

            # Rule 1: daily loss cap
            loss_cap = state.session_start_bankroll * Decimal(str(config.daily_loss_cap))
            if state.daily_pnl < -loss_cap:
                await self._halt(f"Daily loss cap breached: ${float(state.daily_pnl):.2f}")
                return False

            # Rule 2: max concurrent positions (global)
            if len(state.open_positions) >= config.max_concurrent_positions:
                logger.debug("risk.max_positions", count=len(state.open_positions))
                return False

            # Rule 3: market liquidity
            total_liq = window.liquidity_yes + window.liquidity_no
            if total_liq < config.min_market_liquidity and total_liq > 0:
                logger.debug("risk.low_liquidity", liquidity=total_liq)
                return False

            # Rule 4: time guard (too late)
            if window.seconds_remaining < config.min_seconds_remaining:
                return False

            # Guard: already have a position in this exact market
            if any(p.market_id == window.market_id for p in state.open_positions):
                return False

            # Rule 6: per-asset position limit
            asset_count = state.positions_by_asset.get(resolved_asset, 0)
            if asset_count >= config.max_positions_per_asset:
                logger.debug(
                    "risk.per_asset_limit",
                    asset=resolved_asset,
                    count=asset_count,
                )
                return False

            # Rule 7: total exposure cap
            total_exposure = sum(p.bet_size for p in state.open_positions)
            max_exposure = state.current_bankroll * Decimal(str(config.max_total_exposure_pct))
            if total_exposure >= max_exposure:
                logger.debug(
                    "risk.exposure_cap",
                    exposure=float(total_exposure),
                    max=float(max_exposure),
                )
                return False

        return True

    async def record_trade(self, result: TradeResult, asset: Optional[str] = None) -> None:
        resolved_asset = (asset or result.asset or "BTC").upper()
        async with self._lock:
            state = self._state
            state.daily_trade_count += 1

            if not result.filled:
                state.daily_not_filled += 1
                return

            state.open_positions.append(
                OpenPosition(
                    market_id=result.market_id,
                    side=result.side,
                    bet_size=result.bet_size,
                    placed_at=result.timestamp,
                    asset=resolved_asset,
                )
            )
            state.positions_by_asset[resolved_asset] = (
                state.positions_by_asset.get(resolved_asset, 0) + 1
            )
            logger.info(
                "risk.position_opened",
                market_id=result.market_id,
                asset=resolved_asset,
                side=result.side,
                size=str(result.bet_size),
            )

    async def record_outcome(self, result: TradeResult) -> None:
        """Called when market settles."""
        resolved_asset = (result.asset or "BTC").upper()
        async with self._lock:
            state = self._state

            # Remove from open positions and decrement per-asset count
            matching = [p for p in state.open_positions if p.market_id == result.market_id]
            state.open_positions = [
                p for p in state.open_positions if p.market_id != result.market_id
            ]
            if matching:
                state.positions_by_asset[resolved_asset] = max(
                    0, state.positions_by_asset.get(resolved_asset, 0) - 1
                )

            if result.outcome == "WIN":
                state.daily_wins += 1
                pnl = Decimal(str(result.pnl or 0))
            elif result.outcome == "LOSS":
                state.daily_losses += 1
                pnl = -result.bet_size
            else:
                pnl = Decimal("0")

            state.daily_pnl += pnl
            state.current_bankroll += pnl
            logger.info(
                "risk.outcome_recorded",
                market_id=result.market_id,
                asset=resolved_asset,
                outcome=result.outcome,
                pnl=float(pnl),
                bankroll=float(state.current_bankroll),
            )

    async def add_funds(self, amount: float, note: str, db) -> None:
        """Deposit funds into the bankroll. Persists the deposit to the DB."""
        if amount <= 0:
            raise ValueError(f"Deposit amount must be positive, got {amount}")
        deposit_amount = Decimal(str(amount))
        async with self._lock:
            self._state.current_bankroll += deposit_amount
            self._state.session_start_bankroll += deposit_amount
        await db.insert_deposit(amount, note)
        logger.info(
            "risk.funds_added",
            amount=amount,
            note=note,
            new_bankroll=float(self._state.current_bankroll),
        )

    async def resume_trading(self) -> None:
        async with self._lock:
            self._state.trading_halted = False
            self._state.halt_reason = None
        logger.info("risk.trading_resumed")

    async def halt_trading(self, reason: str) -> None:
        async with self._lock:
            await self._halt(reason)

    async def halt_asset(self, asset: str) -> None:
        async with self._lock:
            self._state.halted_assets[asset.upper()] = True
        logger.info("risk.asset_halted", asset=asset)

    async def resume_asset(self, asset: str) -> None:
        async with self._lock:
            self._state.halted_assets[asset.upper()] = False
        logger.info("risk.asset_resumed", asset=asset)

    async def _halt(self, reason: str) -> None:
        """Must be called while holding the lock."""
        if not self._state.trading_halted:
            self._state.trading_halted = True
            self._state.halt_reason = reason
            logger.warning("risk.trading_halted", reason=reason)
            for cb in self._halt_callbacks:
                asyncio.create_task(cb(reason, float(self._state.daily_pnl)))

    def get_stats(self) -> dict:
        state = self._state
        return {
            "trading_halted": state.trading_halted,
            "halt_reason": state.halt_reason,
            "bankroll": float(state.current_bankroll),
            "session_start_bankroll": float(state.session_start_bankroll),
            "daily_pnl": float(state.daily_pnl),
            "daily_trade_count": state.daily_trade_count,
            "daily_wins": state.daily_wins,
            "daily_losses": state.daily_losses,
            "daily_not_filled": state.daily_not_filled,
            "open_positions": len(state.open_positions),
            "positions_by_asset": dict(state.positions_by_asset),
            "halted_assets": dict(state.halted_assets),
            "total_exposure": float(sum(p.bet_size for p in state.open_positions)),
            "session_date": state.session_date,
        }

    def get_asset_stats(self) -> Dict[str, dict]:
        """Per-asset breakdown for /stats/assets endpoint."""
        state = self._state
        result: Dict[str, dict] = {}
        for asset in _SUPPORTED_ASSETS:
            open_for_asset = sum(
                1 for p in state.open_positions if p.asset == asset
            )
            result[asset] = {
                "open": open_for_asset,
                "halted": state.halted_assets.get(asset, False),
            }
        return result

    @property
    def current_bankroll(self) -> Decimal:
        return self._state.current_bankroll

    @property
    def is_halted(self) -> bool:
        return self._state.trading_halted
