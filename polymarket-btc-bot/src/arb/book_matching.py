"""Taker-style book walks shared by PaperExchange and OpportunityScanner."""
from __future__ import annotations

from typing import Literal

from .models import TokenBook

Side = Literal["BUY", "SELL"]


def walk_taker_levels(book: TokenBook, side: Side, limit_price: float, size: float) -> list[tuple[float, float]]:
    """Return (price, size) fills for an aggressive order up to `size`, not crossing `limit_price`.

    Mirrors PaperExchange._simulate_executions so scanner capital matches simulated fills.
    """
    remaining = max(float(size), 0.0)
    if remaining <= 1e-12:
        return []

    executions: list[tuple[float, float]] = []
    levels = book.asks if side == "BUY" else book.bids
    for level in levels:
        if remaining <= 1e-12:
            break
        if side == "BUY":
            price_ok = level.price <= limit_price + 1e-12
        else:
            price_ok = level.price >= limit_price - 1e-12
        if not price_ok:
            break
        take = min(level.size, remaining)
        if take > 0:
            executions.append((float(level.price), float(take)))
            remaining -= take
    return executions


def filled_size(executions: list[tuple[float, float]]) -> float:
    return sum(sz for _, sz in executions)
