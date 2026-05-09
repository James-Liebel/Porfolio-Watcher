"""CLOB FOK share-step rounding (must stay aligned with LiveClobExchange.place_order)."""

from src.arb.clob_rounding import (
    clob_fok_buy_price_and_size,
    min_clob_fok_buy_shares_across_prices,
)


def test_buy_price_and_size_matches_step_rules() -> None:
    # Ceil to $0.50 → gcd(50,100)=50 → share step $0.02
    p, s = clob_fok_buy_price_and_size(0.495, 15.37)
    assert p == 0.50
    assert s == 15.36


def test_complete_set_align_takes_min_across_leg_limit_prices() -> None:
    raw = 15.37
    prices = [0.41, 0.50]
    aligned = min_clob_fok_buy_shares_across_prices(raw, prices)
    _, s41 = clob_fok_buy_price_and_size(0.41, raw)
    _, s50 = clob_fok_buy_price_and_size(0.50, raw)
    assert aligned == min(s41, s50)
    assert aligned == 15.0
