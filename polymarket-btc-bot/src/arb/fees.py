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
