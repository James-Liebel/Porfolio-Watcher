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

# Rebate applied to effective threshold (0.10% maker rebate adjustment)
_REBATE_ADJUSTMENT = 0.001


def _delta_to_prob(abs_delta: float, asset: str) -> float:
    table = _PROB_TABLES.get(asset, _PROB_TABLE_BTC)
    for threshold, prob in table:
        if abs_delta < threshold:
            return prob
    return table[-1][1]


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

    true_prob = _delta_to_prob(abs_delta, resolved_asset)

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

    edge = true_prob - market_implied_prob

    # Reduce threshold slightly to account for 0.10% maker rebate
    effective_threshold = config.edge_threshold - _REBATE_ADJUSTMENT

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
