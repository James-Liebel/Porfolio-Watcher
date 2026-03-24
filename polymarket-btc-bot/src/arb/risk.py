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

    async def hydrate_from_db(self, db: Database, exchange) -> None:
        deposits = await db.get_total_deposits()
        starting_cash = float(deposits) if deposits > 0 else float(self._config.initial_bankroll)
        exchange.set_starting_cash(starting_cash)

    def approve(self, opportunity: ArbOpportunity, exchange, open_baskets: int) -> tuple[bool, str]:
        now = datetime.now(timezone.utc)
        self._enforce_drawdown(exchange)
        if self.halted:
            self.rejected_count += 1
            self.last_decision_reason = self.halt_reason or "trading halted"
            return False, self.last_decision_reason

        if not self._config.allow_taker_execution:
            self.rejected_count += 1
            self.last_decision_reason = "current arb executor requires taker execution"
            return False, self.last_decision_reason

        # Scanner sizing uses float binary search; allow microscopic overshoot vs cap.
        if opportunity.capital_required > self._config.max_basket_notional + 1e-4:
            self.rejected_count += 1
            self.last_decision_reason = "basket notional above configured cap"
            return False, self.last_decision_reason

        if open_baskets >= self._config.max_total_open_baskets:
            self.rejected_count += 1
            self.last_decision_reason = "too many open baskets"
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

    def _enforce_drawdown(self, exchange) -> None:
        baseline = max(exchange.contributed_capital, 1.0)
        drawdown = max(baseline - exchange.equity, 0.0) / baseline
        if drawdown >= self._config.daily_loss_cap:
            self.halted = True
            self.halt_reason = f"daily drawdown cap reached ({drawdown:.2%})"

    def summary(self, exchange, open_baskets: int) -> dict[str, float | int | bool | str]:
        self._enforce_drawdown(exchange)
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
        }
