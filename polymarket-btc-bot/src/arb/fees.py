"""Shared taker fee math for OpportunityScanner and PaperExchange (paper realism)."""
from __future__ import annotations


def taker_fee_on_notional(notional: float, fees_enabled: bool, taker_fee_bps: float) -> float:
    if not fees_enabled or notional <= 0:
        return 0.0
    return float(notional) * (float(taker_fee_bps) / 10000.0)


def maker_rebate_on_notional(notional: float, fees_enabled: bool, rebate_bps: float) -> float:
    if not fees_enabled or notional <= 0:
        return 0.0
    return float(notional) * (float(rebate_bps) / 10000.0)


def paper_structural_taker_buy_cash(
    notional: float,
    *,
    fees_enabled: bool,
    taker_fee_bps: float,
    spread_penalty_bps: float,
) -> float:
    """Cash out for one taker BUY slice: principal + modeled taker fee + optional spread penalty.

    Must match `PaperExchange._apply_fill` for structural legs (taker BUY with spread stress).
    """
    n = float(notional)
    if n <= 0:
        return 0.0
    fee = taker_fee_on_notional(n, fees_enabled, taker_fee_bps)
    spread = n * (float(spread_penalty_bps) / 10000.0) if spread_penalty_bps > 0 else 0.0
    return n + fee + spread
