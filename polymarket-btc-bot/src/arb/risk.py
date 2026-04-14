from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..config import Settings
from ..storage.db import Database
from .models import ArbOpportunity


class ArbRiskManager:
    def __init__(self, config: Settings) -> None:
        self._config = config
        self.halted = False
        self.halt_reason = ""
        self.cooldowns: dict[str, datetime] = {}
        self.executed_count = 0
        self.rejected_count = 0
        self.last_decision_reason = ""
        self.cycle_execution_block_reason: str | None = None
        self._session_realized_pnl_baseline: float | None = None
        self._equity_peak_session: float = 0.0
        self._consecutive_execution_failures = 0

    async def hydrate_from_db(self, db: Database, exchange) -> None:
        deposits = await db.get_total_deposits()
        starting_cash = float(deposits) if deposits > 0 else float(self._config.initial_bankroll)
        exchange.set_starting_cash(starting_cash)

    def capture_session_baseline(self, exchange) -> None:
        """Call once after exchange state is restored so session loss / peak tracking start from truth."""
        if self._session_realized_pnl_baseline is None:
            self._session_realized_pnl_baseline = float(exchange.realized_pnl)
        self._bump_equity_peak(exchange.equity)

    def begin_cycle(self, *, books_synthetic: int) -> None:
        self.cycle_execution_block_reason = None
        thr = int(self._config.arb_halt_execution_if_synthetic_books_ge)
        if thr > 0 and books_synthetic >= thr:
            self.cycle_execution_block_reason = (
                f"synthetic book gate ({books_synthetic} synthetic books >= {thr}; CLOB data quality)"
            )

    def record_execution_failure(self) -> None:
        self._consecutive_execution_failures += 1
        cap = int(self._config.arb_consecutive_execution_failures_halt)
        if cap > 0 and self._consecutive_execution_failures >= cap:
            self.halted = True
            self.halt_reason = f"consecutive execution failures ({self._consecutive_execution_failures})"

    def record_execution_success(self) -> None:
        self._consecutive_execution_failures = 0

    def _bump_equity_peak(self, equity: float) -> None:
        if self._equity_peak_session <= 0:
            self._equity_peak_session = float(equity)
        else:
            self._equity_peak_session = max(self._equity_peak_session, float(equity))

    def approve(
        self,
        opportunity: ArbOpportunity,
        exchange,
        open_baskets: int,
        open_baskets_by_strategy: dict[str, int] | None = None,
        *,
        max_basket_notional: float | None = None,
    ) -> tuple[bool, str]:
        now = datetime.now(timezone.utc)
        basket_cap = (
            float(max_basket_notional)
            if max_basket_notional is not None
            else float(self._config.max_basket_notional)
        )
        self._bump_equity_peak(exchange.equity)
        self._enforce_stops(exchange)
        if self.cycle_execution_block_reason:
            self.rejected_count += 1
            self.last_decision_reason = self.cycle_execution_block_reason
            return False, self.cycle_execution_block_reason

        if self.halted:
            self.rejected_count += 1
            self.last_decision_reason = self.halt_reason or "trading halted"
            return False, self.last_decision_reason

        if not self._config.allow_taker_execution:
            self.rejected_count += 1
            self.last_decision_reason = "current arb executor requires taker execution"
            return False, self.last_decision_reason

        # Scanner sizing uses float binary search; allow microscopic overshoot vs cap.
        if opportunity.capital_required > basket_cap + 1e-4:
            self.rejected_count += 1
            self.last_decision_reason = "basket notional above configured cap"
            return False, self.last_decision_reason

        if open_baskets >= self._config.max_total_open_baskets:
            self.rejected_count += 1
            self.last_decision_reason = "too many open baskets"
            return False, self.last_decision_reason

        if open_baskets_by_strategy is not None:
            strategy_count = open_baskets_by_strategy.get(opportunity.strategy_type, 0)
            if strategy_count >= self._config.max_baskets_per_strategy:
                self.rejected_count += 1
                self.last_decision_reason = f"strategy basket cap reached ({opportunity.strategy_type})"
                return False, self.last_decision_reason

        if exchange.available_cash < opportunity.capital_required:
            self.rejected_count += 1
            self.last_decision_reason = "insufficient available cash"
            return False, self.last_decision_reason

        event_limit = max(exchange.contributed_capital, exchange.equity) * self._config.max_event_exposure_pct
        if exchange.event_exposure(opportunity.event_id) + opportunity.capital_required > event_limit:
            self.rejected_count += 1
            self.last_decision_reason = "event exposure cap breached"
            return False, self.last_decision_reason

        cooldown_key = opportunity.cooldown_key or f"{opportunity.strategy_type}:{opportunity.event_id}"
        cooldown_until = self.cooldowns.get(cooldown_key)
        if cooldown_until and now < cooldown_until:
            self.rejected_count += 1
            self.last_decision_reason = "opportunity in cooldown"
            return False, self.last_decision_reason

        self.last_decision_reason = "approved"
        return True, "approved"

    def record_execution(self, opportunity: ArbOpportunity) -> None:
        now = datetime.now(timezone.utc)
        cooldown_key = opportunity.cooldown_key or f"{opportunity.strategy_type}:{opportunity.event_id}"
        self.cooldowns[cooldown_key] = now + timedelta(seconds=self._config.opportunity_cooldown_seconds)
        self.executed_count += 1
        self.last_decision_reason = "executed"

    async def add_funds(self, amount: float, note: str, db: Database, exchange) -> None:
        if amount <= 0:
            raise ValueError("amount must be positive")
        await db.insert_deposit(amount, note)
        exchange.add_funds(amount)

    async def halt(self, reason: str) -> None:
        self.halted = True
        self.halt_reason = reason or "manual halt"

    async def resume(self) -> None:
        self.halted = False
        self.halt_reason = ""
        self._equity_peak_session = 0.0
        self._consecutive_execution_failures = 0

    def _enforce_stops(self, exchange) -> None:
        if self.halted:
            return

        baseline = max(exchange.contributed_capital, 1.0)
        drawdown = max(baseline - exchange.equity, 0.0) / baseline
        if drawdown >= self._config.daily_loss_cap:
            self.halted = True
            self.halt_reason = f"daily drawdown cap reached ({drawdown:.2%})"
            return

        trail = float(self._config.arb_trailing_equity_drawdown_pct)
        if trail > 0 and self._equity_peak_session > 0:
            floor = self._equity_peak_session * (1.0 - trail)
            if exchange.equity < floor - 1e-9:
                self.halted = True
                self.halt_reason = (
                    f"trailing equity stop (equity {exchange.equity:.2f} < peak×(1−{trail:.0%}) "
                    f"≈ {floor:.2f}; peak {self._equity_peak_session:.2f})"
                )
                return

        loss_cap = float(self._config.arb_session_realized_loss_usd)
        if loss_cap > 0 and self._session_realized_pnl_baseline is not None:
            drop = float(exchange.realized_pnl) - self._session_realized_pnl_baseline
            if drop <= -loss_cap - 1e-9:
                self.halted = True
                self.halt_reason = (
                    f"session realized loss cap (Δ realized {drop:.2f} vs start ≤ −{loss_cap:.2f})"
                )

    def summary(self, exchange, open_baskets: int) -> dict[str, float | int | bool | str]:
        self._bump_equity_peak(exchange.equity)
        self._enforce_stops(exchange)
        return {
            "trading_halted": self.halted,
            "halt_reason": self.halt_reason,
            "available_cash": round(exchange.available_cash, 4),
            "cash": round(exchange.cash, 4),
            "equity": round(exchange.equity, 4),
            "contributed_capital": round(exchange.contributed_capital, 4),
            "realized_pnl": round(exchange.realized_pnl, 4),
            "fees_paid": round(exchange.fees_paid, 4),
            "rebates_earned": round(exchange.rebates_earned, 4),
            "open_positions": len(exchange.get_positions()),
            "open_orders": len(exchange.get_open_orders()),
            "open_baskets": open_baskets,
            "executed_count": self.executed_count,
            "rejected_count": self.rejected_count,
            "last_decision_reason": self.last_decision_reason,
            "equity_peak_session": round(self._equity_peak_session, 4),
            "consecutive_execution_failures": self._consecutive_execution_failures,
        }
