"""Dynamic fractional Kelly sizing with hard caps."""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from ..config import Settings
from ..markets.window import WindowState
from ..signal.calculator import SignalResult


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def compute_bet_size(
    signal: SignalResult,
    bankroll: Decimal,
    config: Settings,
    window: Optional[WindowState] = None,
) -> Decimal:
    """
    Dynamic fractional Kelly sizing for binary prediction contracts.

    For a YES/NO share bought at price p with true win probability q:
      full_kelly_fraction = (q - p) / (1 - p)

    The final stake is then scaled by bounded edge, time, and liquidity
    multipliers so paper and live trading both size from the same stake budget.
    """
    if bankroll <= 0:
        return Decimal("0.00")

    market_implied_prob = signal.market_implied_prob
    true_prob = signal.true_prob
    edge = max(signal.edge, 0.0)

    if market_implied_prob <= 0 or market_implied_prob >= 1:
        return Decimal("0.00")
    if true_prob <= 0 or true_prob >= 1:
        return Decimal("0.00")
    if edge <= 0:
        return Decimal("0.00")

    full_kelly_fraction = max(
        0.0,
        (true_prob - market_implied_prob) / max(1.0 - market_implied_prob, 1e-6),
    )
    base_fraction = config.kelly_fraction * full_kelly_fraction

    edge_floor = max(config.edge_threshold, 0.01)
    target_edge = max(config.target_edge_for_max_size, edge_floor + 0.01)
    edge_progress = _clamp(
        (edge - edge_floor) / (target_edge - edge_floor),
        0.0,
        1.0,
    )
    edge_multiplier = 0.65 + (0.60 * edge_progress)

    time_multiplier = 1.0
    liquidity_multiplier = 1.0
    if window is not None:
        seconds_span = max(
            1,
            config.entry_window_seconds - config.min_seconds_remaining,
        )
        bounded_seconds = _clamp(
            float(window.seconds_remaining),
            float(config.min_seconds_remaining),
            float(config.entry_window_seconds),
        )
        entry_progress = (
            float(config.entry_window_seconds) - bounded_seconds
        ) / float(seconds_span)
        time_multiplier = 0.90 + (0.20 * entry_progress)

        total_liquidity = max(float(window.liquidity_yes + window.liquidity_no), 0.0)
        liquidity_ratio = _clamp(
            total_liquidity / max(config.min_market_liquidity, 1.0),
            0.0,
            4.0,
        )
        liquidity_multiplier = 0.80 + (0.10 * liquidity_ratio)

    adjusted_fraction = (
        base_fraction * edge_multiplier * time_multiplier * liquidity_multiplier
    )
    max_bet = bankroll * Decimal(str(config.max_bet_fraction))
    raw_bet = bankroll * Decimal(str(adjusted_fraction))
    bet_size = min(raw_bet, max_bet)
    if bet_size <= 0:
        return Decimal("0.00")

    min_bet = Decimal(str(config.min_bet_usd))
    bet_size = min(max(bet_size, min_bet), bankroll)

    return bet_size.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
