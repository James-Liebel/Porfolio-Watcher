"""
Polymarket CLOB taker constraints mirrored for sizing and live orders.

Price tick is $0.01; USDC notional (price × size) must land on $0.01, which implies
a price-dependent minimum share step. See LiveClobExchange.place_order.
"""
from __future__ import annotations

import math


def clob_taker_buy_limit_price(raw_price: float) -> float:
    return math.ceil(float(raw_price) * 100) / 100


def clob_taker_sell_limit_price(raw_price: float) -> float:
    return math.floor(float(raw_price) * 100) / 100


def _share_step_for_clob_price(clob_price: float) -> float:
    price_cents = round(float(clob_price) * 100)
    if price_cents <= 0:
        return 0.0
    return 0.01 * (100 // math.gcd(price_cents, 100))


def clob_fok_buy_price_and_size(raw_price: float, raw_size: float) -> tuple[float, float]:
    """BUY taker: ceiled limit price, size floored to valid step (matches live posting)."""
    clob_price = clob_taker_buy_limit_price(raw_price)
    if clob_price <= 0:
        return 0.0, 0.0
    step = _share_step_for_clob_price(clob_price)
    if step <= 0:
        return clob_price, 0.0
    clob_size = round(math.floor(float(raw_size) / step) * step, 2)
    if round(clob_price * clob_size, 2) <= 0 or clob_size <= 0:
        return clob_price, 0.0
    return clob_price, clob_size


def clob_fok_sell_price_and_size(raw_price: float, raw_size: float) -> tuple[float, float]:
    """SELL taker: floored limit price, size floored to valid step."""
    clob_price = clob_taker_sell_limit_price(raw_price)
    if clob_price <= 0:
        return 0.0, 0.0
    step = _share_step_for_clob_price(clob_price)
    if step <= 0:
        return clob_price, 0.0
    clob_size = round(math.floor(float(raw_size) / step) * step, 2)
    if round(clob_price * clob_size, 2) <= 0 or clob_size <= 0:
        return clob_price, 0.0
    return clob_price, clob_size


def min_clob_fok_buy_shares_across_prices(raw_size: float, buy_limit_prices: list[float]) -> float:
    """
    For a complete set (same economic size on every YES BUY leg), return the largest share count
    that is simultaneously postable on every leg at its limit price.
    """
    if not buy_limit_prices:
        return round(float(raw_size), 2)
    out = float(raw_size)
    for rp in buy_limit_prices:
        _, cs = clob_fok_buy_price_and_size(rp, raw_size)
        out = min(out, float(cs))
    return round(out, 2)
