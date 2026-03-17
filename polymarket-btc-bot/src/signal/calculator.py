"""Edge signal calculator: delta → per-asset calibrated probability → edge score."""
from __future__ import annotations

import enum
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from ..config import Settings
from ..markets.window import WindowState


class Direction(str, enum.Enum):
    UP = "UP"
    DOWN = "DOWN"


# ── Per-asset calibrated delta → win-probability lookup tables ───────────────
# Each entry: (abs_delta_threshold, true_win_probability)
# Tables derived from 5-minute candle backtests per asset.

_PROB_TABLE_BTC = [
    (0.001, 0.52),
    (0.003, 0.58),
    (0.005, 0.64),
    (0.010, 0.71),
    (float("inf"), 0.78),
]

_PROB_TABLE_ETH = [
    (0.001, 0.53),
    (0.003, 0.59),
    (0.005, 0.65),
    (0.010, 0.72),
    (float("inf"), 0.79),
]

_PROB_TABLE_SOL = [
    (0.002, 0.53),
    (0.005, 0.60),
    (0.010, 0.67),
    (0.020, 0.74),
    (float("inf"), 0.80),
]

_PROB_TABLE_XRP = [
    (0.001, 0.52),
    (0.002, 0.57),
    (0.004, 0.62),
    (0.008, 0.69),
    (float("inf"), 0.76),
]

_PROB_TABLES: dict[str, list] = {
    "BTC": _PROB_TABLE_BTC,
    "ETH": _PROB_TABLE_ETH,
    "SOL": _PROB_TABLE_SOL,
    "XRP": _PROB_TABLE_XRP,
}

def _delta_to_prob(abs_delta: float, asset: str) -> float:
    table = _PROB_TABLES.get(asset, _PROB_TABLE_BTC)
    previous_threshold = 0.0
    previous_prob = 0.50
    for threshold, prob in table:
        if abs_delta <= threshold:
            if threshold == float("inf"):
                return prob
            span = max(threshold - previous_threshold, 1e-9)
            progress = (abs_delta - previous_threshold) / span
            return previous_prob + ((prob - previous_prob) * progress)
        previous_threshold = threshold
        previous_prob = prob
    return table[-1][1]


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _confidence_scale(window: WindowState, config: Settings) -> float:
    total_liquidity = float(window.liquidity_yes + window.liquidity_no)
    liquidity_ratio = total_liquidity / max(config.min_market_liquidity, 1.0)
    liquidity_factor = _clamp(liquidity_ratio, 0.0, 1.5)

    entry_span = max(config.entry_window_seconds - config.min_seconds_remaining, 1)
    time_progress = _clamp(
        (config.entry_window_seconds - float(window.seconds_remaining)) / entry_span,
        0.0,
        1.0,
    )

    overround = max(
        0.0,
        float(window.current_yes_price + window.current_no_price - Decimal("1.0")),
    )

    scale = 0.60 + (0.20 * min(liquidity_factor, 1.0)) + (0.20 * time_progress)
    scale -= min(overround * 1.5, 0.25)
    return _clamp(scale, 0.35, 1.0)
@dataclass
class SignalResult:
    edge: float
    trade_side: str          # "YES" or "NO"
    true_prob: float
    market_implied_prob: float
    delta: float
    direction: Direction
    tradeable: bool
    token_id: str
    asset: str               # "BTC", "ETH", "SOL", "XRP"


def compute(
    window: WindowState,
    current_price: Decimal,
    config: Settings,
    asset: Optional[str] = None,
) -> Optional[SignalResult]:
    """
    Returns a SignalResult (or None if window_open_price is not yet set).
    tradeable=True only when edge > effective_threshold AND in entry window.

    asset parameter takes precedence over window.asset for explicit overrides.
    """
    if window.window_open_price is None:
        return None
    if current_price <= 0:
        return None

    resolved_asset = (asset or window.asset or "BTC").upper()

    open_price = window.window_open_price
    delta = float((current_price - open_price) / open_price)
    abs_delta = abs(delta)

    base_true_prob = _delta_to_prob(abs_delta, resolved_asset)
    confidence_scale = _confidence_scale(window, config)
    true_prob = 0.5 + ((base_true_prob - 0.5) * confidence_scale)

    if delta >= 0:
        direction = Direction.UP
        market_implied_prob = float(window.current_yes_price)
        trade_side = "YES"
        token_id = window.yes_token_id
    else:
        direction = Direction.DOWN
        market_implied_prob = float(window.current_no_price)
        trade_side = "NO"
        token_id = window.no_token_id

    if market_implied_prob <= 0 or market_implied_prob >= 1:
        return None

    overround = max(
        0.0,
        float(window.current_yes_price + window.current_no_price - Decimal("1.0")),
    )
    edge = true_prob - market_implied_prob - (overround * 0.25)

    effective_threshold = max(
        0.0,
        config.edge_threshold - (config.maker_rebate_bps_assumption / 10000.0),
    )

    in_entry_window = (
        window.seconds_remaining <= config.entry_window_seconds
        and window.seconds_remaining > config.min_seconds_remaining
    )
    tradeable = edge > effective_threshold and in_entry_window

    return SignalResult(
        edge=edge,
        trade_side=trade_side,
        true_prob=true_prob,
        market_implied_prob=market_implied_prob,
        delta=delta,
        direction=direction,
        tradeable=tradeable,
        token_id=token_id,
        asset=resolved_asset,
    )
