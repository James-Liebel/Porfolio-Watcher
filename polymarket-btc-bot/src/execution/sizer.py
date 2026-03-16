"""Fractional Kelly bet sizing with hard caps."""
from __future__ import annotations

from decimal import Decimal

from ..config import Settings
from ..signal.calculator import SignalResult


def compute_bet_size(
    signal: SignalResult,
    bankroll: Decimal,
    config: Settings,
) -> Decimal:
    """
    Fractional Kelly sizing for binary bets on a prediction market.

    Full Kelly fraction = edge / win_multiple
    where win_multiple = (1 / market_implied_prob) - 1

    Returns a Decimal bet size, bounded by:
      - fractional Kelly cap
      - hard max_bet_fraction of bankroll
      - minimum of $1.00
    """
    market_implied_prob = signal.market_implied_prob
    edge = signal.edge

    if market_implied_prob <= 0 or market_implied_prob >= 1:
        return Decimal("1.00")

    win_multiple = (1.0 / market_implied_prob) - 1.0
    if win_multiple <= 0:
        return Decimal("1.00")

    full_kelly_fraction = edge / win_multiple
    kelly_bet = bankroll * Decimal(str(config.kelly_fraction * full_kelly_fraction))

    max_bet = bankroll * Decimal(str(config.max_bet_fraction))
    bet_size = min(kelly_bet, max_bet)
    bet_size = max(bet_size, Decimal("1.00"))

    return round(bet_size, 2)
