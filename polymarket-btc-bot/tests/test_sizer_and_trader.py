from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest


def _make_settings(**overrides):
    from src.config import Settings

    defaults = dict(
        paper_trade=True,
        initial_bankroll=300.0,
        edge_threshold=0.07,
        entry_window_seconds=30,
        min_seconds_remaining=3,
        max_bet_fraction=0.06,
        kelly_fraction=0.20,
        target_edge_for_max_size=0.12,
        min_bet_usd=1.0,
        daily_loss_cap=0.10,
        min_market_liquidity=750.0,
        max_concurrent_positions=3,
        max_reposts_per_window=4,
        repost_stale_ticks=2,
        cancel_at_seconds_remaining=6,
        max_maker_aggression_ticks=3,
        maker_rebate_bps_assumption=0.0,
        max_positions_per_asset=1,
        max_total_exposure_pct=0.40,
        control_api_port=18765,
        log_level="WARNING",
    )
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def _make_window(*, seconds_remaining: float, liquidity: float):
    from src.markets.window import WindowState

    now = datetime.now(timezone.utc)
    window = WindowState(
        market_id="m1",
        condition_id="c1",
        question="Will BTC close higher?",
        yes_token_id="yes-token",
        no_token_id="no-token",
        start_time=now - timedelta(minutes=4),
        end_time=now + timedelta(seconds=seconds_remaining),
        asset="BTC",
    )
    window.window_open_price = Decimal("100000")
    window.current_yes_price = Decimal("0.48")
    window.current_no_price = Decimal("0.52")
    window.liquidity_yes = liquidity / 2
    window.liquidity_no = liquidity / 2
    window.seconds_remaining = seconds_remaining
    return window


def _make_signal():
    from src.signal.calculator import Direction, SignalResult

    return SignalResult(
        edge=0.09,
        trade_side="YES",
        true_prob=0.57,
        market_implied_prob=0.48,
        delta=0.004,
        direction=Direction.UP,
        tradeable=True,
        token_id="yes-token",
        asset="BTC",
    )


def test_compute_bet_size_increases_with_time_and_liquidity():
    from src.execution.sizer import compute_bet_size

    config = _make_settings()
    signal = _make_signal()
    bankroll = Decimal("100.00")

    early_thin = _make_window(seconds_remaining=28, liquidity=800)
    late_deep = _make_window(seconds_remaining=8, liquidity=3000)

    early_size = compute_bet_size(signal, bankroll, config, early_thin)
    late_size = compute_bet_size(signal, bankroll, config, late_deep)

    assert early_size > Decimal("0")
    assert late_size > early_size
    assert late_size <= Decimal("6.00")


def test_live_execute_posts_share_quantity_from_stake_budget(monkeypatch):
    from src.execution.trader import Trader
    from src.execution import trader as trader_module

    config = _make_settings(paper_trade=False)
    trader = Trader(config)
    window = _make_window(seconds_remaining=20, liquidity=2000)
    signal = _make_signal()
    captured = {}

    def fake_post(token_id, share_quantity, maker_price):
        captured["token_id"] = token_id
        captured["share_quantity"] = share_quantity
        captured["maker_price"] = maker_price
        return "order-1", False

    monkeypatch.setattr(trader_module.time, "sleep", lambda _: None)
    monkeypatch.setattr(trader, "_post_maker_order", fake_post)
    monkeypatch.setattr(trader, "_is_filled", lambda order_id: True)
    monkeypatch.setattr(trader, "_cancel_order", lambda order_id: None)

    filled, fill_price, reposts, maker_price, share_quantity, order_cost = trader._live_execute(
        window,
        signal,
        Decimal("10.00"),
        Decimal("0.53"),
        signal.token_id,
    )

    assert filled is True
    assert fill_price == Decimal("0.53")
    assert reposts == 0
    assert maker_price == Decimal("0.53")
    assert captured["token_id"] == "yes-token"
    assert captured["maker_price"] == Decimal("0.53")
    assert captured["share_quantity"] == Decimal("18.86")
    assert share_quantity == Decimal("18.86")
    assert order_cost == Decimal("9.9958")


def test_resolution_pnl_uses_share_quantity_and_rebate(monkeypatch):
    from src.execution.trader import TradeResult, Trader

    config = _make_settings()
    trader = Trader(config)

    class FakeClient:
        def get_market(self, market_id):
            return type("Market", (), {"resolution": "YES"})()

    monkeypatch.setattr(trader, "_get_client", lambda: FakeClient())

    result = TradeResult(
        trade_id=1,
        market_id="m1",
        question="Will BTC close higher?",
        side="YES",
        token_id="yes-token",
        asset="BTC",
        bet_size=Decimal("9.9958"),
        share_quantity=Decimal("18.86"),
        limit_price=Decimal("0.53"),
        filled=True,
        fill_price=Decimal("0.53"),
        outcome="PENDING",
        pnl=None,
        delta=0.004,
        edge=0.09,
        true_prob=0.57,
        market_prob=0.48,
        seconds_at_entry=12,
        timestamp=datetime.now(timezone.utc),
        paper_trade=True,
        maker_rebate_earned=Decimal("0.0100"),
    )

    outcome, pnl = trader._fetch_resolution(result)

    assert outcome == "WIN"
    assert pnl == pytest.approx(float(Decimal("18.86") * Decimal("0.47") + Decimal("0.0100")))


def test_signal_penalizes_low_liquidity_and_overround():
    from src.signal.calculator import compute

    config = _make_settings(edge_threshold=0.04)
    weak_window = _make_window(seconds_remaining=25, liquidity=200)
    weak_window.current_yes_price = Decimal("0.52")
    weak_window.current_no_price = Decimal("0.51")

    strong_window = _make_window(seconds_remaining=8, liquidity=4000)
    strong_window.current_yes_price = Decimal("0.48")
    strong_window.current_no_price = Decimal("0.50")

    weak_signal = compute(weak_window, Decimal("100500"), config, "BTC")
    strong_signal = compute(strong_window, Decimal("100500"), config, "BTC")

    assert weak_signal is not None
    assert strong_signal is not None
    assert strong_signal.edge > weak_signal.edge


def test_compute_maker_price_uses_market_tick_size(monkeypatch):
    from src.execution.trader import Trader

    config = _make_settings()
    trader = Trader(config)
    window = _make_window(seconds_remaining=9, liquidity=2000)
    window.minimum_tick_size = Decimal("0.001")
    signal = _make_signal()

    class Level:
        def __init__(self, price, size):
            self.price = price
            self.size = size

    class Book:
        tick_size = "0.001"
        bids = [Level("0.534", "100")]
        asks = [Level("0.538", "80")]

    class FakeClient:
        def get_order_book(self, token_id):
            return Book()

    monkeypatch.setattr(trader, "_get_client", lambda: FakeClient())

    maker_price = trader._compute_maker_price(signal.token_id, signal, window)

    assert maker_price is not None
    assert maker_price.as_tuple().exponent <= -3
    assert maker_price < Decimal("0.538")
